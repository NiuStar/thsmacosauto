"""Explicit state provisioning and existing-source admission, never implicit startup.

Receipts bind private file identity, schema, domain and declared source authority.
They do not assert real account qualification. Only synthetic fresh provisioning
is supported; real adoption needs an independently hash-pinned authority file.
"""
import hashlib, json, os, re, sqlite3, stat
from pathlib import Path
from .journal import JournalError, canonical

RECEIPT='state-identity.json'
SCHEMAS={'journal.sqlite3':'0b721b5814bbeea2fc02f70a445fd7794d51193c2d5d23b6b5a97bae3297db65',
         'control.sqlite3':'485309a6c067417de8da6b7b673edda09868cfbbbb5f152e05c8282e22c170b5'}
TOKEN=re.compile(r'[A-Za-z0-9_.:-]{1,128}')

def _identity(st):return [st.st_dev,st.st_ino]
def _valid_identity(value):
    return type(value) is list and len(value)==2 and all(type(n) is int and n>=0 for n in value)

def _path(value):
    p=Path(value).absolute()
    if str(p)!=str(value) or '..' in p.parts:raise JournalError('UNSAFE_STATE_PATH')
    for part in (*reversed(p.parents),p):
        if part.is_symlink():raise JournalError('UNSAFE_STATE_PATH')
    st=p.stat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o700:
        raise JournalError('STATE_DIRECTORY_NOT_PRIVATE')
    return p

def _regular(p):
    st=p.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o600:
        raise JournalError('TRANSACTION_STATE_UNADMITTED')
    return st

def _read(p,*,with_raw=False):
    _regular(p)
    fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        before=os.fstat(fd)
        if _identity(before)!=_identity(p.lstat()):raise JournalError('TRANSACTION_STATE_UNADMITTED')
        data=os.read(fd,16385)
        if len(data)>16384:raise JournalError('TRANSACTION_STATE_UNADMITTED')
        def pairs(items):
            d={}
            for k,v in items:
                if k in d:raise ValueError()
                d[k]=v
            return d
        after=os.fstat(fd)
        fields=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        if fields(before)!=fields(after) or fields(after)!=fields(p.lstat()):raise JournalError('TRANSACTION_STATE_UNADMITTED')
        value=json.loads(data,object_pairs_hook=pairs)
        return (value,data) if with_raw else value
    finally:os.close(fd)

def _metadata(st):
    return (st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns,st.st_mode,st.st_uid,st.st_nlink)


def _coherent(p):
    for name in ('journal.sqlite3','control.sqlite3'):
        for suffix in ('-journal','-wal','-shm'):
            if os.path.lexists(p/(name+suffix)):raise JournalError('TRANSACTION_STATE_UNADMITTED')
    st=_path(str(p)).stat()
    return {'directory':(st.st_dev,st.st_ino,st.st_mode,st.st_uid),**{n:_metadata(_regular(p/n)) for n in ('owner.lock','journal.sqlite3','control.sqlite3')}}


def _stream_digest(p):
    before=_regular(p);fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        if _metadata(before)!=_metadata(os.fstat(fd)):raise JournalError('STATE_AUTHORITY_MISMATCH')
        h=hashlib.sha256()
        while True:
            chunk=os.read(fd,65536)
            if not chunk:break
            h.update(chunk)
        if _metadata(before)!=_metadata(os.fstat(fd)) or _metadata(before)!=_metadata(_regular(p)):raise JournalError('STATE_AUTHORITY_MISMATCH')
        return h.hexdigest()
    finally:os.close(fd)


def validate_pair(p):
    """Read-only immutable connections: no recovery, schema init or sidecars."""
    from .journal import Journal
    conns=[]
    try:
        for name in ('journal.sqlite3','control.sqlite3'):
            _regular(p/name)
            c=sqlite3.connect((p/name).as_uri()+'?mode=ro&immutable=1',uri=True,isolation_level=None)
            conns.append(c)
            c.row_factory=sqlite3.Row
            if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise JournalError('TRANSACTION_STATE_UNADMITTED')
            rows=[tuple(r) for r in c.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]
            if hashlib.sha256(json.dumps(rows,separators=(',',':')).encode()).hexdigest()!=SCHEMAS[name]:
                raise JournalError('TRANSACTION_STATE_UNADMITTED')
        j=Journal.__new__(Journal);j.conn=conns[0];import threading;j._lock=threading.RLock();j._validate()
        c=conns[1]
        if c.execute('PRAGMA user_version').fetchone()[0]!=1:raise JournalError('TRANSACTION_STATE_UNADMITTED')
        tables=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        if tables!=['control'] or [r[1] for r in c.execute('PRAGMA table_info(control)')]!=['id','generation','stopped']:
            raise JournalError('TRANSACTION_STATE_UNADMITTED')
        rows=c.execute('SELECT id,generation,stopped FROM control').fetchall()
        if len(rows)!=1 or rows[0][0]!=1 or type(rows[0][1]) is not int or rows[0][1]<0 or rows[0][2] not in (0,1):raise JournalError('TRANSACTION_STATE_UNADMITTED')
    finally:
        import sys
        primary=sys.exc_info()[1]
        failure=None
        for c in conns:
            try:c.close()
            except BaseException as error:
                if failure is None:failure=error
        if primary is None and failure is not None:raise failure

