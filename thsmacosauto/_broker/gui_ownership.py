"""Fail-closed, cooperative local GUI lease. Not wired into production callers yet.

The operator provisions one canonical private directory for EVERY GUI writer.
Never remove/replace the persistent lock file, including after normal shutdown.
"""
from dataclasses import dataclass
import errno
import fcntl
import os
from pathlib import Path
import stat
import sys
import threading
from . import readonly_budget as rb


class OwnershipError(RuntimeError):
    """Public-safe reason code: no paths, PIDs, or account details."""


@dataclass(frozen=True)
class KnownWriter:
    name: str
    marker: str | Path


_LOCK_NAME = 'gui-owner.lock'
_registry = {}  # (device, inode) -> live lease; also excludes same-process threads
_mutex = threading.RLock()
_PROOF_CODES = frozenset({
    'GUI_OWNER_PATH_UNSAFE', 'GUI_OWNER_DIRECTORY_NOT_PRIVATE',
    'GUI_OWNER_LOCK_UNSAFE', 'GUI_OWNER_IDENTITY_CHANGED',
    'GUI_INCUMBENT_MARKER_PRESENT',
})


def _close_preserving(fd, primary):
    """One close attempt; cleanup cannot replace a proof failure/interrupt."""
    try:
        os.close(fd)
    except BaseException:
        if primary is None:
            raise
        primary.add_note('GUI owner descriptor cleanup failed')


def _fork_child():
    # flock belongs to the open file description. Never LOCK_UN in a child:
    # that would unlock the parent's lease. Close inherited copies instead.
    global _mutex, _registry
    leases = tuple(_registry.values())
    _registry = {}
    _mutex = threading.RLock()
    first_error = None
    for lease in leases:
        fd, lease._fd = lease._fd, None
        lease._invalid = True
        if fd is not None:
            try:
                os.close(fd)
            except BaseException as exc:
                # One ambiguous close must not abandon other inherited FDs.
                if first_error is None:
                    first_error = exc
    if first_error is not None:
        raise first_error  # Python reports the first hook failure as unraisable.


def _fork_prepare():
    _mutex.acquire()


def _fork_parent():
    _mutex.release()


os.register_at_fork(before=_fork_prepare, after_in_parent=_fork_parent,
                    after_in_child=_fork_child)


def _absolute(value):
    # Caller conversion and filesystem encoding are part of the safe boundary.
    # Validate the original token; never normalize/repair it for admission.
    try:
        raw = os.fspath(value)
        if (not isinstance(raw, str) or not raw.startswith('/') or '\x00' in raw
                or raw != os.path.normpath(raw) or raw.startswith('//')):
            raise ValueError
        os.fsencode(raw)
        return Path(raw)
    except Exception:
        raise OwnershipError('GUI_OWNER_PATH_UNSAFE') from None


def _identity(st):
    # Directory nlink changes as children appear (notably on APFS).
    return st.st_dev, st.st_ino, st.st_uid, stat.S_IMODE(st.st_mode)


def _walk(directory, *, private=False):
    """Open every component relative to pinned parent FDs; never follow links."""
    path = _absolute(directory)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', flags)
    identities = []
    try:
        for component in ('', *path.parts[1:]):
            if component:
                next_fd = os.open(component, flags, dir_fd=fd)
                old, fd = fd, next_fd
                os.close(old)
            st = os.fstat(fd)
            if (not stat.S_ISDIR(st.st_mode) or st.st_uid not in (0, os.geteuid())
                    or stat.S_IMODE(st.st_mode) & 0o022):
                raise OwnershipError('GUI_OWNER_PATH_UNSAFE')
            identities.append(_identity(st))
        if private and (st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700):
            raise OwnershipError('GUI_OWNER_DIRECTORY_NOT_PRIVATE')
        result, fd = fd, None
        return result, tuple(identities)
    finally:
        if fd is not None:
            _close_preserving(fd, sys.exception())


def _check_file(st):
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
            or stat.S_IMODE(st.st_mode) != 0o600 or st.st_nlink != 1):
        raise OwnershipError('GUI_OWNER_LOCK_UNSAFE')


