"""Isolated bounded timing/fault journal. No engine, broker, SDK or network imports.

Trace settlement is memory-only. Journal.append is synchronous durable IO and
MUST be called after the GUI owner/press/business locks are released.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid

STAGES = frozenset({'QUEUE', 'SIGNAL', 'BACKEND', 'OBSERVE', 'VALIDATE', 'PREPARED',
                    'PREPARE', 'SUBMIT', 'CONFIRMATION', 'CONFIRM', 'RECONCILE',
                    'RESTORE', 'SETTLE', 'CHILD_START', 'CHILD_WAIT', 'CHILD_REAP'})
CODES = frozenset({'OK', 'REJECTED', 'FAILED', 'UNKNOWN', 'TIMEOUT', 'CANCELLED',
                   'IDENTITY_REJECTED', 'STALE_CAPTURE', 'PERSISTENCE_FAILED',
                   'RESTORE_FAILED', 'CHILD_STARTED', 'CHILD_EXITED',
                   'CHILD_TERMINATED', 'CHILD_KILLED', 'CHILD_REAPED',
                   'CHILD_REAP_UNCONFIRMED'})
OUTCOMES = frozenset({'OK', 'BLOCKED', 'FAILED', 'UNKNOWN', 'CANCELLED'})
MAX_STEPS = 32
MAX_RECORD = 16384
LIMIT = 2**63 - 1


class TelemetryError(Exception):
    """Only a fixed code is public, never OS paths, exception text or payloads."""


def _integer(value):
    return type(value) is int and 0 <= value <= LIMIT


def _enum(value, allowed):
    return type(value) is str and value in allowed


def validate(record):
    """Closed schema: reject rather than redact credentials/amount/exception fields."""
    keys = {'version', 'request_id', 'source_capture_start_ms', 'started_utc_ns',
            'ended_utc_ns', 'wall_clock_discontinuous', 'total_ns', 'leaf_ns',
            'unattributed_ns', 'outcome', 'event_code', 'steps'}
    if type(record) is not dict or set(record) != keys:
        raise TelemetryError('UNSAFE_RECORD')
    rid = record['request_id']
    if type(rid) is not str or re.fullmatch(r'[0-9a-f]{32}', rid) is None:
        raise TelemetryError('UNSAFE_RECORD')
    integers = ('source_capture_start_ms', 'started_utc_ns', 'ended_utc_ns',
                'total_ns', 'leaf_ns', 'unattributed_ns')
    if (type(record['version']) is not int or record['version'] != 1
            or not all(_integer(record[k]) for k in integers)
            or type(record['wall_clock_discontinuous']) is not bool
            or not _enum(record['outcome'], OUTCOMES)
            or not _enum(record['event_code'], CODES)
            or type(record['steps']) is not list or len(record['steps']) > MAX_STEPS):
        raise TelemetryError('UNSAFE_RECORD')
    previous = 0
    leaf = 0
    for step in record['steps']:
        if (type(step) is not dict or set(step) != {'stage', 'event_code', 'offset_ns', 'duration_ns'}
                or not _enum(step['stage'], STAGES) or not _enum(step['event_code'], CODES)
                or not _integer(step['offset_ns']) or not _integer(step['duration_ns'])
                or step['offset_ns'] < previous):
            raise TelemetryError('UNSAFE_RECORD')
        previous = step['offset_ns'] + step['duration_ns']
        leaf += step['duration_ns']
    failed_codes = CODES - {'OK', 'CHILD_STARTED', 'CHILD_EXITED',
                            'CHILD_TERMINATED', 'CHILD_KILLED', 'CHILD_REAPED'}
    if record['outcome'] == 'OK' and (record['event_code'] in failed_codes
            or any(step['event_code'] in failed_codes for step in record['steps'])):
        raise TelemetryError('FAILURE_WASH')
    if (previous > record['total_ns'] or leaf != record['leaf_ns']
            or leaf + record['unattributed_ns'] != record['total_ns']):
        raise TelemetryError('UNSAFE_RECORD')
    raw = json.dumps(record, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'
    if len(raw) > MAX_RECORD:
        raise TelemetryError('RECORD_LIMIT')
    return raw


class Trace:
    """One serial owner, no nested/double-counted leaves, no business clock reset."""
    def __init__(self, source_capture_start_ms, *, monotonic_ns=time.monotonic_ns,
                 wall_ns=time.time_ns):
        if not _integer(source_capture_start_ms):
            raise TelemetryError('UNSAFE_CAPTURE_TIME')
        self.request_id = uuid.uuid4().hex  # Never caller's account/order/token string.
        self._capture = source_capture_start_ms
        self._mono, self._wall = monotonic_ns, wall_ns
        self._start = monotonic_ns()
        self._utc = wall_ns()
        self._steps = []
        self._open = None
        self._finished = False

    def begin(self, stage):
        if self._finished or self._open is not None or not _enum(stage, STAGES):
            raise TelemetryError('INVALID_STAGE')
        if len(self._steps) >= MAX_STEPS:
            raise TelemetryError('STEP_LIMIT')
        self._open = (stage, self._mono())

    def end(self, event_code='OK'):
        if self._finished or self._open is None or not _enum(event_code, CODES):
            raise TelemetryError('INVALID_STAGE')
        stage, start = self._open
        end = self._mono()
        if end < start or start < self._start:
            raise TelemetryError('MONOTONIC_DISCONTINUITY')
        self._steps.append({'stage':stage, 'event_code':event_code,
                            'offset_ns':start-self._start, 'duration_ns':end-start})
        self._open = None

    def finish(self, outcome, event_code):
        if self._finished or self._open is not None:
            raise TelemetryError('UNSETTLED_TRACE')
        # Capture elapsed before consulting wall time. No mutation of source anchor.
        total = self._mono() - self._start
        utc = self._wall()
        leaf = sum(step['duration_ns'] for step in self._steps)
        record = {'version':1, 'request_id':self.request_id,
                  'source_capture_start_ms':self._capture, 'started_utc_ns':self._utc,
                  'ended_utc_ns':utc, 'wall_clock_discontinuous':abs((utc-self._utc)-total) > 1_000_000_000,
                  'total_ns':total, 'leaf_ns':leaf, 'unattributed_ns':total-leaf,
                  'outcome':outcome, 'event_code':event_code,
                  'steps':[dict(step) for step in self._steps]}
        validate(record)
        self._finished = True
        return record


def _close_after_failure(fd):
    """Best effort for an already-consumed FD; never replace a primary failure."""
    if fd is not None:
        try:
            os.close(fd)
        except BaseException:
            pass


def _directory(path, *, create=False):
    """Walk absolute components with openat/O_NOFOLLOW; pin each directory FD."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or str(path) == '/':
        raise TelemetryError('UNSAFE_DIRECTORY')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, component in enumerate(path.parts[1:]):
            if create and index == len(path.parts[1:])-1:
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                # The final directory's own fsync cannot persist its parent entry.
                # Also sync an existing entry to recover an earlier ambiguous create.
                os.fsync(fd)
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            previous, fd = fd, child  # Transfer before close: its integer may be reused.
            os.close(previous)
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
            raise TelemetryError('UNSAFE_DIRECTORY')
        return fd
    except BaseException:
        fd, closing = None, fd
        _close_after_failure(closing)
        raise


