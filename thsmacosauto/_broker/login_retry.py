"""Explicit single successor to historical UNKNOWN; original bytes never replaced.

All entry points run under the Broker's existing exclusive GUI owner/budget.
The exclusive claim survives every crash/refusal after authorization. No cleanup
or success path deletes it; another UNKNOWN requires manual investigation.
"""
from pathlib import Path
import hashlib
import json
import os
import stat
import time
from . import readonly_budget as rb
from .login_auto import LoginFailure, discover, _dirsync
from .disconnect_warning import scan, Refused

COOLDOWN_NS = 60_000_000_000
CLAIM = 'login-retry-authorization.json'
ATTEMPT = 'login-retry-attempt.json'
FRESH = 'login-fresh-attempt.json'
ORIGINAL = 'login-auto-attempt.json'


def legacy_artifacts(directory):
    # The archive alone is a fence (including a partially written or linked file).
    names=[]
    with os.scandir(directory) as entries:
        for i,p in enumerate(entries):
            rb.check()
            if i>=4096:raise LoginFailure('LOGIN_FRESH_BLOCKED')
            if p.name in (CLAIM, ATTEMPT) or p.name.startswith('login-unknown-'):names.append(p.name)
    return tuple(sorted(names))


def pending_writes(directory):
    return any(os.path.lexists((directory/n).with_suffix('.success.tmp'))
               for n in (ORIGINAL, ATTEMPT, FRESH))


def historical(path):
    raw, modified = _read_record(path)
    if not 0 < modified <= time.time_ns():
        raise LoginFailure('LOGIN_RETRY_BLOCKED')
    data = json.loads(raw, object_pairs_hook=_pairs)
    # Historical deployed marker predates the SUCCESS rearm schema.
    if (type(data) is not dict or data.get('state') != 'UNKNOWN'
            or data.get('schema') != 'THS_LOGIN_ATTEMPT_V1'
            or set(data) not in ({'schema', 'state'}, {'schema', 'state', 'success_ns'})
            or ('success_ns' in data and (type(data['success_ns']) is not int or data['success_ns'] != 0))):
        raise LoginFailure('LOGIN_RETRY_BLOCKED')
    return raw, hashlib.sha256(raw).hexdigest(), modified


def _pairs(items):
    result = dict(items)
    if len(result)!=len(items):raise ValueError('DUPLICATE_LOGIN_RECORD_KEY')
    return result