def _writers_absent(writers, retained_capability=None, canonical=None):
    retained = None
    if retained_capability is not None:
        raise OwnershipError('GUI_OWNER_CONFIG_INVALID')
    for writer in writers:
        marker = _absolute(writer.marker)
        if retained is not None and str(marker) == retained:
            continue
        fd, _ = _walk(marker.parent)
        try:
            try:
                os.stat(marker.name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            # Any entry, even a dangling symlink, empty file or dead PID, refuses.
            raise OwnershipError('GUI_INCUMBENT_MARKER_PRESENT')
        finally:
            _close_preserving(fd, sys.exception())


class GUIOwnerLease:
    """Process-bound ownership proof, shareable by that process's GUI worker.

    No destructor: explicitly close only after the backend/worker has stopped.
    Any proof failure poisons this object permanently until close/reacquire.
    """
    def __init__(self, root, chain, fd, key, writers, retained_capability=None):
        self._root, self._chain, self._fd, self._key = root, chain, fd, key
        self._writers = writers
        self._retained_capability = retained_capability
        self._pid = os.getpid()
        self._invalid = False

    def assert_owned(self):
        with rb.cost('LEASE_TOTAL'):
            with _mutex:
                if (self._pid != os.getpid() or self._invalid or self._fd is None
                        or _registry.get(self._key) is not self):
                    raise OwnershipError('GUI_OWNER_NOT_HELD')
                directory_fd = None
                try:
                    try:
                        with rb.cost('CANONICAL_LOCK'):
                            directory_fd, chain = _walk(self._root, private=True)
                            if chain != self._chain:
                                raise OwnershipError('GUI_OWNER_IDENTITY_CHANGED')
                            opened = os.fstat(self._fd)
                            named = os.stat(_LOCK_NAME, dir_fd=directory_fd, follow_symlinks=False)
                            _check_file(opened)
                            _check_file(named)
                            if ((opened.st_dev, opened.st_ino) != self._key
                                    or _identity(opened) != _identity(named)):
                                raise OwnershipError('GUI_OWNER_IDENTITY_CHANGED')
                        _writers_absent(self._writers, self._retained_capability, self._root)
                    finally:
                        if directory_fd is not None:
                            fd, directory_fd = directory_fd, None
                            _close_preserving(fd, sys.exception())
                except BaseException as exc:
                    # Poison before any propagation, including interrupts and cleanup.
                    # This boundary excludes the arbitrary callback in call_owned.
                    self._invalid = True
                    if not isinstance(exc, Exception):
                        raise
                    if (type(exc) is OwnershipError and len(exc.args) == 1
                            and isinstance(exc.args[0], str) and exc.args[0] in _PROOF_CODES):
                        raise
                    raise OwnershipError('GUI_OWNER_PROOF_FAILED') from None
            return None

    def call_passive(self, function, /):
        """Retain serialization; the attested passive RPC supplies its final proof.

        No disk witness is reused. Timeout configuration may precede discovery
        of disk revocation; the observation must follow a fresh full proof.
        """
        with _mutex:
            if (self._pid != os.getpid() or self._invalid or self._fd is None
                    or _registry.get(self._key) is not self):
                raise OwnershipError('GUI_OWNER_NOT_HELD')
            return function()

    def call_owned(self, function, /, *args, **kwargs):
        """Revalidate immediately before a synchronous native operation.

        Serializes against close and other guarded calls. Do not return a future
        or defer the native operation; wrap its actual execution on the worker.
        """
        with _mutex:
            self.assert_owned()
            return function(*args, **kwargs)

    def close(self):
        with _mutex:
            # Consume the FD before closing: do not retry a recycled FD number.
            fd, self._fd = self._fd, None
            self._invalid = True
            if self._pid == os.getpid() and _registry.get(self._key) is self:
                del _registry[self._key]
            if fd is not None:
                # Closing releases flock; no LOCK_UN (fork-shared description).
                os.close(fd)

    def __enter__(self):
        self.assert_owned()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.close()
        except OSError as cleanup:
            if exc is None:
                raise
            exc.add_note('GUI owner close failed: ' + type(cleanup).__name__)


def acquire_gui_owner(canonical_directory, *, known_writers, inventory_complete,
                      candidate_enabled=False, retained_capability=None):
    """Acquire before constructing ANY native backend, not per-state SQLite.

    Explicit inventory attestation is required even for an empty writer tuple.
    The directory must already exist. This function never creates directories,
    removes incumbent markers, probes PIDs, or terminates processes.
    """
    if candidate_enabled is not True:
        raise OwnershipError('GUI_CANDIDATE_DISABLED')
    if inventory_complete is not True or not isinstance(known_writers, (tuple, list)):
        raise OwnershipError('GUI_WRITER_INVENTORY_REQUIRED')
    writers = tuple(known_writers)
    if (any(not isinstance(w, KnownWriter) or not isinstance(w.name, str)
            or not w.name.strip() for w in writers)
            or len({w.name for w in writers}) != len(writers)):
        raise OwnershipError('GUI_WRITER_INVENTORY_REQUIRED')
    root = _absolute(canonical_directory)
    for writer in writers:
        if _absolute(writer.marker).name == '':
            raise OwnershipError('GUI_OWNER_PATH_UNSAFE')
    with _mutex:
        directory_fd = lock_fd = None
        try:
            directory_fd, chain = _walk(root, private=True)
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            try:
                lock_fd = os.open(_LOCK_NAME, flags | os.O_CREAT | os.O_EXCL,
                                  0o600, dir_fd=directory_fd)
            except FileExistsError:
                lock_fd = os.open(_LOCK_NAME, flags, dir_fd=directory_fd)
            opened = os.fstat(lock_fd)
            _check_file(opened)
            key = opened.st_dev, opened.st_ino
            if key in _registry:
                raise OwnershipError('GUI_OWNER_BUSY')
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EACCES):
                    raise OwnershipError('GUI_OWNER_BUSY') from None
                raise
            lease = GUIOwnerLease(root, chain, lock_fd, key, writers, retained_capability)
            _registry[key] = lease
            lock_fd = None  # lease now owns exactly one FD
            try:
                lease.assert_owned()  # markers + path identity, AFTER flock
                # Release the acquisition directory before publishing the lease.
                # If close fails, its FD is consumed and the unreturned lease closes.
                fd, directory_fd = directory_fd, None
                os.close(fd)
            except BaseException as primary:
                try:
                    lease.close()
                except BaseException:
                    primary.add_note('GUI owner lease cleanup failed')
                raise
            return lease
        except OSError:
            raise OwnershipError('GUI_OWNER_ACQUIRE_FAILED') from None
        finally:
            primary = sys.exception()
            try:
                if lock_fd is not None:
                    fd, lock_fd = lock_fd, None
                    _close_preserving(fd, primary)
            finally:
                if directory_fd is not None:
                    fd, directory_fd = directory_fd, None
                    _close_preserving(fd, sys.exception())