class Journal:
    """Exclusive single writer, bounded rotating JSONL, file+directory fsync ACK.

    Any IO ambiguity fences this instance. No retry, success substitution, SDK,
    GUI, notification dispatch, or per-write worker. Construction does disk IO.
    """
    def __init__(self, directory, *, max_bytes=65536, archives=3):
        if (type(max_bytes) is not int or not MAX_RECORD <= max_bytes <= 1048576
                or type(archives) is not int or not 1 <= archives <= 8):
            raise TelemetryError('INVALID_BUDGET')
        self._dir = self._owner = None
        self._path = Path(directory)
        self._max, self._archives = max_bytes, archives
        self._mutex = threading.Lock()
        self._fenced = False
        try:
            self._dir = _directory(directory, create=True)
            self._identity = self._identity_of(self._dir)
            self._owner = self._open('writer.lock', os.O_CREAT | os.O_RDWR)
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            allowed = {'writer.lock', 'events.jsonl'} | {f'events.{i}.jsonl' for i in range(1, archives+1)}
            if set(os.listdir(self._dir)) - allowed:
                raise TelemetryError('UNEXPECTED_DIRECTORY_ENTRY')
            for name in sorted(allowed - {'writer.lock'}):
                try:
                    fd = self._open(name, os.O_RDONLY)
                except FileNotFoundError:
                    continue
                try:
                    if os.fstat(fd).st_size > max_bytes:
                        raise TelemetryError('RETENTION_LIMIT')
                    data = b''
                    while True:
                        chunk = os.read(fd, max_bytes+1-len(data))
                        if not chunk:
                            break
                        data += chunk
                        if len(data) > max_bytes:
                            raise TelemetryError('RETENTION_LIMIT')
                    if data and not data.endswith(b'\n'):
                        raise TelemetryError('JOURNAL_CORRUPT')
                    for line in data.splitlines(keepends=True):
                        if len(line) > MAX_RECORD:
                            raise TelemetryError('RECORD_LIMIT')
                        validate(json.loads(line))
                except BaseException:
                    fd, closing = None, fd
                    _close_after_failure(closing)
                    raise
                else:
                    fd, closing = None, fd
                    os.close(closing)
            os.fsync(self._dir)
        except BaseException as exc:
            try:
                self.close()
            except BaseException:
                pass
            if isinstance(exc, Exception):
                raise TelemetryError('JOURNAL_UNAVAILABLE') from None
            raise

    @staticmethod
    def _identity_of(fd):
        st = os.fstat(fd)
        return st.st_dev, st.st_ino

    def _open(self, name, flags):
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self._dir)
        try:
            st = os.fstat(fd)
            if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid()
                    or stat.S_IMODE(st.st_mode) != 0o600
                    or (name == 'writer.lock' and st.st_size != 0)):
                raise TelemetryError('UNSAFE_FILE')
        except BaseException:
            fd, closing = None, fd
            _close_after_failure(closing)
            raise
        return fd

    def _check_directory(self):
        if os.fstat(self._owner).st_size != 0:
            raise TelemetryError('UNSAFE_FILE')
        other = _directory(self._path)
        try:
            if self._identity_of(other) != self._identity:
                raise TelemetryError('DIRECTORY_REPLACED')
        except BaseException:
            other, closing = None, other
            _close_after_failure(closing)
            raise
        else:
            other, closing = None, other
            os.close(closing)

    def _rotate(self):
        # Validate all extant leaves before any rename; never follow targets.
        for i in range(self._archives+1):
            name = 'events.jsonl' if i == 0 else f'events.{i}.jsonl'
            try:
                fd = self._open(name, os.O_RDONLY)
            except FileNotFoundError:
                continue
            fd, closing = None, fd
            os.close(closing)
        for i in range(self._archives, 0, -1):
            source = 'events.jsonl' if i == 1 else f'events.{i-1}.jsonl'
            try:
                os.rename(source, f'events.{i}.jsonl', src_dir_fd=self._dir, dst_dir_fd=self._dir)
            except FileNotFoundError:
                pass
        os.fsync(self._dir)

    def append(self, record):
        # Snapshot bytes now; caller mutation cannot alter persisted bytes.
        raw = validate(record)
        request_id = json.loads(raw)['request_id']
        with self._mutex:
            if self._fenced or self._dir is None:
                raise TelemetryError('JOURNAL_FENCED')
            fd = None
            try:
                self._check_directory()
                fd = self._open('events.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND)
                if os.fstat(fd).st_size + len(raw) > self._max:
                    fd, closing = None, fd
                    os.close(closing)
                    self._rotate()
                    fd = self._open('events.jsonl', os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                view = memoryview(raw)
                while view:
                    count = os.write(fd, view)
                    if count <= 0:
                        raise OSError('NO_PROGRESS')
                    view = view[count:]
                os.fsync(fd)
                fd, closing = None, fd
                os.close(closing)
                os.fsync(self._dir)
            except BaseException as exc:
                self._fenced = True
                fd, closing = None, fd
                _close_after_failure(closing)
                if isinstance(exc, Exception):
                    raise TelemetryError('DURABILITY_UNKNOWN') from None
                raise
        return {'request_id':request_id, 'status':'FILE_AND_DIRECTORY_SYNCED'}

    def close(self):
        with self._mutex:
            failures = []
            for key in ('_owner', '_dir'):
                fd = getattr(self, key, None)
                setattr(self, key, None)
                if fd is not None:
                    try:
                        os.close(fd)
                    except BaseException as exc:
                        failures.append(exc)
            if failures:
                for exc in failures:
                    if not isinstance(exc, Exception):
                        raise exc
                raise TelemetryError('JOURNAL_CLOSE_FAILED') from None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is None:
            self.close()
        else:
            try:
                self.close()
            except BaseException:
                pass  # The body failure remains primary, including interrupts.