def _read_record(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        st = os.fstat(f.fileno())
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or stat.S_IMODE(st.st_mode) != 0o600 or st.st_nlink != 1
                or st.st_size > 1024):
            raise LoginFailure('LOGIN_RETRY_BLOCKED')
        raw = f.read(1025)
        after = os.fstat(f.fileno())
        if len(raw) != st.st_size or (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (st.st_size, st.st_mtime_ns, st.st_ctime_ns):
            raise LoginFailure('LOGIN_RETRY_BLOCKED')
    return raw, st.st_mtime_ns


def _spent_legacy(directory):
    original, digest, modified = historical(directory/ORIGINAL)
    archive_name = 'login-unknown-'+digest+'.json'
    if pending_writes(directory) or set(legacy_artifacts(directory))!={CLAIM, ATTEMPT, archive_name}:
        raise LoginFailure('LOGIN_FRESH_BLOCKED')
    archive = _read_record(directory/archive_name)
    claim = _read_record(directory/CLAIM)
    successor = historical(directory/ATTEMPT)
    data = json.loads(claim[0], object_pairs_hook=_pairs)
    authorized = data.get('authorized_ns') if type(data) is dict else None
    if (type(authorized) is not int or not 0 < modified <= authorized <= successor[2]
            or data != {'schema':'THS_LOGIN_RETRY_V1','state':'CLAIMED','historical_sha256':digest,
                        'historical_mtime_ns':modified,'authorized_ns':authorized,'attempt':ATTEMPT}
            or type(data['historical_mtime_ns']) is not int or archive[0] != original):
        raise LoginFailure('LOGIN_FRESH_BLOCKED')
    if time.time_ns()-successor[2]<COOLDOWN_NS:
        raise LoginFailure('LOGIN_RETRY_COOLDOWN')
    return (original, modified, archive, claim, successor)


def fresh_login(owner, budget):
    """One separately authorized login after the complete legacy attempt chain.

    Keep the old authorization and both old attempts intact. A surviving new
    marker, including a partial record, blocks another submission.
    """
    result = {'status':'LOGIN_FRESH_BLOCKED','press_attempted':False,
              'execution_enabled':False,'transaction_qualified':False}
    rb.require(budget); owner._owner_proof(); budget.check()
    if not owner.config.get('allow_navigation', False):
        return {**result,'status':'NAVIGATION_DISABLED'}
    if owner._original is not None or owner._attempt is not None:
        return {**result,'status':'NATIVE_SESSION_UNCERTAIN'}
    current = owner._login_entry('login-status', budget)
    if current['already_logged_in']:
        return {**result,'status':'ALREADY_LOGGED_IN'}
    if current['status']=='LOGIN_COMMUNICATION_ERROR':
        return {**result,**{k:current[k] for k in ('status','authenticated','authentication_state','observed_at','provenance')}}
    directory = Path(owner._gui_owner._root)
    marker = directory/FRESH
    if os.path.lexists(marker):
        return {**result,'status':'LOGIN_FRESH_USED'}
    try:
        legacy = _spent_legacy(directory)
    except (OSError, ValueError, LoginFailure) as exc:
        return {**result,'status':'LOGIN_RETRY_COOLDOWN' if isinstance(exc,LoginFailure) and exc.code=='LOGIN_RETRY_COOLDOWN' else 'LOGIN_FRESH_BLOCKED'}

    def validate_legacy():
        try:
            if _spent_legacy(directory)==legacy:return
        except (OSError, ValueError, LoginFailure):
            pass
        raise LoginFailure('LOGIN_FRESH_BLOCKED')

    from .login_auto import _attempt
    return _attempt(owner,budget,retry_marker=marker,retry_guard=validate_legacy)


def _exclusive(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(raw); f.flush(); os.fsync(f.fileno())
    _dirsync(path.parent)


def retry(owner, budget):
    result = {'status': 'LOGIN_RETRY_BLOCKED', 'press_attempted': False,
              'execution_enabled': False, 'transaction_qualified': False}
    claimed = False
    try:
        rb.require(budget); owner._owner_proof(); budget.check()
        if not owner.config.get('allow_navigation', False):
            return {**result, 'status': 'NAVIGATION_DISABLED'}
        if owner._original is not None or owner._attempt is not None:
            return {**result, 'status': 'NATIVE_SESSION_UNCERTAIN'}
        current = owner._login_entry('login-status', budget)
        if current['already_logged_in']:
            return {**result, 'status': 'ALREADY_LOGGED_IN'}
        directory = Path(owner._gui_owner._root)
        marker = directory / ORIGINAL
        # lexists also rejects dangling symlinks and partial claims.
        if legacy_artifacts(directory) or pending_writes(directory) or os.path.lexists(directory/FRESH):
            return {**result, 'status': 'LOGIN_RETRY_USED'}
        if current['status']=='LOGIN_COMMUNICATION_ERROR':
            return {**result,**{k:current[k] for k in ('status','authenticated','authentication_state','observed_at','provenance')}}
        raw, digest, modified = historical(marker)
        if time.time_ns() - modified < COOLDOWN_NS:
            return {**result, 'status': 'LOGIN_RETRY_COOLDOWN'}
        app, session, identity = owner._connect(); root = session.scope().root
        front = owner._owned_effect(owner._runtime.front)
        key = app.backend.key

        def sample():
            owner._owner_proof(); budget.check()
            if (owner._original is not None or owner._attempt is not None
                    or owner._owned_effect(owner._runtime.front) != front
                    or owner._owned_effect(owner._runtime.app_identity) != identity
                    or key(session.scope().root) != key(root)):
                raise LoginFailure('LOGIN_TARGET_CHANGED')
            if scan(owner, app, root) is not None:
                raise LoginFailure('LOGIN_RETRY_FORM_REQUIRED')
            state = owner._login_entry('login-status', budget)
            forms = [f for f in state['auth_forms'] if f.get('signature')]
            if (state['already_logged_in'] or len(forms) != 1
                    or forms[0]['role'] != 'AXSheet' or forms[0]['identifier'] != '_NS:259'
                    or forms[0]['field_count'] != 6 or forms[0]['secure_field_count'] != 1):
                raise LoginFailure('LOGIN_RETRY_FORM_REQUIRED')
            return discover(owner, app, root)

        proof, children, challenge = sample()
        def validate():
            if (legacy_artifacts(directory) or pending_writes(directory)
                    or os.path.lexists(directory/FRESH)):
                raise LoginFailure('LOGIN_RETRY_USED')
            fresh, refs, text = sample()
            if fresh != proof or text != challenge or tuple(map(key, refs)) != tuple(map(key, children)):
                raise LoginFailure('LOGIN_TARGET_CHANGED')
            if historical(marker) != (raw, digest, modified):
                raise LoginFailure('LOGIN_RETRY_BLOCKED')
        validate()  # Two agreeing fresh complete proofs before authorization.
        archive = directory / ('login-unknown-' + digest + '.json')
        # Any failed/partial archive is itself a permanent refusal, not a reset.
        if os.path.lexists(archive):
            return {**result, 'status': 'LOGIN_RETRY_USED'}
        _exclusive(archive, raw)
        authorization = {'schema': 'THS_LOGIN_RETRY_V1', 'state': 'CLAIMED',
                         'historical_sha256': digest, 'historical_mtime_ns': modified,
                         'authorized_ns': time.time_ns(), 'attempt': ATTEMPT}
        authorization_raw=(json.dumps(authorization, sort_keys=True) + '\n').encode()
        _exclusive(directory / CLAIM, authorization_raw)
        claimed = True
        authorized_cohort={archive.name, CLAIM}
        def validate_attempt():
            if (set(legacy_artifacts(directory)) - {ATTEMPT} != authorized_cohort
                    or pending_writes(directory) or os.path.lexists(directory/FRESH)
                    or historical(marker)!=(raw,digest,modified)
                    or _read_record(archive)[0]!=raw or _read_record(directory/CLAIM)[0]!=authorization_raw):
                raise LoginFailure('LOGIN_RETRY_BLOCKED')
            if (owner._owned_effect(owner._runtime.front)!=front or owner._owned_effect(owner._runtime.app_identity)!=identity
                    or key(session.scope().root)!=key(root) or key(app.backend.root(app.pid))!=key(app._root)):
                raise LoginFailure('LOGIN_TARGET_CHANGED')
        # No reusable token or marker-path override is exposed to public callers.
        # attempt persists its independent UNKNOWN before any native write.
        from .login_auto import _attempt
        return _attempt(owner, budget, retry_marker=directory / ATTEMPT, retry_guard=validate_attempt)
    except LoginFailure as e:
        return {**result, 'status': e.code}
    except Refused:
        return {**result, 'status': 'LOGIN_RETRY_FORM_REQUIRED'}
    except Exception:
        return {**result, 'status': 'LOGIN_RETRY_USED' if claimed else 'LOGIN_RETRY_BLOCKED'}
