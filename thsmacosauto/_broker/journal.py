"""Private durable ownership journal. No repair/reset/replay paths."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading


class JournalError(Exception):
    pass

_PROVISION = object()  # only the explicit fixture provisioner supplies this


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


class Journal:
    VERSION = 2

    def __init__(self, state_dir, *, state_admission=None, transaction_identity=None, transaction_authority=None, _provision=None):
        # Validate existing coherent state before mkdir, lock creation, PRAGMA or
        # restart recovery. Ordinary startup is never a provisioning authority.
        self._lock = threading.RLock()
        self._control_lock = threading.RLock()
        self._closed = False
        self._close_state = 'OPEN'
        self.conn = None
        self._control_conn = None
        self._fd = None
        self._state_admission = None
        admitted = None
        if state_admission is None and _provision is not _PROVISION:
            from .state_lifecycle import admit
            admitted = admit(str(state_dir),transaction_identity,transaction_authority=transaction_authority)
        self._state_admission = state_admission
        self.state_domain = admitted['domain'] if admitted is not None else ('SHADOW' if state_admission is not None else 'SYNTHETIC')
        if state_admission is not None:
            from .shadow_admission import ShadowStateAdmission
            if type(state_admission) is not ShadowStateAdmission or state_admission.path != str(state_dir):
                raise JournalError('SHADOW_STATE_CAPABILITY_REQUIRED')
        path = Path(state_dir).absolute()
        # Existing components must not route a private journal through symlinks.
        for parent in [*reversed(path.parents), path]:
            if parent.exists() and parent.is_symlink():
                raise JournalError('UNSAFE_STATE_PATH')
        if admitted is None:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = path.stat()
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
            raise JournalError('STATE_DIRECTORY_NOT_PRIVATE')
        self.path = path / 'journal.sqlite3'
        try:
            if state_admission is not None:state_admission.check()
            lockpath = path/'owner.lock'
            if state_admission is None:
                self._fd = os.open(lockpath, os.O_RDWR|os.O_NOFOLLOW|(os.O_CREAT if admitted is None else 0), 0o600)
            else:
                self._fd = os.dup(state_admission.admit('owner.lock',create=not lockpath.exists()))
                state_admission.check()
            if stat.S_IMODE(os.fstat(self._fd).st_mode) != 0o600:
                raise JournalError('UNSAFE_OWNER_LOCK')
            fcntl.flock(self._fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
            if admitted is not None:
                # Revalidate after exclusive ownership, before writable SQLite.
                if admit(str(state_dir),transaction_identity,transaction_authority=transaction_authority) != admitted:
                    raise JournalError('TRANSACTION_STATE_UNADMITTED')
            existed = self.path.exists()
            if admitted is not None and not existed:
                raise JournalError('TRANSACTION_STATE_UNADMITTED')
            if self.path.is_symlink():
                raise JournalError('UNSAFE_JOURNAL')
            if existed:
                st = self.path.stat()
                if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
                    raise JournalError('JOURNAL_NOT_PRIVATE')
            else:
                fd = os.open(self.path, os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
                os.close(fd)
                if state_admission is not None:state_admission.admit('journal.sqlite3')
            connect = sqlite3.connect if state_admission is None else state_admission.connect
            self.conn = connect(self.path, check_same_thread=False, timeout=2, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            # Refuse legacy/unknown versions before any mode-changing pragma or recovery writes.
            if existed and self.conn.execute('PRAGMA user_version').fetchone()[0] != self.VERSION:
                raise JournalError('JOURNAL_SCHEMA_MISMATCH')
            self.conn.execute('PRAGMA synchronous=FULL')
            self.conn.execute('PRAGMA journal_mode=DELETE')
            self.conn.execute('PRAGMA busy_timeout=2000')
            if self.conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise JournalError('JOURNAL_INTEGRITY_FAILED')
            if existed:
                self._validate()
            else:
                self.conn.executescript('''BEGIN IMMEDIATE;
                    CREATE TABLE control (id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL, stopped INTEGER NOT NULL CHECK(stopped IN (0,1)));
                    INSERT INTO control VALUES (1,0,0);
                    CREATE TABLE requests (id TEXT PRIMARY KEY, source TEXT NOT NULL CHECK(source IN ('manual','auto')), digest TEXT NOT NULL, payload TEXT NOT NULL, scope TEXT, scope_digest TEXT, status TEXT NOT NULL, response TEXT NOT NULL, reservation TEXT NOT NULL DEFAULT '0');
                    CREATE TABLE events (seq INTEGER PRIMARY KEY, request_id TEXT NOT NULL, stage TEXT NOT NULL, elapsed_ms INTEGER NOT NULL);
                    CREATE TABLE auto_daily (account_scope TEXT NOT NULL, broker_day TEXT NOT NULL, symbol TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE REFERENCES requests(id), digest TEXT NOT NULL, PRIMARY KEY(account_scope,broker_day,symbol));
                    PRAGMA user_version=2;
                    COMMIT;''')
                dfd = os.open(path, os.O_RDONLY|os.O_DIRECTORY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            # Separate SQLite durability lane: stop never queues behind order writes.
            controlpath = path/'control.sqlite3'
            if controlpath.is_symlink() or (existed and not controlpath.exists()):
                raise JournalError('CONTROL_JOURNAL_UNAVAILABLE')
            if not existed:
                fd = os.open(controlpath,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                os.close(fd)
                if state_admission is not None:state_admission.admit('control.sqlite3')
            cst = controlpath.stat()
            if cst.st_uid != os.getuid() or stat.S_IMODE(cst.st_mode) != 0o600:
                raise JournalError('CONTROL_JOURNAL_NOT_PRIVATE')
            self._control_conn = connect(controlpath,check_same_thread=False,timeout=2,isolation_level=None)
            self._control_conn.execute('PRAGMA synchronous=FULL')
            self._control_conn.execute('PRAGMA journal_mode=DELETE')
            if not existed:
                self._control_conn.executescript('BEGIN IMMEDIATE; CREATE TABLE control(id INTEGER PRIMARY KEY CHECK(id=1),generation INTEGER NOT NULL,stopped INTEGER NOT NULL CHECK(stopped IN (0,1))); INSERT INTO control VALUES(1,0,0); PRAGMA user_version=1; COMMIT;')
                dfd = os.open(path,os.O_RDONLY|os.O_DIRECTORY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            if self._control_conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or self._control_conn.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise JournalError('CONTROL_JOURNAL_INVALID')
            tables = [r[0] for r in self._control_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            controls = self._control_conn.execute('SELECT id,generation,stopped FROM control').fetchall()
            if tables != ['control'] or len(controls) != 1 or controls[0][0] != 1 or type(controls[0][1]) is not int or controls[0][1] < 0 or controls[0][2] not in (0,1):
                raise JournalError('CONTROL_JOURNAL_INVALID')
            # Any in-flight durable ownership survives as UNKNOWN, never re-executes.
            if state_admission is not None:
                state_admission.check()
                if self.conn.execute("SELECT 1 FROM requests WHERE status IN ('CLAIMED','PREPARED','SUBMITTING','CONFIRMING') LIMIT 1").fetchone():
                    raise JournalError('SHADOW_PENDING_RECOVERY_REQUIRES_REVIEW')
            with self._lock:
                self.conn.execute('BEGIN IMMEDIATE')
                try:
                    rows = self.conn.execute("SELECT id FROM requests WHERE status IN ('CLAIMED','PREPARED','SUBMITTING','CONFIRMING')").fetchall()
                    for row in rows:
                        response = self.response(row['id'], 'UNKNOWN', 'RESTART_WITH_UNRESOLVED_OWNERSHIP')
                        self.conn.execute('UPDATE requests SET status=?,response=? WHERE id=?', ('UNKNOWN',canonical(response),row['id']))
                    self.conn.execute('COMMIT')
                except BaseException:
                    self.conn.execute('ROLLBACK')
                    raise
        except BaseException as exc:
            try:self.close()
            except BaseException as cleanup:
                import logging
                logging.getLogger(__name__).error('journal startup cleanup: %s',type(cleanup).__name__)
            if not isinstance(exc, Exception) or isinstance(exc, JournalError):
                raise
            raise JournalError('JOURNAL_UNAVAILABLE') from None

    def _validate(self):
        if self.conn.execute('PRAGMA user_version').fetchone()[0] != self.VERSION:
            raise JournalError('JOURNAL_SCHEMA_MISMATCH')
        required = {'control': ['id','generation','stopped'], 'requests':['id','source','digest','payload','scope','scope_digest','status','response','reservation'], 'events':['seq','request_id','stage','elapsed_ms'],
                    'auto_daily':['account_scope','broker_day','symbol','request_id','digest']}
        names = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if names != set(required):
            raise JournalError('JOURNAL_SCHEMA_MISMATCH')
        for name, cols in required.items():
            if [r[1] for r in self.conn.execute('PRAGMA table_info('+name+')')] != cols:
                raise JournalError('JOURNAL_SCHEMA_MISMATCH')
        daily_info = self.conn.execute('PRAGMA table_info(auto_daily)').fetchall()
        unique_columns = set()
        for index in self.conn.execute('PRAGMA index_list(auto_daily)'):
            if index['unique'] and not index['partial']:
                unique_columns.add(tuple(r['name'] for r in self.conn.execute('PRAGMA index_info('+index['name']+')')))
        if ([r['pk'] for r in daily_info] != [1,2,3,0,0]
                or not all(r['notnull'] for r in daily_info)
                or not {('account_scope','broker_day','symbol'),('request_id',)} <= unique_columns):
            raise JournalError('JOURNAL_SCHEMA_MISMATCH')
        controls = self.conn.execute('SELECT * FROM control').fetchall()
        if len(controls) != 1 or controls[0]['id'] != 1 or type(controls[0]['generation']) is not int or controls[0]['generation'] < 0 or controls[0]['stopped'] not in (0,1):
            raise JournalError('JOURNAL_CONTROL_INVALID')
        states = {'CLAIMED','PREPARED','SUBMITTING','CONFIRMING','UNKNOWN','BLOCKED','BROKER_ORDER_VISIBLE_UNFILLED','BROKER_ORDER_REJECTED','BROKER_ORDER_CANCELLED'}
        for row in self.conn.execute('SELECT * FROM requests'):
            from decimal import Decimal
            payload, response = json.loads(row['payload']), json.loads(row['response'])
            digest = hashlib.sha256(canonical(payload).encode()).hexdigest()
            reserve = Decimal(row['reservation'])
            scope_digest = hashlib.sha256(row['scope'].encode()).hexdigest() if row['scope'] is not None else None
            if (type(payload) is not dict or type(response) is not dict or row['source'] not in ('manual','auto')
                    or not reserve.is_finite() or reserve < 0 or scope_digest != row['scope_digest']
                    or digest != row['digest'] or row['status'] not in states or response.get('client_order_id') != row['id'] or response.get('status') != row['status']):
                raise JournalError('JOURNAL_RECORD_INVALID')
        claimed = set()
        for daily in self.conn.execute('SELECT * FROM auto_daily'):
            key = {k:daily[k] for k in ('account_scope','broker_day','symbol','request_id')}
            row = self.lookup(daily['request_id'])
            if row is None or daily['digest'] != self._daily_digest(key) or row['scope'] is None or row['status'] == 'CLAIMED':
                raise JournalError('JOURNAL_DAILY_INVALID')
            self._validate_daily_scope(key,row,row['scope'])
            claimed.add(row['id'])
        for row in self.conn.execute("SELECT * FROM requests WHERE source='auto'"):
            # Scoped automatic requests have crossed PREPARED; even BLOCKED retains ownership.
            if (row['scope'] is not None or row['status'] not in ('CLAIMED','BLOCKED','UNKNOWN')) and row['id'] not in claimed:
                raise JournalError('JOURNAL_DAILY_INVALID')

    @staticmethod
    def _daily_digest(key):
        return hashlib.sha256(canonical(key).encode()).hexdigest()

    @staticmethod
    def _validate_daily_scope(key, row, scope_text):
        from . import business_contracts as bc
        try:
            scope = json.loads(scope_text)
            intent = scope['intent']
            bc.token(key['account_scope']); bc.day(key['broker_day']); bc.symbol(key['symbol'])
            if (row['source'] != 'auto' or key['request_id'] != row['id']
                    or json.loads(row['payload'])['request_id'] != row['id']
                    or scope['account_scope'] != key['account_scope']
                    or scope['broker_trading_day'] != key['broker_day']
                    or intent['account_scope'] != key['account_scope'] or intent['symbol'] != key['symbol']
                    or intent['client_order_id'] != row['id'] or intent['source'] != 'auto' or intent['side'] != 'buy'
                    or intent['shareholder_scope'] != scope['shareholder_scope']):
                raise ValueError('DAILY_SCOPE_MISMATCH')
        except (ValueError,TypeError,KeyError):
            raise JournalError('JOURNAL_DAILY_INVALID') from None

    @staticmethod
    def response(request_id, status, reason, **fields):
        return dict(client_order_id=request_id,status=status,reason=reason,submitted=False,
                    retryable=False,broker_contract_id=None,broker_status=None,
                    independent_fill_verified=False,filled_quantity=None,buy_time=None,buy_price=None,**fields)

    def lookup(self, request_id):
        with self._lock:
            row = self.conn.execute('SELECT * FROM requests WHERE id=?',(request_id,)).fetchone()
            return dict(row) if row else None

    def claim(self, request_id, source, payload):
        text = canonical(payload)
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self._lock:
            old = self.lookup(request_id)
            if old:
                if old['source'] != source or old['digest'] != digest or old['payload'] != text:
                    raise JournalError('REQUEST_ID_CONFLICT')
                return json.loads(old['response'])
            if self.unresolved():
                raise JournalError('ACCOUNT_UNKNOWN')
            response = self.response(request_id,'CLAIMED','OWNER_CLAIMED')
            self.conn.execute('INSERT INTO requests(id,source,digest,payload,status,response) VALUES(?,?,?,?,?,?)',
                              (request_id,source,digest,text,'CLAIMED',canonical(response)))
            return None

    def unresolved(self):
        with self._lock:
            return self.conn.execute("SELECT 1 FROM requests WHERE status='UNKNOWN' LIMIT 1").fetchone() is not None

    def status_counts(self):
        with self._lock:
            total, unresolved, in_flight = self.conn.execute('''SELECT COUNT(*),
                COUNT(CASE WHEN status='UNKNOWN' THEN 1 END),
                COUNT(CASE WHEN status IN ('CLAIMED','PREPARED','SUBMITTING','CONFIRMING') THEN 1 END)
                FROM requests''').fetchone()
        return dict(request_count=total, unresolved_count=unresolved, in_flight_count=in_flight,
                    new_requests_blocked_by_unknown=unresolved > 0)

    def reservations(self):
        from decimal import Decimal
        with self._lock:
            return sum((Decimal(r[0]) for r in self.conn.execute('SELECT reservation FROM requests')), Decimal(0))

    def update(self, request_id, response, *, scope=None, reservation=None, auto_daily=None):
        with self._lock:
            self.conn.execute('BEGIN IMMEDIATE')
            try:
                row = self.lookup(request_id)
                if response['status'] == 'PREPARED' and row is not None and row['source'] == 'auto' and auto_daily is None:
                    raise JournalError('INVALID_AUTO_DAILY_CLAIM')
                if auto_daily is not None:
                    if response['status'] != 'PREPARED' or scope is None or row is None or row['status'] != 'CLAIMED':
                        raise JournalError('INVALID_AUTO_DAILY_CLAIM')
                    key = dict(auto_daily,request_id=request_id)
                    self._validate_daily_scope(key,row,scope)
                    if self.conn.execute('SELECT 1 FROM auto_daily WHERE account_scope=? AND broker_day=? AND symbol=?',
                            (key['account_scope'],key['broker_day'],key['symbol'])).fetchone():
                        raise JournalError('AUTO_STOCK_DAY_ALREADY_CLAIMED')
                    self.conn.execute('INSERT INTO auto_daily VALUES(?,?,?,?,?)',
                        (key['account_scope'],key['broker_day'],key['symbol'],request_id,self._daily_digest(key)))
                self.conn.execute('UPDATE requests SET status=?, response=?, scope=COALESCE(?,scope), scope_digest=COALESCE(?,scope_digest), reservation=COALESCE(?,reservation) WHERE id=?',
                                  (response['status'],canonical(response),scope,hashlib.sha256(scope.encode()).hexdigest() if scope is not None else None,reservation,request_id))
                self.conn.execute('COMMIT')
            except BaseException:
                self.conn.execute('ROLLBACK')
                raise

    def control(self):
        with self._control_lock:
            r = self._control_conn.execute('SELECT generation,stopped FROM control WHERE id=1').fetchone()
            return {'generation':r[0], 'stopped':bool(r[1])}

    def set_control(self, generation, stopped):
        with self._control_lock:
            self._control_conn.execute('UPDATE control SET generation=?, stopped=? WHERE id=1',(generation,int(stopped)))

    def event(self, request_id, stage, elapsed_ms):
        with self._lock:
            self.conn.execute('INSERT INTO events(request_id,stage,elapsed_ms) VALUES(?,?,?)',(request_id,stage,elapsed_ms))

    def close(self):
        with self._lock:
            if self._close_state=='COMPLETE':return
            if self._close_state!='OPEN':raise RuntimeError('JOURNAL_TEARDOWN_INCOMPLETE')
            self._close_state='FAILED'
            self._closed=True
            # Consume references before close; never retry a recycled descriptor.
            # The worker is already drained. Attempt every ordinary resource,
            # report failure and require process restart rather than interpreter reuse.
            failures=[]
            for name in ('conn','_control_conn','_fd','_state_admission'):
                resource=getattr(self,name)
                setattr(self,name,None)
                if resource is not None:
                    try:
                        if name=='_fd':os.close(resource)
                        else:resource.close()
                    except BaseException as error:failures.append(error)
            if failures:raise failures[0]
            self._close_state='COMPLETE'
