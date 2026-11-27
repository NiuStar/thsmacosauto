"""Single-owner Python transaction engine; no network or GUI during construction."""
from concurrent.futures import Future
from contextlib import contextmanager, nullcontext

from decimal import Decimal
import json
import queue
import re
import threading
import time
from types import MappingProxyType
from urllib.parse import parse_qs, urlsplit

from . import business_contracts as bc
from . import readonly_budget as rb
from .buy_only_policy import size_buy
from .journal import Journal, JournalError, canonical
from .engine_telemetry import DeferredTrace, EngineTelemetry, failure_code


class ServiceError(Exception):
    def __init__(self, code: str, http_status: int = 409):
        self.code, self.http_status = code, http_status
        super().__init__(code)

    @property
    def public_metadata(self):
        rid = getattr(self, 'diagnostic_request_id', None)
        return {'diagnostic_request_id':rid} if rid is not None else {}


def _attach_diagnostic(error, request_id, status=None):
    # Legacy trusted Python convenience only, never the HTTP authority. A
    # read-only exception must not mask the primary error or its trace admission.
    if isinstance(error, ServiceError):
        try:
            error.diagnostic_request_id = request_id
            if status is not None:
                error.diagnostic_status = status
        except Exception:
            pass


def stage(trace, name):
    return trace.span(name) if trace is not None else nullcontext()


def immutable(value):
    if isinstance(value, dict):
        return MappingProxyType({k: immutable(v) for k,v in value.items()})
    if isinstance(value, list):
        return tuple(immutable(v) for v in value)
    return value


def owned(value):
    # JSON boundary ensures no caller-controlled mutable object remains shared.
    return json.loads(canonical(value))


def identifier(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value):
        raise ServiceError('INVALID_REQUEST_ID',400)
    return value


# Fixed product-only traceback vocabulary. No exception text, args, source lines,
# arbitrary paths, locals (except the validated read kind), or chained exceptions.
_READ_FAILURE_FRAMES = {
    'business_contracts.py': frozenset('__post_init__ aliases automatic_sell_capability blocked day decimal_cell freeze_baseline fresh normalize_available_cash normalize_native_positions normalize_orders normalize_positions position_scope_check reconcile shares size_automatic_buy state_verdict symbol table times token validate_manual_sell'.split()),
    'engine.py': frozenset('__init__ _attach_diagnostic _before_press _budget_settle _check_auto _diagnostic_status _dispatch _get_backend _http_diagnostics _loop _loop_budgeted _manual _press _read _require_execution _resolve_auto _run _run_budgeted _set_control _snapshot _transaction _validate_receipt _verify_form close dispatch event identifier immutable owned public_metadata record_http_rejection save stage telemetry_state unknown'.split()),
    'native_backend.py': frozenset('__getattr__ __init__ _admit _before_rpc _binding _block _cash _check_holders _complete _connect _day _discovered_form _fill_scope _form _form_fields _get _identity _intent _matrix _nodes _order _owned_effect _owner_failure _owner_proof _passive_effect _positions _press _profile _proof _qualified _read_form _restore_preserving_primary _scope_config _scoped_position_rows _selector _sheet _switch_page _synthetic_cash _table _thread _verify_fixed_rows _verify_framework actions app_identity confirmation dispatch front get guarded is_settable keys locate observe perform plain prepare press_confirm press_submit press_with_permit process_identity read read_budgeted reconcile refs restore root sample set set_before_press'.split()),
    'holdings.py': frozenset({'read', 'scoped_position_rows', 'table_report'}),
    'holdings_refresh.py': frozenset({'discover', 'dispatch', '_refresh_observe', '_refresh_scan', 'permit'}),
    'login_status.py': frozenset({'_probe', 'sample', 'status', 'current_logged', 'logged_in'}),
    'readonly_budget.py': frozenset('__getattr__ __init__ __post_init__ _bounded _clock accumulate at bind check cost cost_receipt current effect event mark_rpc receipt remaining_seconds require set'.split()),
    'pymacauto/core.py': frozenset('__init__ _get _json_value _matches _path_step _positive _required_get _resolve_phase _selector _selector_plan _value_equal _walk _writable automation child child_window children click connect control descendants dump_tree exists explain export get get_value info inspect inspect_children is_trusted labelled_field list_apps locator_path perform read_table resolve session set_edit_text set_value wait window windows'.split()),
    'pymacauto/query.py': frozenset('__init__ find find_all walk window_scope'.split()),
    'pymacauto/pages.py': frozenset('__init__ _detect_phase check configuration describe detect direct_matches from_config resolve scope switch table_headers wait'.split()),
    'pymacauto/tables.py': frozenset('check get many optional read_table sample sequence'.split()),
    'pymacauto/native.py': frozenset('__eq__ __hash__ __init__ _check _geometry _name _names _normalize actions app_metadata apps attributes get get_many is_settable key perform process_identity root set trusted'.split()),
    'pymacauto/automation.py': frozenset('__enter__ __exit__ __init__ _check_open check click close condition control ensure fill page read read_table'.split()),
    'pymacauto/ths_query.py': frozenset('recognize_query_view'.split()),
}