def _authority(value):
    """Separate constructor configuration, never derived from the receipt."""
    if type(value) is not tuple or len(value)!=2:raise JournalError('STATE_AUTHORITY_REQUIRED')
    path,digest=value
    if type(path) is not str or not Path(path).is_absolute() or str(Path(path))!=path:raise JournalError('STATE_AUTHORITY_REQUIRED')
    p=Path(path)
    if '..' in p.parts or any(part.is_symlink() for part in (*reversed(p.parents),p)):raise JournalError('STATE_AUTHORITY_REQUIRED')
    a,raw=_read(p,with_raw=True)
    if type(digest) is not str or not re.fullmatch('[0-9a-f]{64}',digest) or hashlib.sha256(raw).hexdigest()!=digest:raise JournalError('STATE_AUTHORITY_MISMATCH')
    _validate_authority(a)
    return a,digest


def _validate_authority(a):
    keys={'schema','identity','domain','source','history_coverage_complete','account_scope_bound','coherent_source','files'}
    if type(a) is not dict:raise JournalError('STATE_AUTHORITY_REQUIRED')
    real=a.get('domain')=='REAL'
    if real:keys|={'directory','file_identities'}
    if (set(a)!=keys or a['schema']!=('THS_STATE_SOURCE_AUTHORITY_V2' if real else 'THS_STATE_SOURCE_AUTHORITY_V1')
            or a['domain'] not in ('REAL','SYNTHETIC')
            or any(type(a[k]) is not str or not TOKEN.fullmatch(a[k]) for k in ('identity','source'))
            or any(a[k] is not True for k in ('history_coverage_complete','account_scope_bound','coherent_source'))
            or type(a['files']) is not dict or set(a['files'])!={'journal.sqlite3','control.sqlite3'}
            or any(type(h) is not str or not re.fullmatch('[0-9a-f]{64}',h) for h in a['files'].values())):raise JournalError('STATE_AUTHORITY_REQUIRED')
    if real and (not _valid_identity(a['directory']) or type(a['file_identities']) is not dict
            or set(a['file_identities'])!={'owner.lock','journal.sqlite3','control.sqlite3'}
            or any(not _valid_identity(v) for v in a['file_identities'].values())):raise JournalError('STATE_AUTHORITY_REQUIRED')