def _budget_backend_failure(exc, budget):
    import os
    root=os.path.dirname(os.path.abspath(__file__))
    allowed={os.path.join(root,name) if '/' not in name else
             os.path.join(root,'..','_ax',name.rsplit('/',1)[-1]): (name.rsplit('/',1)[-1],functions)
             for name,functions in _READ_FAILURE_FRAMES.items()}
    allowed={os.path.normpath(path):value for path,value in allowed.items()}
    exception='UNKNOWN'
    for cls in (ValueError,TypeError,KeyError,IndexError,AttributeError,RuntimeError,OSError,TimeoutError):
        if type(exc) is cls:exception=cls.__name__;break
    frames=[];kind='UNKNOWN';tb=exc.__traceback__
    for _ in range(64):
        if tb is None:break
        code=tb.tb_frame.f_code
        entry=allowed.get(os.path.normpath(code.co_filename))
        if entry is not None and code.co_name in entry[1]:
            frames.append({'file':entry[0],'function':code.co_name,'line':min(1_000_000,max(0,tb.tb_lineno))})
            if entry[0]=='engine.py' and code.co_name=='_read':
                candidate=tb.tb_frame.f_locals.get('kind')
                if type(candidate) is str and candidate in Engine.READS.values():kind=candidate
        tb=tb.tb_next
    receipt=budget.receipt()
    phases=('WORK','RESTORE','FINALIZE')
    phase=receipt.get('phase')
    events=[]
    for event in receipt.get('events',())[:16]:
        if type(event) in (list,tuple) and len(event)==3:
            p,_,reason=event
            if type(p) is str and p in phases and type(reason) is str and reason in ('OWNER_BOUND','FACTORY_RETURNED','RESTORE_FAILED','TERMINAL'):
                events.append({'phase':p,'event':reason})
    return {'schema':'READ_BACKEND_FAILURE_V1','code':'BACKEND_UNAVAILABLE',
            'kind':kind,'exception':exception,'frames':frames[-8:],
            'phase':phase if type(phase) is str and phase in phases else 'UNKNOWN',
            'events':events,
            **{key:receipt.get(key) is True for key in
               ('dispatch_attempted','restore_required','restored_verified','poisoned','terminal_late')}}


class Engine:
    READS = {'/v1/real/native-current-panel':'current', '/v1/real/positions':'positions',
             '/v1/real/positions-scope-diagnostic':'positions-scope-diagnostic',
             '/v1/real/positions-panel':'positions-panel',
             '/v1/real/orders':'orders', '/v1/real/trades':'trades', '/v1/real/account-observation':'account',
             '/v1/real/form-observation':'form',
             '/v1/real/form-discovery':'form-discovery'}
    MANUAL_FIELDS = frozenset(('client_order_id','mode','shareholder_scope','order_type','symbol','side','price','quantity'))

    def __init__(self, state_dir, backend_factory, *, clock_ms=None, signal_provider=None,
                 telemetry_writer=None, runtime_mode=None, state_admission=None,
                 operation_mode='READ_ONLY', transaction_identity=None, transaction_authority=None):
        # Two-stage Service assignment can always drain this object, including
        # validation/telemetry failures before persistent ownership begins.
        self._close_lock=threading.RLock()
        self._lifecycle=threading.Lock()
        self._owner=None
        self._worker_drained=True
        self._close_state='OPEN'
        self._closed=False
        self.journal=None
        if runtime_mode not in (None, 'SHADOW'):
            raise ServiceError('REAL_EXECUTION_NOT_ADMITTED')
        self.__runtime_mode = runtime_mode
        if runtime_mode == 'SHADOW' and state_admission is None:
            raise ServiceError('SHADOW_STATE_CAPABILITY_REQUIRED')
        if state_admission is not None and runtime_mode != 'SHADOW':
            raise ServiceError('SHADOW_STATE_CAPABILITY_REQUIRED')
        if type(operation_mode) is not str or operation_mode not in ('READ_ONLY','TRANSACTIONAL'):
            raise ServiceError('INVALID_OPERATION_MODE',503)
        if runtime_mode == 'SHADOW' and operation_mode != 'READ_ONLY':
            raise ServiceError('SHADOW_EXECUTION_FORBIDDEN')
        self.__operation_mode = operation_mode
        if runtime_mode is None and state_admission is not None:
            raise ServiceError('SHADOW_STATE_CAPABILITY_REQUIRED')
        if operation_mode == 'READ_ONLY' and (transaction_identity is not None or transaction_authority is not None):
            raise ServiceError('TRANSACTION_MODE_REQUIRED')
        self._telemetry = EngineTelemetry(telemetry_writer) if telemetry_writer is not None else None
        self.journal = None
        self._factory, self._signal_provider = backend_factory, signal_provider
        self._clock = clock_ms or (lambda: time.time_ns()//1_000_000)
        self._slot = threading.Lock()
        self._lifecycle = threading.Lock()
        self._control_serial = threading.Lock()
        self._press_lock = threading.Lock()
        self._control = self.journal.control() if self.journal is not None else None
        self._control_pending = False
        self._queue = queue.Queue(maxsize=1)
        self._owner = None
        self._backend = None
        self._closed = False  # admission fence, not a teardown acknowledgement
        self._close_lock = threading.RLock()
        self._close_state = 'OPEN'
        self._fatal = False
        self._active = None
        self._press_held = False
        self._press_called = False
        self._legacy_compat_enabled = False  # set only by trusted service composition
        self._http_local = threading.local()
        try:
            if runtime_mode == 'SHADOW' or operation_mode == 'TRANSACTIONAL':
                # Assign the owner before any constructor/control code can fail.
                self.journal = Journal.__new__(Journal)
                Journal.__init__(self.journal,state_dir,state_admission=state_admission,
                                 transaction_identity=transaction_identity,transaction_authority=transaction_authority)
                self._control = self.journal.control()
        except BaseException as primary:
            try:self.close()
            except BaseException as cleanup:
                import logging
                logging.getLogger(__name__).error('engine startup cleanup: %s',type(cleanup).__name__)
            if isinstance(primary,JournalError):raise ServiceError(str(primary),503) from None
            raise

    @contextmanager
    def _http_diagnostics(self):
        """One trusted transport call, not exception attributes or client input.

        Keep the original exception object, including read-only exceptions. The
        worker returns its receipt through the bounded operation handoff below.
        """
        previous = getattr(self._http_local, 'metadata', None)
        metadata = {}
        self._http_local.metadata = metadata
        try:
            yield metadata
        finally:
            if previous is None:
                del self._http_local.metadata
            else:
                self._http_local.metadata = previous

    def dispatch(self, method: str, path: str, payload: dict | None = None) -> dict:
        # Controls/status remain independent. No telemetry waits/IO in this path.
        route = urlsplit(path).path
        if method == 'POST' and path == '/v1/operator/trade-page':
            self._require_readonly_operator()
            if (type(payload) is not dict or set(payload) != {'action'}
                    or type(payload['action']) is not str or payload['action'] not in ('login-status','inspect','switch','login-inspect','login-open','ensure-login','retry-login','fresh-login')):
                raise ServiceError('INVALID_REQUEST',400)
            action = payload['action']
            return self._run_operator(lambda backend, budget: backend.trade_page(action, budget=budget))
        if self.__runtime_mode is None and self.__operation_mode == 'READ_ONLY':
            if method != 'GET':
                raise ServiceError('TRANSACTION_MODE_REQUIRED',409)
            if route in ('/v1/real/orders/status','/v07/legacy/orders/status'):
                raise ServiceError('TRANSACTION_STATE_UNAVAILABLE',503)
        read_budget = rb.ReadBudget(read_only=self.__operation_mode == 'READ_ONLY') if method == 'GET' and route in self.READS else None
        tracked = method == 'GET' and route in self.READS
        trace = DeferredTrace(self._clock()) if self._telemetry and tracked else None
        if trace is not None:
            self._telemetry.state()  # bounded memory snapshot, before GUI admission
        handed_off = [False, {}]
        try:
            # close's permanent latch precedes interruptible lifecycle acquisition.
            if (self._closed or self._close_state != 'OPEN') and threading.current_thread() is not self._owner:
                raise ServiceError('ENGINE_CLOSED',503)
            return self._dispatch(method, path, payload, trace, handed_off, read_budget)
        except BaseException as exc:
            if read_budget is not None and handed_off[0]:
                # Worker already owns terminal diagnostics. Never re-enter fallible
                # trace properties or replace its settled primary during projection.
                try:
                    metadata=getattr(self._http_local,'metadata',None)
                    if metadata is not None:metadata.update(handed_off[1])
                except BaseException:pass
                raise
            if trace is not None:
                _attach_diagnostic(exc, trace.request_id)
                if not handed_off[0]:
                    admission = self._telemetry.submit(trace, error=exc)
                    handed_off[1].update(diagnostic_request_id=trace.request_id,
                                         diagnostic_status=self._diagnostic_status(admission))
                    _attach_diagnostic(exc, trace.request_id, self._diagnostic_status(admission))
                metadata = getattr(self._http_local, 'metadata', None)
                if metadata is not None:
                    metadata.update(handed_off[1])
            raise

    @staticmethod
    def _diagnostic_status(admission):
        return 'ACCEPTED' if admission == 'ACCEPTED' else 'NOT_RECORDED'

    def record_http_rejection(self):
        """Trusted HTTP boundary only: no caller payload, no GUI slot or IO wait."""
        if self._telemetry is None:
            return {}
        trace = DeferredTrace(self._clock())
        admission = self._telemetry.submit(trace, result={'status':'BLOCKED'})
        return {'diagnostic_request_id':trace.request_id,
                'diagnostic_status':self._diagnostic_status(admission)}

    def telemetry_state(self):
        return self._telemetry.state() if self._telemetry else {'enabled':False, 'degraded':False}

    def _dispatch(self, method, path, payload, trace, handed_off, read_budget=None):
        parsed = urlsplit(path)
        route = parsed.path
        if self.__runtime_mode == 'SHADOW':
            if method == 'POST' and route == '/v1/shadow/evidence' and not parsed.query:
                return self._signal_provider.preflight(payload)
            if not (method == 'GET' and route in ('/v1/health', '/v1/capabilities', '/v1/real/orders/status')
                    or method == 'POST' and route == '/v1/control/stop'):
                self._require_execution()
        if method == 'GET' and route == '/v1/health' and not parsed.query:
            telemetry = self.telemetry_state()
            if self.journal is None:
                return {'status':'ok' if not self._fatal and not telemetry['degraded'] else 'degraded',
                        'engine':'python-pymacauto','execution_enabled':False,
                        'operation_mode':self.__operation_mode,'transaction_state':'UNADMITTED',
                        'transaction_domain':None,
                        'account_unknown':None,'history_verified':False,'control':None,'reservations':None,
                        **({'telemetry':telemetry} if self._telemetry else {})}
            return {'status':'ok' if not self._fatal and not telemetry['degraded'] else 'degraded', 'execution_enabled':False,
                    'operation_mode':'SHADOW' if self.__runtime_mode == 'SHADOW' else self.__operation_mode,
                    'transaction_state':'SHADOW_ADMITTED' if self.__runtime_mode == 'SHADOW' else 'ADMITTED',
                    'history_verified':self.__runtime_mode != 'SHADOW',
                    'transaction_domain':self.journal.state_domain,
                    'engine':'python-pymacauto', 'account_unknown':self.journal.unresolved(), **self.journal.control(),
                    **({'telemetry':telemetry} if self._telemetry else {})}
        if method == 'GET' and route == '/v1/capabilities' and not parsed.query:
            if self.__runtime_mode == 'SHADOW':
                return {'runtime_mode':'SHADOW', 'executable':False, 'execution_enabled':False,
                        'source_scope':self._signal_provider.scope,
                        'source_status':self._signal_provider.status,
                        'readiness':'NOT_CHECKED', 'native_backend':'FORBIDDEN'}
            return {'status':'IMPLEMENTED_SUPPORT_ONLY',
                    'operation_mode':self.__operation_mode,
                    'transaction_state':'ADMITTED' if self.journal is not None else 'UNADMITTED',
                    'transaction_domain':self.journal.state_domain if self.journal is not None else None,
                    'implemented_support':{'manual_limit_buy':True, 'manual_limit_sell':True,
                        'automatic_buy':True, 'automatic_sell':False, 'cancel':False},
                    'trusted_signal_provider_configured':self._signal_provider is not None,
                    'backend_qualification':'NOT_CHECKED', 'effective_readiness':False,
                    'execution_enabled':False}
        if method == 'GET' and route == '/v1/real/orders/status':
            query = parse_qs(parsed.query,keep_blank_values=True)
            if set(query) != {'client_order_id'} or len(query['client_order_id']) != 1:
                raise ServiceError('INVALID_STATUS_QUERY',400)
            row = self.journal.lookup(identifier(query['client_order_id'][0]))
            if not row:
                raise ServiceError('ORDER_NOT_FOUND',404)
            return json.loads(row['response'])
        if parsed.query:
            raise ServiceError('UNKNOWN_ROUTE',404)
        if method == 'POST' and route in ('/v1/control/stop','/v1/control/resume'):
            if payload not in (None,{}):
                raise ServiceError('INVALID_CONTROL_PAYLOAD',400)
            return self._set_control(route.endswith('/stop'))
        if method == 'GET' and route in self.READS:
            return self._run(lambda:self._read(self.READS[route],trace,read_budget),trace,handed_off,read_budget)
        if method == 'POST' and route == '/v1/real/orders/submit':
            normalized = self._manual(payload)
            return self._run(lambda:self._transaction('manual',normalized,trace),trace,handed_off)
        if method == 'POST' and route == '/v1/auto/buy':
            if type(payload) is not dict or set(payload) != {'request_id','signal_id'}:
                raise ServiceError('INVALID_AUTO_PAYLOAD',400)
            request = {'request_id':identifier(payload['request_id']), 'signal_id':identifier(payload['signal_id'])}
            return self._run(lambda:self._transaction('auto',request,trace),trace,handed_off)
        raise ServiceError('UNKNOWN_ROUTE',404)

    def _manual(self, payload):
        if type(payload) is not dict or set(payload) != self.MANUAL_FIELDS:
            raise ServiceError('INVALID_MANUAL_FIELDS',400)
        out = owned(payload)
        identifier(out['client_order_id'])
        if out['mode'] != 'real' or out['order_type'] != 'limit' or out['side'] not in ('buy','sell'):
            raise ServiceError('EXPLICIT_MANUAL_LIMIT_REQUIRED',400)
        scope = out['shareholder_scope']
        if type(scope) is not str or not scope or len(scope) > 256 or scope != scope.strip():
            raise ServiceError('INVALID_SHAREHOLDER_SCOPE',400)
        try:
            bc.symbol(out['symbol'])
            price = bc.decimal_cell(out['price'])
            bc.OrderIntent(bc.AccountBinding('SYNTHETIC','validation','validation'),'20200101',
                           out['symbol'],out['side'].upper(),price,out['quantity'])
        except (ValueError,TypeError,KeyError):
            raise ServiceError('INVALID_LIMIT_ORDER',400) from None
        out['price'] = format(price,'.2f')
        return out

    def _run(self, operation, trace=None, handed_off=None, read_budget=None):
        if read_budget is not None:
            return self._run_budgeted(operation,trace,handed_off,read_budget)
        with self._lifecycle:
            with stage(trace,'QUEUE'):
                if self._closed or self._close_state != 'OPEN':
                    raise ServiceError('ENGINE_CLOSED',503)
                if not self._slot.acquire(blocking=False):
                    raise ServiceError('BUSY',409)
                future = Future()
                if self._owner is None:
                    self._owner = threading.Thread(target=self._loop,name='ths-gui-owner',daemon=False)
                    self._owner.start()
                # End QUEUE before publishing to avoid concurrent collector access.
            if handed_off is not None:
                handed_off[0] = True
            self._queue.put_nowait((future,operation,trace,handed_off))
        # Future is never exposed or canceled by an HTTP waiter.
        return future.result()

    def _loop(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            if isinstance(item, dict):
                self._loop_budgeted(item)
                continue
            future, operation, trace, handed_off = item
            result = error = None
            try:
                result = operation()
            except BaseException as exc:
                error = exc if isinstance(exc,ServiceError) or not isinstance(exc,Exception) else ServiceError('BACKEND_UNAVAILABLE',503)
                if not isinstance(exc,Exception):self._closed=True;self._fatal=True
            finally:
                self._slot.release()
            # Slot is free, but no next operation executes until this local trace
            # is frozen/submitted. Never consult an engine-global active trace.
            if trace is not None:
                admission = self._telemetry.submit(trace,result,error)
                diagnostic_status = self._diagnostic_status(admission)
                if handed_off is not None:
                    handed_off[1].update(diagnostic_request_id=trace.request_id,
                                         diagnostic_status=diagnostic_status)
                if error is not None:
                    _attach_diagnostic(error, trace.request_id, diagnostic_status)
                elif type(result) is dict:
                    result = {**result,'diagnostic_request_id':trace.request_id,
                              'diagnostic_status':diagnostic_status}
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(result)

    def _require_readonly_operator(self):
        if self.__runtime_mode is not None or self.__operation_mode != 'READ_ONLY':
            raise ServiceError('TRANSACTION_MODE_REQUIRED', 409)

    def _run_operator(self, operation):
        self._require_readonly_operator()
        budget = rb.ReadBudget(read_only=True)
        return self._run_budgeted(lambda: self._operator_call(operation, budget), None, [False, {}], budget)

    def _operator_call(self, operation, budget):
        with rb.bind(budget), rb.cost('READ_TOTAL'):
            budget.check()
            backend = self._get_backend()
            budget.event('FACTORY_RETURNED')
            budget.check()
            result = operation(backend, budget)
            budget.check()
            return result

    def _read(self, kind, trace, read_budget):
        try:
            with rb.bind(read_budget), rb.cost('READ_TOTAL'):
                read_budget.check()
                with stage(trace,'BACKEND'):
                    backend = self._get_backend()
                read_budget.event('FACTORY_RETURNED')
                read_budget.check()
                seam = getattr(backend,'read_budgeted',None)
                if not callable(seam):
                    raise ServiceError('READ_BUDGET_UNSUPPORTED',503)
                with stage(trace,'OBSERVE'):
                    result = seam(kind,budget=read_budget)
                    read_budget.check()
                    if trace is not None:
                        trace.observe_source(result)
                read_budget.check()
                return result
        except rb.BudgetError as exc:
            raise ServiceError(exc.code,503) from None

    def _budget_settle(self, task):
        # Caller owns pre-handoff failures; worker owns RUNNING drain. Lock held.
        b=task['budget']
        try:b.event('TERMINAL')
        except BaseException:
            if task['error'] is None:task['error']=ServiceError('BACKEND_UNAVAILABLE',503)
        try:
            b.check('FINALIZE')
        except rb.BudgetError:
            b.set(terminal_late=True)
            if task['error'] is None:task['error']=ServiceError('READ_BUDGET_EXCEEDED',503)
        try:
            task['receipt']=b.receipt()
        except BaseException:
            if task['error'] is None:task['error']=ServiceError('BACKEND_UNAVAILABLE',503)
        try:
            if task['error'] is None:task['future'].set_result(task['result'])
            else:task['future'].set_exception(task['error'])
        finally:
            try:
                if task['slot_held']:
                    task['slot_held']=False
                    self._slot.release()
            finally:
                task['state']='SETTLED'
                task['terminal_stamp']=rb.monotonic_ns()
                if task['terminal_stamp']>=b.absolute_deadline_ns:b.set(terminal_late=True)
                if task.get('receipt') is not None:task['receipt']['terminal_late']=b.terminal_late
                self._last_read_receipt=task.get('receipt')
                task['drained'].set()
                # Independent of optional telemetry; fixed fields only, no error
                # text, account values, selectors, paths or owner payloads.
                if isinstance(task['error'],ServiceError) and task['error'].code=='READ_BUDGET_EXCEEDED':
                    try:
                        import sys
                        receipt=task.get('receipt') or {}
                        record={'schema':'READ_BUDGET_FAILURE_V1','first_exceeded':receipt.get('first_exceeded')}
                        for key in ('dispatch_attempted','restore_required','restored_verified','poisoned','terminal_late'):
                            record[key]=receipt.get(key) is True
                        print(json.dumps(record,sort_keys=True),file=sys.stderr,flush=True)
                    except BaseException:
                        pass

                if task.get('backend_failure') is not None:
                    try:
                        import sys
                        print(json.dumps(task['backend_failure'],sort_keys=True),file=sys.stderr,flush=True)
                    except BaseException:
                        pass

                if task['error'] is not None:
                    try:
                        import sys
                        print(json.dumps(b.cost_receipt(),sort_keys=True),file=sys.stderr,flush=True)
                    except BaseException:
                        pass

    def _run_budgeted(self, operation, trace, handed_off, budget):
        task=dict(budget=budget,future=Future(),drained=threading.Event(),slot_held=False,state='NEW',error=None,
                  result=None,operation=operation,trace=trace,handed_off=handed_off)
        if not self._lifecycle.acquire(blocking=False):raise ServiceError('BUSY',409)
        try:
            try:
                if self._closed or self._close_state!='OPEN':raise ServiceError('ENGINE_CLOSED',503)
                budget.check()
                if not self._slot.acquire(blocking=False):raise ServiceError('BUSY',409)
                task.update(slot_held=True,state='SLOT_HELD')
                budget.check()
                if self._owner is None:
                    owner=threading.Thread(target=self._loop,name='ths-gui-owner',daemon=False)
                    owner.start()
                    self._owner=owner
                self._queue.put_nowait(task)
                task['state']='PUBLISHED'
                if handed_off is not None:handed_off[0]=True
            except BaseException as exc:
                task['error']=ServiceError(exc.code,503) if isinstance(exc,rb.BudgetError) else exc
                task['state']='ABORTED'
                self._budget_settle(task)
        finally:self._lifecycle.release()
        primary=None
        try:
            return task['future'].result()
        except BaseException as exc:
            primary=exc
            # A waiter interruption cannot release an executing slot.
            task['drained'].wait()
            raise
        finally:
            with self._lifecycle:
                late=budget.terminal_late or rb.monotonic_ns()>=budget.absolute_deadline_ns
                if late:budget.set(terminal_late=True)
                if primary is None and late:raise ServiceError('READ_BUDGET_EXCEEDED',503)

    def _loop_budgeted(self,task):
        with self._lifecycle:
            if task['state']!='PUBLISHED':return
            task['state']='RUNNING'
        trace=task['trace']
        try:
            task['budget'].check()
            task['result']=task['operation']()
        except BaseException as exc:
            task['error']=(ServiceError(exc.code,503) if isinstance(exc,rb.BudgetError) else
                           exc if isinstance(exc,ServiceError) or not isinstance(exc,Exception) else ServiceError('BACKEND_UNAVAILABLE',503))
            if not isinstance(exc,Exception):self._closed=True;self._fatal=True
            if isinstance(exc,Exception) and not isinstance(exc,(ServiceError,rb.BudgetError)):
                try:task['backend_failure']=_budget_backend_failure(exc,task['budget'])
                except BaseException:pass
        finally:
            with self._lifecycle:task['state']='DRAINED'
            try:
                if trace is not None:
                    admission=self._telemetry.submit(trace,task['result'],task['error'])
                    status=self._diagnostic_status(admission)
                    if task['handed_off'] is not None:
                        task['handed_off'][1].update(diagnostic_request_id=trace.request_id,diagnostic_status=status)
                    if task['error'] is not None:_attach_diagnostic(task['error'],trace.request_id,status)
                    elif type(task['result']) is dict:task['result']={**task['result'],'diagnostic_request_id':trace.request_id,'diagnostic_status':status}
            except BaseException:
                if task['error'] is None:task['error']=ServiceError('BACKEND_UNAVAILABLE',503)
            finally:
                with self._lifecycle:self._budget_settle(task)

    def _require_execution(self):
        if self.__runtime_mode == 'SHADOW':
            raise ServiceError('SHADOW_EXECUTION_FORBIDDEN')
        if self.__operation_mode != 'TRANSACTIONAL' or self.journal is None:
            raise ServiceError('TRANSACTION_MODE_REQUIRED')

    def _get_backend(self):
        if self.__runtime_mode == 'SHADOW':
            raise ServiceError('SHADOW_EXECUTION_FORBIDDEN')
        if self._backend is None:
            self._backend = self._factory()
        return self._backend

    def _set_control(self, stopped):
        if stopped is not True:
            self._require_execution()
        # Set in-memory fence before slow FULL-sync. No journal lock while holding press lock.
        with self._control_serial:
            with self._press_lock:
                self._control_pending = True
                generation = self._control['generation']+1
            try:
                self.journal.set_control(generation,stopped)
            except Exception:
                with self._press_lock:
                    self._fatal = True
                    self._control = {'generation':generation,'stopped':True}
                raise ServiceError('CONTROL_PERSISTENCE_FAILED',503) from None
            with self._press_lock:
                self._control = {'generation':generation,'stopped':stopped}
                self._control_pending = False
            return {'status':'STOPPED' if stopped else 'RESUMED', **self._control}

    def _check_auto(self, generation):
        if self._fatal:
            raise ServiceError('ENGINE_FENCED')
        if self._active and self._active['source'] == 'auto' and (
                self._control_pending or self._control['stopped'] or self._control['generation'] != generation):
            raise ServiceError('AUTOMATIC_STOPPED')

    def _before_press(self):
        self._require_execution()
        if threading.current_thread() is not self._owner or self._active is None or self._press_called:
            raise ServiceError('INVALID_PRESS_PERMIT')
        self._press_lock.acquire()
        self._press_held = True
        try:
            self._check_auto(self._active['generation'])
            if not bc.fresh(self._active['start'],self._active['end'],self._clock()):
                raise ServiceError('STALE_CAPTURE')
            if self._active['state'] not in ('SUBMITTING','CONFIRMING'):
                raise ServiceError('DURABLE_PRESS_STATE_REQUIRED')
            self._press_called = True
            # Lock is released by _press only after the native final action returns.
            return True
        except BaseException:
            self._press_held = False
            self._press_lock.release()
            raise

    def _press(self, method, intent):
        self._require_execution()
        self._press_called = False
        try:
            getattr(self._backend,method)(intent)
            if not self._press_called:
                raise ServiceError('BACKEND_PRESS_GUARD_NOT_CALLED')
        finally:
            if self._press_held:
                self._press_held = False
                self._press_lock.release()

    def _snapshot(self, raw):
        if type(raw) is not dict or type(raw.get('real_identity_count')) is not int or raw.get('real_identity_count') != 1 or raw.get('simulation_marker') is not False:
            raise ServiceError('REAL_IDENTITY_UNPROVEN')
        scope = bc.token(raw['account_scope'])
        bc.token(raw['shareholder_scope'])
        binding = bc.AccountBinding('REAL','TRUSTED_NATIVE_BACKEND',scope)
        day = bc.day(raw['broker_trading_day'])
        start,end = raw['captured_start_ms'],raw['captured_end_ms']
        if not bc.fresh(start,end,self._clock()):
            raise ServiceError('STALE_CAPTURE')
        if raw.get('orders_complete') is not True:
            raise ServiceError('INCOMPLETE_ORDER_SCOPE')
        if type(raw.get('original_panel')) is not str or not raw['original_panel']:
            raise ServiceError('ORIGINAL_PANEL_UNPROVEN')
        cash = bc.decimal_cell(raw['cash_available'])
        positions = tuple(bc.Position(p['symbol'],p['total_shares'],p['sellable_shares'],Decimal(0),None,None) for p in raw['positions'])
        ps = bc.PositionsSnapshot(binding,day,start,end,positions,
            'COMPLETE_ACCOUNT_POSITIONS' if raw.get('positions_complete') is True else 'AX_EXPOSED_ROWS_ONLY')
        orders = tuple(bc.BrokerOrder(o['contract_id'],o['order_date'],o.get('order_time','00:00:00'),
             o['symbol'],o['side'].upper(),bc.decimal_cell(o['price']),o['quantity'],o.get('broker_status'),
             o.get('reported_filled'),o.get('reported_remaining'),o.get('cancelled'),
             bc.decimal_cell(o['reported_fill_price']) if o.get('reported_fill_price') is not None else None) for o in raw['orders'])
        osnap = bc.OrdersSnapshot(binding,start,end,orders,'COMPLETE_ACCOUNT_ORDERS')
        return binding,day,start,end,cash,ps,osnap

    def _verify_form(self, form, intent):
        if type(form) is not dict or type(form.get('real_identity_count')) is not int or form.get('real_identity_count') != 1 or form.get('simulation_marker') is not False or form.get('unique_expected_control') is not True or form.get('opposite_control_absent') is not True:
            raise ServiceError('FORM_CONTROL_SCOPE_UNPROVEN')
        for key in ('account_scope','shareholder_scope','symbol','side','order_type','price','quantity'):
            if type(form.get(key)) is not type(intent[key]) or form[key] != intent[key]:
                raise ServiceError('FORM_SCOPE_MISMATCH')
        if not bc.fresh(form['captured_start_ms'],form['captured_end_ms'],self._clock()):
            raise ServiceError('STALE_FORM')
        if not bc.fresh(self._active['start'],self._active['end'],self._clock()):
            raise ServiceError('STALE_CAPTURE')

    def _resolve_auto(self, request, trace=None):
        if self._signal_provider is None:
            raise ServiceError('TRUSTED_SIGNAL_PROVIDER_UNAVAILABLE')
        signal = owned(self._signal_provider(request['signal_id']))
        if trace is not None:
            trace.observe_source(signal)
        fields = {'signal_id','symbol','side','trading_day','captured_start_ms','captured_end_ms',
                  'last_price','previous_close','upper_limit','lower_limit'}
        if type(signal) is not dict or set(signal) != fields or signal['signal_id'] != request['signal_id'] or signal['side'] != 'buy':
            raise ServiceError('TRUSTED_SIGNAL_INVALID')
        bc.symbol(signal['symbol']); bc.day(signal['trading_day'])
        if not bc.fresh(signal['captured_start_ms'],signal['captured_end_ms'],self._clock()):
            raise ServiceError('STALE_SIGNAL')
        return signal

    def _transaction(self, source, request, trace=None):
        self._require_execution()
        rid = request['client_order_id'] if source == 'manual' else request['request_id']
        try:
            with stage(trace,'SETTLE'):
                replay = self.journal.claim(rid,source,request)
        except JournalError as exc:
            raise ServiceError(str(exc)) from None
        if replay is not None:
            return replay
        started = time.monotonic_ns()
        touched = False
        observed = False
        result = None
        generation = self._control['generation']
        self._active = {'source':source,'generation':generation,'state':'CLAIMED'}
        def save(state, reason, **kwargs):
            self._active['state'] = state
            response = Journal.response(rid,state,reason)
            response.update(kwargs)
            self.journal.update(rid,response)
            return response
        def event(stage):
            self.journal.event(rid,stage,(time.monotonic_ns()-started)//1_000_000)
        try:
            if self._telemetry and self._telemetry.degraded():
                raise ServiceError('TELEMETRY_DEGRADED',503)
            self._check_auto(generation)
            if source == 'auto':
                with stage(trace,'SIGNAL'):
                    signal = self._resolve_auto(request,trace)
            else:
                signal = None
            with stage(trace,'BACKEND'):
                self._get_backend()
            base_intent = dict(request) if source == 'manual' else {'client_order_id':rid,'mode':'real',
                'order_type':'limit','symbol':signal['symbol'],'side':'buy','source':'auto'}
            base_intent['source'] = source
            observed = True
            with stage(trace,'OBSERVE'):
                baseline = owned(self._backend.observe(immutable(base_intent)))
                if trace is not None:
                    trace.observe_source(baseline)
            event('observe')
            with stage(trace,'VALIDATE'):
                binding,day,start,end,cash,positions,orders = self._snapshot(baseline)
            self._active.update(start=start,end=end)
            self._check_auto(generation)
            if source == 'manual' and request['shareholder_scope'] != baseline['shareholder_scope']:
                raise ServiceError('SHAREHOLDER_SCOPE_MISMATCH')
            if source == 'auto':
                if signal['trading_day'] != day:
                    raise ServiceError('SIGNAL_DAY_MISMATCH')
                for order in orders.orders:
                    if order.symbol != signal['symbol'] or order.side != 'BUY':
                        continue
                    verdict = bc.state_verdict(order)
                    terminal = verdict in ('ORDER_CANCELLED','ORDER_REJECTED') or (
                        verdict == 'ORDER_ACCEPTED' and (
                            order.raw_status in ('已成','全部成交','完全成交')
                            or order.reported_filled == order.quantity))
                    if not terminal:
                        raise ServiceError('EXISTING_UNFINISHED_BUY')
                # The oldest signal/account capture remains the budget anchor.
                self._active['start'] = min(start,signal['captured_start_ms'])
                self._active['end'] = max(end,signal['captured_end_ms'])
                sizing = size_buy(**{k:signal[k] for k in ('last_price','previous_close','upper_limit','lower_limit')},
                                  available_cash=format(max(Decimal(0),cash-self.journal.reservations()),'f'))
                if sizing['status'] != 'SIZE_ONLY_NOT_ORDER_READY':
                    raise ServiceError(sizing['reason'])
                base_intent.update(price=sizing['price'],quantity=sizing['quantity'])
            base_intent.update(account_scope=baseline['account_scope'],shareholder_scope=baseline['shareholder_scope'])
            intent = immutable(base_intent)
            typed = bc.OrderIntent(binding,day,intent['symbol'],intent['side'].upper(),Decimal(intent['price']),intent['quantity'])
            if source == 'auto':
                check = bc.size_automatic_buy(typed,positions,now_ms=self._clock(),
                    **{k:signal[k] for k in ('last_price','previous_close','upper_limit','lower_limit')},
                    available_cash=format(max(Decimal(0),cash-self.journal.reservations()),'f'))
                if check['status'] != 'SIZE_ONLY_NOT_ORDER_READY':
                    raise ServiceError(check['reason'])
            elif intent['side'] == 'sell':
                check = bc.validate_manual_sell(typed,positions,now_ms=self._clock())
                if check['status'] != 'BUSINESS_CHECK_ONLY':
                    raise ServiceError(check['reason'])
            elif typed.limit_price*typed.quantity > cash-self.journal.reservations():
                raise ServiceError('INSUFFICIENT_AVAILABLE_CASH')
            self._check_auto(generation)
            if not bc.fresh(self._active['start'],self._active['end'],self._clock()):
                raise ServiceError('STALE_CAPTURE')
            prepared = Journal.response(rid,'PREPARED','INTENT_DURABLE_BEFORE_FORM')
            # The immutable concrete intent, not mutable caller data, is durably linked.
            with stage(trace,'PREPARED'):
                self.journal.update(rid,prepared,scope=canonical({'account_scope':binding.account_scope,
                    'shareholder_scope':intent['shareholder_scope'],'broker_trading_day':day,'intent':dict(intent),'baseline_contract_ids':sorted(o.contract_id for o in orders.orders)}),
                    reservation=format(typed.limit_price*typed.quantity,'f') if intent['side']=='buy' else '0',
                    auto_daily={'account_scope':binding.account_scope,'broker_day':day,'symbol':typed.symbol} if source == 'auto' else None)
            self._active['state'] = 'PREPARED'
            event('prepared')
            self._check_auto(generation)
            self._backend.set_before_press(self._before_press)
            with stage(trace,'PREPARE'):
                form = self._backend.prepare(intent)
            event('prepare')
            with stage(trace,'VALIDATE'):
                self._verify_form(form,intent)
            self._check_auto(generation)
            with stage(trace,'SETTLE'):
                save('SUBMITTING','SUBMIT_MAY_HAVE_OCCURRED')
            # Conservative from entry: native can raise after a physical action.
            touched = True
            with stage(trace,'SUBMIT'):
                self._press('press_submit',intent)
            event('submit')
            with stage(trace,'CONFIRMATION'):
                confirm = self._backend.confirmation(intent)
            event('confirmation')
            with stage(trace,'VALIDATE'):
                self._verify_form(confirm,intent)
            self._check_auto(generation)
            with stage(trace,'SETTLE'):
                save('CONFIRMING','CONFIRM_MAY_HAVE_OCCURRED')
            with stage(trace,'CONFIRM'):
                self._press('press_confirm',intent)
            event('confirm')
            with stage(trace,'RECONCILE'):
                receipt = owned(self._backend.reconcile(intent,owned(baseline)))
                result = self._validate_receipt(receipt,typed,orders,intent)
            event('reconcile')
        except Exception as exc:
            if trace is not None:
                trace.note(failure_code(exc))
            reason = exc.code if isinstance(exc,ServiceError) else str(exc) if isinstance(exc,JournalError) else 'BACKEND_OR_EVIDENCE_FAILURE'
            result = Journal.response(rid,'UNKNOWN' if touched else 'BLOCKED',reason)
            result['submitted'] = touched
        finally:
            if observed:
                try:
                    with stage(trace,'RESTORE'):
                        self._backend.restore()
                    event('restore')
                except Exception:
                    result = Journal.response(rid,'UNKNOWN' if touched else 'BLOCKED','RESTORE_FAILED')
                    result['submitted'] = touched
            try:
                if result is None:
                    result = Journal.response(rid,'UNKNOWN','OWNER_UNEXPECTED_EXIT')
                # Only no-press outcomes release reservation; accepted exposure is retained.
                with stage(trace,'SETTLE'):
                    self.journal.update(rid,result,reservation='0' if not touched else None)
            except Exception:
                self._fatal = True
                result = Journal.response(rid,'UNKNOWN','JOURNAL_PERSISTENCE_FAILED')
            self._active = None
        return result

    def _validate_receipt(self, receipt, typed, orders, intent):
        rid = intent['client_order_id']
        def unknown(reason):
            result = Journal.response(rid,'UNKNOWN',reason)
            result['submitted'] = True
            return result
        if type(receipt) is not dict:
            return unknown('INVALID_BROKER_RECEIPT')
        if receipt.get('status') == 'UNKNOWN':
            # Only fixed approved missing-evidence reasons cross this seam.
            reason = receipt.get('reason')
            if reason not in {'BROKER_CONTRACT_HINT_AND_INDEPENDENT_FILLS_UNQUALIFIED',
                              'BROKER_CONTRACT_UNPROVEN','INCOMPLETE_ORDER_SCOPE',
                              'NO_UNIQUE_NEW_CONTRACT_MATCH','AMBIGUOUS_NEW_ORDERS',
                              'BASELINE_IDS_DISAPPEARED','STALE_BASELINE'}:
                reason = 'BROKER_RECONCILIATION_UNPROVEN'
            return unknown(reason)
        for key in ('client_order_id','symbol','side','price','quantity'):
            if type(receipt.get(key)) is not type(intent[key]) or receipt[key] != intent[key]:
                return unknown('BROKER_RECEIPT_INTENT_MISMATCH')
        if receipt.get('order_date') != typed.trading_day or receipt.get('retryable') is not False or receipt.get('submitted') is not True:
            return unknown('BROKER_RECEIPT_CONTEXT_MISMATCH')
        ids = receipt.get('baseline_contract_ids')
        if type(ids) is not list or len(ids) != len(set(ids)) or set(ids) != {o.contract_id for o in orders.orders}:
            return unknown('BROKER_BASELINE_MISMATCH')
        contract = receipt.get('broker_contract_id')
        try:
            bc.token(contract)
        except ValueError:
            return unknown('BROKER_CONTRACT_UNPROVEN')
        if contract in ids:
            return unknown('BROKER_CONTRACT_NOT_NEW')
        # Backend owns native unique-new-order evidence; engine checks the canonical
        # receipt and exact known state, never treats a boolean as a fill proof.
        candidate = bc.BrokerOrder(contract,typed.trading_day,'00:00:00',typed.symbol,typed.side,
              typed.limit_price,typed.quantity,receipt.get('broker_status'),receipt.get('reported_filled'),
              receipt.get('reported_remaining'),receipt.get('cancelled'),
              bc.decimal_cell(receipt['reported_fill_price']) if receipt.get('reported_fill_price') is not None else None)
        verdict = bc.state_verdict(candidate)
        expected = {'ORDER_ACCEPTED':'BROKER_ORDER_VISIBLE_UNFILLED','ORDER_REJECTED':'BROKER_ORDER_REJECTED',
                    'ORDER_CANCELLED':'BROKER_ORDER_CANCELLED'}.get(verdict)
        if expected is None or receipt.get('status') not in (expected,'BROKER_ORDER_VISIBLE_FILLED' if verdict == 'ORDER_ACCEPTED' else expected):
            return unknown('BROKER_STATE_UNPROVEN')
        result = Journal.response(rid,expected,'UNIQUE_NEW_BROKER_ORDER')
        result.update(submitted=True,broker_contract_id=contract,broker_status=receipt['broker_status'],
                      symbol=typed.symbol,side=intent['side'],price=intent['price'],quantity=intent['quantity'],
                      order_date=typed.trading_day)
        return result

    def close(self):
        # Separate from admission's lock: the worker may still need lifecycle
        # operations while we wait. Serialize concurrent/reentrant closers.
        with self._close_lock:
            if self._close_state == 'COMPLETE':
                return
            if self._close_state != 'OPEN':
                raise RuntimeError('ENGINE_TEARDOWN_INCOMPLETE')
            # Latch before any fallible step, including interrupts. Journal.close
            # fences itself before closing resources; a retry/no-op is not proof
            # of completion, and an ambiguous enqueue must never be repeated.
            self._close_state = 'FAILED'
            self._worker_drained = self._owner is None
            with self._lifecycle:
                self._closed = True
                owner = self._owner
            if owner is not None:
                self._worker_drained = False
                # Bounded native calls are required; never orphan the GUI owner.
                self._slot.acquire()
                self._slot.release()
                self._queue.put(None)
                owner.join()
                if owner.is_alive():
                    raise RuntimeError('ENGINE_TEARDOWN_INCOMPLETE')
                self._worker_drained = True
            if self.journal is not None:
                self.journal.close()
            self._close_state = 'COMPLETE'