def admit(state_dir,identity,*,transaction_authority=None):
    try:
        if type(identity) is not str or not TOKEN.fullmatch(identity):raise ValueError()
        p=_path(state_dir);r=_read(p/RECEIPT)
        if (type(r) is not dict or set(r)!={'schema','identity','domain','source','authority_sha256','directory','files'}
                or r['schema']!='THS_TRANSACTION_STATE_V1' or r['identity']!=identity
                or r['domain'] not in ('SYNTHETIC','REAL')
                or type(r['source']) is not str or not TOKEN.fullmatch(r['source'])
                or type(r['authority_sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',r['authority_sha256'])
                or not _valid_identity(r['directory']) or r['directory']!=_identity(p.stat())
                or type(r['files']) is not dict or set(r['files'])!={'owner.lock','journal.sqlite3','control.sqlite3'}):raise ValueError()
        for name,ident in r['files'].items():
            if not _valid_identity(ident) or ident!=_identity(_regular(p/name)):raise ValueError()
        # SQLite recovery sidecars imply an interrupted multi-file image. Do not
        # discard/replay them during admission; reviewed recovery is separate.
        before=_coherent(p)
        if r['domain']=='REAL':
            a,digest=_authority(transaction_authority)
            if (any(a[k]!=r[k] for k in ('identity','domain','source','directory'))
                    or a['file_identities']!=r['files'] or digest!=r['authority_sha256']):raise ValueError()
        elif transaction_authority is not None:raise ValueError()
        validate_pair(p)
        if _coherent(p)!=before:raise ValueError()
        return r
    except (OSError,ValueError,TypeError,KeyError,sqlite3.Error,JournalError):
        raise JournalError('TRANSACTION_STATE_UNADMITTED') from None

def _publish(p,identity,domain,source,authority_sha,*,expected=None):
    if type(identity) is not str or not TOKEN.fullmatch(identity) or type(source) is not str or not TOKEN.fullmatch(source):raise JournalError('INVALID_STATE_IDENTITY')
    import tempfile
    before=_coherent(p)
    if expected is not None and before!=expected:raise JournalError('STATE_AUTHORITY_MISMATCH')
    r=dict(schema='THS_TRANSACTION_STATE_V1',identity=identity,domain=domain,source=source,authority_sha256=authority_sha,
           directory=_identity(p.stat()),files={n:_identity(_regular(p/n)) for n in ('owner.lock','journal.sqlite3','control.sqlite3')})
    # A failed write/fsync never exposes a provenance receipt. link is atomic
    # no-replace; the unique staging file is the only file cleaned up here.
    fd,temporary=tempfile.mkstemp(prefix='.state-identity-',dir=p)
    try:
        data=canonical(r).encode();view=memoryview(data)
        while view:
            written=os.write(fd,view)
            if written<=0:raise OSError('STATE_RECEIPT_SHORT_WRITE')
            view=view[written:]
        os.fsync(fd)
        if _coherent(p)!=before:raise JournalError('STATE_AUTHORITY_MISMATCH')
        os.link(temporary,p/RECEIPT,follow_symlinks=False)
    finally:
        os.close(fd)
        os.unlink(temporary)
    fd=os.open(p,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)
    return r

def provision_synthetic(state_dir,*,identity,source):
    """Explicit fixture-only initial state; never usable as REAL authority."""
    from .journal import Journal,_PROVISION
    if type(identity) is not str or not TOKEN.fullmatch(identity) or type(source) is not str or not TOKEN.fullmatch(source):raise JournalError('INVALID_STATE_IDENTITY')
    p=Path(state_dir).absolute()
    if str(p)!=str(state_dir) or os.path.lexists(p):raise JournalError('FRESH_STATE_REQUIRED')
    # Do not recursively create arbitrary ancestors.
    for part in (*reversed(p.parent.parents),p.parent):
        if part.is_symlink():raise JournalError('UNSAFE_STATE_PATH')
    p.mkdir(mode=0o700)
    j=Journal(p,_provision=_PROVISION)
    j.close();validate_pair(p)
    return _publish(p,identity,'SYNTHETIC',source,hashlib.sha256(('SYNTHETIC:'+source).encode()).hexdigest())

def bind_existing(state_dir,*,authority_file,authority_sha256):
    """Explicit operator action; absent independent authority is a refusal.

    The supplied file must be private and its exact SHA must come from the
    operator's separate review. This API does not mint history coverage.
    """
    import fcntl
    p=_path(state_dir);a=Path(authority_file)
    authority,raw=_read(a,with_raw=True)
    if type(authority_sha256) is not str or not re.fullmatch('[0-9a-f]{64}',authority_sha256) or hashlib.sha256(raw).hexdigest()!=authority_sha256:raise JournalError('STATE_AUTHORITY_MISMATCH')
    authority,_digest=_authority((str(a),authority_sha256))
    if authority['domain']=='REAL' and (authority['directory']!=_identity(p.stat()) or authority['file_identities']!={n:_identity(_regular(p/n)) for n in ('owner.lock','journal.sqlite3','control.sqlite3')}):raise JournalError('STATE_AUTHORITY_MISMATCH')
    fd=os.open(p/'owner.lock',os.O_RDWR|os.O_NOFOLLOW)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        before=_coherent(p)
        if _metadata(os.fstat(fd))!=before['owner.lock']:raise JournalError('STATE_AUTHORITY_MISMATCH')
        validate_pair(p)
        for name,digest in authority['files'].items():
            if _stream_digest(p/name)!=digest:raise JournalError('STATE_AUTHORITY_MISMATCH')
        if _coherent(p)!=before:raise JournalError('STATE_AUTHORITY_MISMATCH')
        if _authority((str(a),authority_sha256))[0]!=authority:raise JournalError('STATE_AUTHORITY_MISMATCH')
        if authority['domain']=='REAL' and (authority['directory']!=_identity(p.stat()) or authority['file_identities']!={n:_identity(_regular(p/n)) for n in ('owner.lock','journal.sqlite3','control.sqlite3')}):raise JournalError('STATE_AUTHORITY_MISMATCH')
        return _publish(p,authority['identity'],authority['domain'],authority['source'],authority_sha256,expected=before)
    finally:os.close(fd)

def main(argv=None):
    import argparse
    a=argparse.ArgumentParser(description='Explicit offline state provisioning/adoption, not service startup')
    s=a.add_subparsers(dest='action',required=True)
    f=s.add_parser('provision-synthetic');f.add_argument('--state-dir',required=True);f.add_argument('--identity',required=True);f.add_argument('--source',required=True)
    b=s.add_parser('bind-existing');b.add_argument('--state-dir',required=True);b.add_argument('--authority-file',required=True);b.add_argument('--authority-sha256',required=True)
    q=a.parse_args(argv)
    if q.action=='provision-synthetic':r=provision_synthetic(q.state_dir,identity=q.identity,source=q.source)
    else:r=bind_existing(q.state_dir,authority_file=q.authority_file,authority_sha256=q.authority_sha256)
    print(canonical({'identity':r['identity'],'domain':r['domain'],'schema':r['schema']}));return 0
if __name__=='__main__':raise SystemExit(main())
