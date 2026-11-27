"""Direct pinned pymacauto backend. No GUI imports or effects at construction.

Checked-in transaction profile is deliberately unqualified. Synthetic runtime and
profile injection are Python-only test seams, never configuration/HTTP switches.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time

from . import business_contracts as contracts
from . import fill_evidence as fills
from . import readonly_budget as rb


from .native_errors import NativeError, _block
from . import holdings, login_status
from .holdings_refresh import HoldingsRefresh


_OWNER_FAILURE_CODES = frozenset({
    'GUI_OWNER_NOT_HELD', 'GUI_OWNER_PATH_UNSAFE', 'GUI_OWNER_DIRECTORY_NOT_PRIVATE',
    'GUI_OWNER_LOCK_UNSAFE', 'GUI_OWNER_IDENTITY_CHANGED', 'GUI_INCUMBENT_MARKER_PRESENT',
    'GUI_OWNER_PROOF_FAILED',
})


def _owner_failure(error):
    code = error.args[0] if len(error.args) == 1 else None
    if not isinstance(code, str) or code not in _OWNER_FAILURE_CODES:
        code = 'GUI_OWNER_PROOF_FAILED'
    return NativeError(code)



def _profile():
    return json.loads(Path(__file__).with_name('native_profile.json').read_text())


def _verify_framework(profile):
    from .. import _ax as pymacauto
    directory = Path(pymacauto.__file__).resolve().parent
    actual = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.glob('*.py')}
    if pymacauto.__version__ != profile['framework']['version'] or actual != profile['framework']['files']:
        _block('FRAMEWORK_SOURCE_MISMATCH')


from .._ax.native import NativeBackend as _FrameworkNativeBackend

class _BudgetNativeBackend(_FrameworkNativeBackend):
    """Adapter-local clamp: the pinned framework source remains unchanged."""
    def _before_rpc(self,ref):
        b=rb.current()
        if b is None:return
        proof=rb._proof.get()
        if proof is None:raise rb.BudgetError('READ_BUDGET_UNSUPPORTED')
        for _ in range(2):
            if not rb._passive.get():proof()
            timeout=b.remaining_seconds(2)/2
            record=rb._effect.get()
            if record is not None:record['setter_attempted']=True
            b.check()
            with rb.cost('RPC_TIMEOUT'):
                self._check('AXUIElementSetMessagingTimeout',self._ax.AXUIElementSetMessagingTimeout(ref,timeout))
            b.check()
            proof()
            if b.remaining_seconds()>=timeout:
                b.check()
                return
        raise rb.BudgetError()

    def root(self,pid):
        b=rb.current()
        self.rpc_timeout=b.remaining_seconds(1) if b else 1.0
        rb.check()
        result=super().root(pid)
        rb.check()
        return result

    def get(self,ref,attribute):
        with rb.at('AX_GET'):
            self._before_rpc(ref)
            with rb.cost('AX_GET'):
                result=super().get(ref,attribute)
            rb.check()
            return result

    def actions(self,ref):
        self._before_rpc(ref)
        result=super().actions(ref)
        rb.check()
        return result

    def is_settable(self,ref,attribute):
        self._before_rpc(ref)
        result=super().is_settable(ref,attribute)
        rb.check()
        return result

    def set(self,ref,attribute,value):
        self._before_rpc(ref)
        result=super().set(ref,attribute,value)
        rb.check()
        return result

    def perform(self,ref,action):
        if rb.current() is None:return super().perform(ref,action)
        action=self._name(action)
        self._before_rpc(ref)
        rb.mark_rpc()  # Exactly adjacent to the actual native dispatch, not setter.
        self._check(f'AXUIElementPerformAction({action})',self._ax.AXUIElementPerformAction(ref,action))
        rb.check()

    def process_identity(self,pid):
        if rb.current() is not None:raise rb.BudgetError('READ_BUDGET_UNSUPPORTED')
        return super().process_identity(pid)


class NativeRuntime:
    evidence_domain = 'REAL'

    def __init__(self):
        if sys.platform != 'darwin':
            _block('MACOS_REQUIRED')
        import AppKit
        self.appkit = AppKit
        self.workspace = AppKit.NSWorkspace.sharedWorkspace()
        self.backend = _BudgetNativeBackend(rpc_timeout=rb.current().remaining_seconds(1) if rb.current() else 1.0)
        if not self.backend.trusted(prompt=False):
            _block('AX_NOT_TRUSTED')

    def app_identity(self):
        with rb.cost('APP_IDENTITY'):
            import subprocess
            expected = _profile()['app']
            apps = self.appkit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(expected['bundle_id'])
            if len(apps) != 1:
                _block('APP_COUNT_MISMATCH')
            app = apps[0]
            url = app.bundleURL()
            path = str(url.path()) if url else None
            if path != expected['path']:
                _block('APP_IDENTITY_MISMATCH')
            bundle = self.appkit.NSBundle.bundleWithPath_(path)
            meta = bundle.infoDictionary() if bundle else None
            executable = app.executableURL()
            live = str(executable.path()) if executable else None
            if not meta or not live or live != str(bundle.executablePath()) or not live.startswith(path + '/Contents/MacOS/'):
                _block('APP_EXECUTABLE_MISMATCH')
            uid = subprocess.run(['/bin/ps', '-o', 'uid=', '-p', str(app.processIdentifier())],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=rb.current().remaining_seconds(2) if rb.current() else 2, check=True, text=True).stdout.strip()
            rb.check()
            if not uid.isdecimal():
                _block('PROCESS_OWNER_UNVERIFIED')
            return {'pid': int(app.processIdentifier()), 'bundle_id': str(app.bundleIdentifier()),
                    'path': path, 'version': str(meta.get('CFBundleShortVersionString')),
                    'build': str(meta.get('CFBundleVersion')), 'owner_uid': int(uid)}

    def front(self):
        from .._ax.focus import current_front
        return current_front(self.workspace)


class _OwnedAX:
    """Guard the actual dispatch, not PageSession's potentially long read/wait.

    Only read-only methods of the pinned framework are delegated. New effect
    APIs must be explicitly integrated here, never silently forwarded.
    """
    def __init__(self, owner, backend):
        self.owner, self.backend = owner, backend
        self._switch_attempt = None

    def __getattr__(self, name):
        if name not in ('get', 'key', 'actions', 'apps', 'root', 'trusted',
                        'app_metadata', 'process_identity', 'is_settable'):
            raise AttributeError(name)
        function = getattr(self.backend, name)
        def guarded(*args, **kwargs):
            b=rb.current()
            if (name in ('get','actions','is_settable') and type(self.backend) is _BudgetNativeBackend
                    and b is not None and b.read_only):
                return self.owner._passive_effect(lambda: function(*args, **kwargs))
            return self.owner._owned_effect(lambda: function(*args, **kwargs))
        return guarded

    def set(self, ref, attr, value):
        return self.owner._owned_effect(self.backend.set, ref, attr, value)

    def perform(self, ref, action):
        attempt = self._switch_attempt
        if attempt is None:
            return self.owner._owned_effect(self.backend.perform, ref, action)
        # The pinned PageSession dispatches at most once. Never associate a
        # second effect or a native callback failure with the first proof.
        if attempt['attempted']:
            _block('NATIVE_SESSION_UNCERTAIN')
        attempt['attempted'] = True
        record={'outer_callback_entered':False,'setter_attempted':False,'actual_rpc_entered':False}
        def dispatch():
            record['outer_callback_entered']=True
            with rb.effect(record,self.owner._owner_proof):
                if not isinstance(self.backend,_BudgetNativeBackend):
                    # Injected synthetic runtime: its actual callback is next.
                    rb.mark_rpc()
                return self.backend.perform(ref, action)
        try:
            return self.owner._owned_effect(dispatch)
        except (NativeError,rb.BudgetError) as exc:
            if isinstance(exc,rb.BudgetError):exc=NativeError(exc.code)
            if (not record['actual_rpc_entered'] and type(exc) is NativeError
                    and exc.code in _OWNER_FAILURE_CODES | {'READ_BUDGET_EXCEEDED','READ_BUDGET_UNSUPPORTED'}):
                attempt['failure'] = exc
            raise exc

    def _switch_page(self, session, page):
        """One-call bridge for 0.7's receipt-only protected exceptions.

        No receipt/message parsing: only this adapter's actual pre-callback
        fixed-code rejection can replace the framework wrapper exception.
        """
        self.owner._thread()
        if self._switch_attempt is not None:
            _block('NATIVE_SESSION_UNCERTAIN')
        attempt = {'attempted': False, 'failure': None}
        self._switch_attempt = attempt
        try:
            return session.switch(page, timeout=rb.current().remaining_seconds() if rb.current() else 1)
        except Exception:
            failure, attempt['failure'] = attempt['failure'], None
            if failure is not None:
                raise failure from None
            raise
        finally:
            attempt['failure'] = None
            self._switch_attempt = None

    def press_with_permit(self, ref, callback):
        def dispatch():
            if callback() is not True:
                _block('PRESS_PERMISSION_DENIED')
            return self.backend.perform(ref, 'AXPress')
        return self.owner._owned_effect(dispatch)


class PymacautoBackend:
    def __init__(self, config, *, gui_owner=None, runtime_factory=None, profile=None, clock_ms=None):
        self.config = copy.deepcopy(config)
        self._fixed_account = None
        if self.config.get('account_identity_kind') == 'USER_CONFIRMED_SINGLE_ACCOUNT':
            from .single_fixed_account import SingleFixedAccount
            if 'account_scope' in self.config or 'shareholder_scopes' in self.config:
                _block('FIXED_POLICY_CONFIG_CONFLICT')
            try:
                self._fixed_account = SingleFixedAccount.from_private_policy(self.config.get('private_policy_path'))
            except (ValueError, TypeError):
                _block('FIXED_POLICY_UNAVAILABLE')
        elif 'account_identity_kind' in self.config or 'private_policy_path' in self.config:
            _block('INVALID_NATIVE_POLICY')
        for name in ('allow_navigation', 'allow_trade'):
            if type(self.config.get(name, False)) is not bool:
                _block('INVALID_NATIVE_POLICY')
        self.profile = copy.deepcopy(profile) if profile is not None else _profile()
        # A supplied qualified profile must NEVER qualify a native runtime.
        self._injected_profile = profile is not None
        self._runtime_factory = runtime_factory or NativeRuntime
        self._runtime = None
        self._owner = threading.get_ident()
        self._clock = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._before_press = None
        self._original = None
        self._attempt = None
        self._baseline = None
        self._stage = 'NEW'
        self._poisoned = False
        self._session_identity = None
        self._form_original = None
        self._used_ids = set()
        self._trade_page_claimed = False
        self._cancel_context = None
        self._cancel_stage = 'READY'
        self.holdings_refresh = HoldingsRefresh(self)
        from .gui_ownership import GUIOwnerLease
        if gui_owner is not None and type(gui_owner) is not GUIOwnerLease:
            _block('GUI_OWNER_NOT_HELD')
        self._gui_owner = gui_owner
        if self.config.get('allow_navigation', False) or self.config.get('allow_trade', False):
            self._owner_proof()

    def _owner_proof(self):
        with rb.at('OWNER_PROOF'):
            rb.check()
            if self._gui_owner is None:
                _block('GUI_OWNER_NOT_HELD')
            from .gui_ownership import OwnershipError
            try:
                self._gui_owner.assert_owned()
                rb.check()
            except OwnershipError as exc:
                raise _owner_failure(exc) from None

    def _passive_effect(self, function):
        self._thread()
        rb.check()
        if self._gui_owner is None:_block('GUI_OWNER_NOT_HELD')
        from .gui_ownership import OwnershipError
        entered=False
        def dispatch():
            nonlocal entered
            entered=True
            rb.require(rb.current())
            token=rb._passive.set(True)
            try:
                with rb.effect({'outer_callback_entered':True,'setter_attempted':False,'actual_rpc_entered':False},self._owner_proof):
                    result=function()
                rb.check()
                return result
            finally:rb._passive.reset(token)
        try:
            with rb.at('OWNER_LEASE'):
                return self._gui_owner.call_passive(dispatch)
        except OwnershipError as exc:
            if entered:raise
            raise _owner_failure(exc) from None
        except rb.BudgetError as exc:
            raise NativeError(exc.code) from None

    def _owned_effect(self, function, *args):
        self._thread()
        rb.check()
        if self._gui_owner is None:
            _block('GUI_OWNER_NOT_HELD')
        # Native callback exceptions are outside proof sanitization in the lease.
        from .gui_ownership import OwnershipError
        entered=False
        try:
            def guarded():
                nonlocal entered
                entered=True
                # Attribute the post-proof checkpoint to the proof, not the
                # callback that has not run yet.
                rb.check()
                with rb.at('NATIVE_CALLBACK'):
                    record={'outer_callback_entered':True,'setter_attempted':False,'actual_rpc_entered':False}
                    with rb.effect(record,self._owner_proof):
                        result=function(*args)
                    rb.check()
                    return result
            with rb.at('OWNER_LEASE'):
                return self._gui_owner.call_owned(guarded)
        except OwnershipError as exc:
            if entered:raise
            raise _owner_failure(exc) from None
        except rb.BudgetError as exc:
            raise NativeError(exc.code) from None

    def _thread(self):
        if threading.get_ident() != self._owner:
            _block('GUI_OWNER_THREAD_MISMATCH')
        if self._poisoned:
            _block('NATIVE_SESSION_UNCERTAIN')

    def set_before_press(self, callback):
        self._thread()
        if not callable(callback):
            _block('PRESS_GUARD_REQUIRED')
        self._before_press = callback

    def _connect(self):
        self._thread()
        self._owner_proof()
        _verify_framework(self.profile)
        if self._runtime is None:
            self._runtime = self._owned_effect(self._runtime_factory)
        identity = self._owned_effect(self._runtime.app_identity)
        if (any(identity.get(k) != v for k, v in self.profile['app'].items())
                or type(identity.get('pid')) is not int or not 0 < identity['pid'] < 2**31
                or identity.get('owner_uid') != os.getuid()):
            _block('APP_IDENTITY_MISMATCH')
        if self._session_identity is not None and identity != self._session_identity:
            _block('NATIVE_SESSION_IDENTITY_CHANGED')
        from .._ax import Application, PageSession
        app = Application.connect(pid=identity['pid'], backend=_OwnedAX(self, self._runtime.backend),
                                  max_nodes=14000, max_depth=80)
        if app.pid != identity['pid']:
            _block('APP_IDENTITY_MISMATCH')
        session = PageSession(app, window=self.profile['window'], pages=self.profile['pages'],
                              content_path=self.profile['content_path'],
                              trigger_children_only=self.profile['trigger_children_only'])
        return app, session, identity

    def _get(self, ref, attr):
        from .._ax.core import _get
        return _get(_OwnedAX(self, self._runtime.backend), ref, attr)

    def _nodes(self, app, root):
        # Window only: no app menu/system-wide search, secure fields pruned.
        queue = [(root, 0)]
        seen, out = set(), []
        while queue:
            ref, depth = queue.pop(0)
            key = app.backend.key(ref)
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > app.max_nodes:
                _block('AX_TRAVERSAL_LIMIT')
            role = self._get(ref, 'AXRole')
            subrole = self._get(ref, 'AXSubrole')
            if subrole == 'AXSecureTextField' or role in ('AXMenu', 'AXMenuBar'):
                continue
            node = {'ref': ref, 'role': role}
            # Do not inspect table cell values in structural discovery.
            if role == 'AXStaticText':
                node['value'] = self._get(ref, 'AXValue')
            if role == 'AXButton':
                node['title'] = self._get(ref, 'AXTitle')
            out.append(node)
            if role in ('AXTable', 'AXList', 'AXOutline'):
                continue
            children = self._get(ref, 'AXChildren') or []
            if depth >= app.max_depth and children:
                _block('AX_TRAVERSAL_LIMIT')
            queue.extend((child, depth + 1) for child in children)
        return out

    def _identity(self, app, session):
        nodes = self._nodes(app, session.scope().root)
        texts = [n.get('value', '') for n in nodes if n['role'] == 'AXStaticText']
        # Diagnostic reads only: exact vocabulary, no text or field payload output.
        title_matches = {label:0 for label in ('天府证券','持仓','委托','成交','交易','登录')}
        if texts.count('天府证券') != 1:
            for node in nodes:
                if node['role'] == 'AXStaticText':
                    title = self._get(node['ref'],'AXTitle')
                    if isinstance(title,str) and title in title_matches:title_matches[title]+=1
        if (texts.count('天府证券') != 1
                or any(marker in value for value in texts if isinstance(value, str)
                       for marker in self.profile['forbidden_text'])):
            # One bounded same-owner alternate-window diagnosis; no input effects.
            if not getattr(self,'_identity_windows_diagnosed',False):
                self._identity_windows_diagnosed=True
                try:
                    windows=self._get(app._root,'AXWindows')
                    facts=[]
                    if isinstance(windows,(list,tuple)) and len(windows)<=12:
                        for window in windows:
                            if self._get(window,'AXSubrole')=='AXSecureTextField':continue
                            ident=self._get(window,'AXIdentifier')
                            safe_ident=ident if ident=='主窗口' or isinstance(ident,str) and re.fullmatch(r'_NS:[0-9]{1,5}',ident) else 'OTHER'
                            sample=self._nodes(app,window)
                            values=[n.get('value','') for n in sample if n['role']=='AXStaticText']
                            facts.append({'identifier':safe_ident,'broker_count':values.count('天府证券'),
                                'forbidden':any(marker in v for v in values if isinstance(v,str) for marker in self.profile['forbidden_text']),
                                'known_labels':{label:sum(n.get('value')==label or n.get('title')==label for n in sample) for label in ('持仓','委托','成交','登录','交易')},
                                'node_count':len(sample)})
                    print(json.dumps({'schema':'BROKER_WINDOW_DIAG_V1','windows':facts},sort_keys=True),file=sys.stderr,flush=True)
                except BaseException:
                    pass
            # Counts only: never emit static text, account values or selectors.
            try:
                print(json.dumps({'schema':'BROKER_IDENTITY_REJECTION_V1',
                    'broker_exact_count':min(99,texts.count('天府证券')),
                    'broker_trimmed_count':sum(isinstance(v,str) and v.strip()=='天府证券' for v in texts),
                    'broker_fragment_count':sum(isinstance(v,str) and '天府证券' in v for v in texts),
                    'broker_button_count':sum(n.get('title')=='天府证券' for n in nodes),
                    'known_labels':{label:sum(n.get('value')==label or n.get('title')==label for n in nodes)
                        for label in ('持仓','委托','成交','交易','登录','交易登录','退出登录','锁定','解锁','资金','银证转账')},
                    'forbidden_present':any(marker in value for value in texts if isinstance(value,str)
                                            for marker in self.profile['forbidden_text']),
                    'static_value_string_count':sum(isinstance(v,str) for v in texts),
                    'static_value_nonempty_count':sum(isinstance(v,str) and bool(v) for v in texts),
                    'static_title_matches':title_matches,
                    'node_roles':{role:sum(n['role']==role for n in nodes) for role in ('AXWindow','AXStaticText','AXButton','AXScrollArea','AXTable','AXTextField','AXTabGroup')},
                    'static_text_count':min(14000,len(texts))},sort_keys=True),file=sys.stderr,flush=True)
            except BaseException:
                pass
            _block('REAL_BROKER_IDENTITY_UNPROVEN')
        return nodes

    def _scope_config(self):
        if self._fixed_account is not None:
            return self._fixed_account.account_key(), self._fixed_account.transport_scopes()
        scope = self.config.get('account_scope')
        scopes = self.config.get('shareholder_scopes')
        try:
            contracts.token(scope)
            if type(scopes) is not dict or not scopes or set(scopes) - {'上海', '深圳'}:
                raise ValueError()
            for value in scopes.values():
                contracts.token(value)
        except (ValueError, TypeError):
            _block('ACCOUNT_SCOPE_NOT_CONFIGURED')
        return scope, scopes

    def _table(self, target=None, *, require_current=None):
        from .._ax import read_table
        start = self._clock()
        with rb.at('CONNECT'):
            app, session, identity = self._connect()
        front = self._owned_effect(self._runtime.front)
        with rb.at('IDENTITY'):
            self._identity(app, session)
        with rb.at('PANEL_DETECT'):
            initial = session.detect()
        if require_current is not None and initial['page'] != require_current:
            _block('POSITIONS_PANEL_REQUIRED')
        if target is not None and initial['page'] != target:
            if not self.config.get('allow_navigation', False):
                _block('NAVIGATION_DISABLED')
            if self._original is None:
                self._original = initial['page']
            # PageSession uses unique AX trigger; never coordinates/keyboard.
            with rb.at('SWITCH_TARGET'):
                app.backend._switch_page(session, target)
            with rb.at('IDENTITY'):
                self._identity(app, session)
            with rb.at('PANEL_DETECT'):
                initial = session.detect()
        with rb.at('TABLE_READ'):
            raw = read_table(app.backend, initial['table'])
        with rb.at('PANEL_DETECT'):
            after = session.detect()
        with rb.at('IDENTITY'):
            self._identity(app, session)
        if (after['page'] != initial['page'] or after['headers'] != initial['headers']
                or app.backend.key(after['table']) != app.backend.key(initial['table'])):
            _block('PANEL_CHANGED')
        if self._owned_effect(self._runtime.app_identity) != identity or self._owned_effect(self._runtime.front) != front:
            _block('NATIVE_CONTINUITY_CHANGED')
        return {'panel': initial['page'], 'raw': raw, 'start': start, 'end': self._clock()}

    def _matrix(self, raw):
        headers, rows = raw['headers'], raw['rows']
        if raw.get('stable_double_read') is not True or raw.get('row_column_crosscheck') is not True:
            _block('NATIVE_TABLE_UNSTABLE')
        if any(type(row) is not dict or set(row) != set(headers) for row in rows):
            _block('NATIVE_ROW_KEYS_MISMATCH')
        matrix = [[row[h] for h in headers] for row in rows]
        contracts.table(headers, matrix, headers)
        canonical = json.dumps({'headers': headers, 'matrix': matrix}, ensure_ascii=False, separators=(',', ':')).encode()
        if raw['sha256'] != hashlib.sha256(canonical).hexdigest():
            _block('NATIVE_CONTENT_SHA_MISMATCH')
        return headers, matrix

    def _verify_fixed_rows(self, pairs, *, diagnostic=False):
        if self._fixed_account is None:
            return
        scopes = {}
        for market, holder in pairs:
            if market not in contracts.MARKET_LABELS or holder is None:
                _block('FIXED_SCOPE_CHANGED')
            region = '上海' if market in ('沪Ａ', '沪A', '上海Ａ股', '上海A股') else '深圳'
            if region in scopes and scopes[region] != holder:
                _block('FIXED_SCOPE_CHANGED')
            scopes[region] = holder
        if diagnostic:
            status,missing=self._fixed_account.observation_status('天府证券',scopes)
            return dict(scope_status=status,observed_market_count=len(scopes),missing_market_count=missing)
        try:
            self._fixed_account.verify_visible('天府证券', scopes)
        except ValueError as exc:
            code=exc.args[0] if len(exc.args)==1 else None
            _block(code if code in ('FIXED_SCOPE_CHANGED','FIXED_SCOPE_INCOMPLETE_OBSERVATION') else 'FIXED_SCOPE_CHANGED')

    def _check_holders(self, positions):
        self._verify_fixed_rows((p.market, p.shareholder) for p in positions.positions)
        scope, expected = self._scope_config()
        for row in positions.positions:
            market = '上海' if row.market in ('沪Ａ', '沪A', '上海Ａ股', '上海A股') else '深圳'
            if row.market is None or row.shareholder is None or expected.get(market) != row.shareholder:
                _block('SHAREHOLDER_SCOPE_MISMATCH')
        return scope

    def _binding(self):
        return contracts.AccountBinding(self._runtime.evidence_domain, '天府证券', self._scope_config()[0])

    def _positions(self, capture, trading_day):
        headers, rows = self._matrix(capture['raw'])
        result = contracts.normalize_positions(headers, rows, binding=self._binding(), trading_day=trading_day,
                                               captured_start_ms=capture['start'], captured_end_ms=capture['end'])
        self._check_holders(result)
        return result

    def _cash(self, app, session):
        cfg = self.profile['funds']
        if (cfg.get('read_qualified') is not True
                or cfg.get('read_schema') != 'HEADERLESS_AXCELL_STATIC_TEXT_PAIR_V1'
                or cfg.get('read_table') != {'role': 'AXTable', 'identifier': '_NS:65'}
                or cfg.get('exposed_rows') != 10):
            _block('CASH_ASSOCIATION_UNPROVEN')
        started = time.monotonic()
        identity, front = self._owned_effect(self._runtime.app_identity), self._owned_effect(self._runtime.front)

        def get(ref, attr):
            if time.monotonic() - started >= 15:
                _block('CASH_ASSOCIATION_UNPROVEN')
            return self._get(ref, attr)

        def refs(ref, attr, size):
            value = get(ref, attr)
            if not isinstance(value, (list, tuple)) or len(value) != size:
                _block('CASH_ASSOCIATION_UNPROVEN')
            if len({app.backend.key(r) for r in value}) != size:
                _block('CASH_ASSOCIATION_UNPROVEN')
            return list(value)

        def keys(values):
            return tuple(app.backend.key(r) for r in values)

        def locate():
            nodes = self._identity(app, session)
            matches = [n for n in nodes if n['role'] == 'AXTable'
                       and get(n['ref'], 'AXIdentifier') == '_NS:65']
            if (len(matches) != 1 or matches[0]['role'] != 'AXTable'
                    or any(n['role'] == 'AXStaticText' and n.get('value') == '可用金额' for n in nodes)):
                _block('CASH_ASSOCIATION_UNPROVEN')
            return matches[0]['ref']

        # Historical sibling geometry is a SYNTHETIC-only compatibility fixture,
        # never a native fallback when the observed funds table is missing.
        nodes = self._identity(app, session)
        if (self._runtime.evidence_domain == 'SYNTHETIC'
                and not any(get(n['ref'], 'AXIdentifier') == '_NS:65' for n in nodes)):
            return self._synthetic_cash(app, session)
        table = locate()

        def sample():
            if (get(table, 'AXRole') != 'AXTable'
                    or get(table, 'AXSubrole') == 'AXSecureTextField'):
                _block('CASH_ASSOCIATION_UNPROVEN')
            columns, rows = refs(table, 'AXColumns', 2), refs(table, 'AXRows', 10)
            for attr, expected in [('AXRowCount', 10), ('AXColumnCount', 2)]:
                declared = get(table, attr)
                if declared is not None and (type(declared) is not int or declared != expected):
                    _block('CASH_ASSOCIATION_UNPROVEN')
            axes = []
            for j, col in enumerate(columns):
                index = get(col, 'AXIndex')
                if (get(col, 'AXRole') != 'AXColumn' or type(index) is not int or index != j
                        or get(col, 'AXHeader') is not None):
                    _block('CASH_ASSOCIATION_UNPROVEN')
                axes.append(refs(col, 'AXRows', 10))
            topology, labels, candidates, seen_cells = [], [], [], set()
            for i, row in enumerate(rows):
                index = get(row, 'AXIndex')
                if get(row, 'AXRole') != 'AXRow' or type(index) is not int or index != i:
                    _block('CASH_ASSOCIATION_UNPROVEN')
                cells = refs(row, 'AXChildren', 2)
                for j, cell in enumerate(cells):
                    key = app.backend.key(cell)
                    if (key in seen_cells or key != app.backend.key(axes[j][i])
                            or get(cell, 'AXRole') != 'AXCell'
                            or get(cell, 'AXSubrole') == 'AXSecureTextField'):
                        _block('CASH_ASSOCIATION_UNPROVEN')
                    seen_cells.add(key)
                label_children = refs(cells[0], 'AXChildren', 1)
                label = label_children[0]
                if (get(label, 'AXRole') != 'AXStaticText'
                        or get(label, 'AXSubrole') == 'AXSecureTextField'):
                    _block('CASH_ASSOCIATION_UNPROVEN')
                value = get(label, 'AXValue')
                if type(value) is not str:
                    _block('CASH_ASSOCIATION_UNPROVEN')
                labels.append(value)
                topology.append((keys(cells), keys(label_children)))
                if value == '可用金额':
                    children = refs(cells[1], 'AXChildren', 1)
                    text = children[0]
                    if (get(text, 'AXRole') != 'AXStaticText'
                            or get(text, 'AXSubrole') == 'AXSecureTextField'):
                        _block('CASH_ASSOCIATION_UNPROVEN')
                    raw = get(text, 'AXValue')
                    try:
                        cash = format(contracts.normalize_available_cash(raw), 'f')
                    except (ValueError, TypeError):
                        _block('CASH_ASSOCIATION_UNPROVEN')
                    candidates.append((keys(children), raw, cash))
            if (len(candidates) != 1 or keys(refs(table, 'AXRows', 10)) != keys(rows)
                    or keys(refs(table, 'AXColumns', 2)) != keys(columns)):
                _block('CASH_ASSOCIATION_UNPROVEN')
            return keys(columns), keys(rows), tuple(topology), tuple(labels), candidates[0]

        first = sample()
        if first != sample() or app.backend.key(locate()) != app.backend.key(table):
            _block('CASH_ASSOCIATION_UNPROVEN')
        if self._owned_effect(self._runtime.app_identity) != identity or self._owned_effect(self._runtime.front) != front:
            _block('NATIVE_CONTINUITY_CHANGED')
        if time.monotonic() - started >= 15:
            _block('CASH_ASSOCIATION_UNPROVEN')
        return first[-1][-1]

    def _synthetic_cash(self, app, session):
        nodes = self._identity(app, session)
        values = []
        for n in nodes:
            children = self._get(n['ref'], 'AXChildren') or []
            for i, ref in enumerate(children):
                if (self._get(ref, 'AXRole') == 'AXStaticText'
                        and self._get(ref, 'AXSubrole') != 'AXSecureTextField'
                        and self._get(ref, 'AXValue') == '可用金额'):
                    if i + 1 >= len(children):
                        _block('CASH_ASSOCIATION_UNPROVEN')
                    other = children[i + 1]
                    if self._get(other, 'AXRole') != 'AXStaticText' or self._get(other, 'AXSubrole') == 'AXSecureTextField':
                        _block('CASH_ASSOCIATION_UNPROVEN')
                    values.append(format(contracts.normalize_available_cash(self._get(other, 'AXValue')), 'f'))
        if len(values) != 1:
            _block('CASH_ASSOCIATION_UNPROVEN')
        return values[0]

    def _discovered_form(self, app, session, *, side_aware=False, confirmation=None):
        """SELL by default; side-aware facts or a closed required confirmation."""
        from .form_discovery import discover, DiscoveryError
        if side_aware:
            from .buy_form_discovery import discover
        # Every GUI read requires the Broker lease; supplied proof stays live.
        if self._gui_owner is not None:
            self._owner_proof()
        try:
            if side_aware:
                proof = discover(app.backend, session.scope().root, confirmation=confirmation)
            else:
                proof = discover(app.backend, session.scope().root)
        except DiscoveryError:
            _block('FORM_ASSOCIATION_UNPROVEN')
        if self._gui_owner is not None:
            self._owner_proof()
        return proof

    def _read_form(self, *, side_aware=False):
        start = self._clock()
        app, session, identity = self._connect()
        front = self._owned_effect(self._runtime.front)
        proof = self._discovered_form(app, session, side_aware=side_aware)
        if self._gui_owner is not None:
            self._owner_proof()
        if self._owned_effect(self._runtime.app_identity) != identity or self._owned_effect(self._runtime.front) != front:
            _block('NATIVE_CONTINUITY_CHANGED')
        end = self._clock()
        if end - start > 15000:
            _block('STALE_READ_CAPTURE')
        facts = proof.facts()
        return {'status': self._runtime.evidence_domain + '_FORM_READ_ONLY',
                'mode': self._runtime.evidence_domain.lower(), 'profile_id': self.profile['profile_id'],
                'captured_start_ms': start, 'captured_end_ms': end, 'form': facts,
                'profile_candidate_reference': {'policy': facts['policy'],
                                                 'observed_confirmation': facts['observed_confirmation']},
                'selected_direction': None, 'transaction_qualified': False,
                'execution_enabled': False, 'monetary_authority': 'NONE'}

    def _fill_scope(self, capture):
        """Record the actual independent holder producer, not a REAL flag proof."""
        headers, rows = self._matrix(capture['raw'])
        self._scoped_position_rows(headers, rows)
        parsed = contracts.table(headers, rows, ('交易市场', '股东账户'))
        pairs = tuple(sorted(set((r['交易市场'], r['股东账户']) for r in parsed)))
        return fills.ScopeRecord(self._binding(), 'POSITION_HOLDER_REFERENCE_V1', capture['raw']['sha256'],
                                 capture['start'], capture['end'], pairs)


    def _navigation_modal_clear(self, app, session, identity, root, front):
        from .login_result import modal_census, Unrecognized
        key=app.backend.key
        self._owner_proof()
        if (self._owned_effect(self._runtime.app_identity)!=identity
                or key(app.backend.root(app.pid))!=key(app._root)
                or key(session.scope().root)!=key(root)
                or self._owned_effect(self._runtime.front)!=front):return False
        try:_,modals=modal_census(self,app,root)
        except Unrecognized:return False
        return not modals

    def _press_operator_control(self, ref):
        # Called inside the existing owned-effect boundary.
        if not isinstance(self._runtime.backend, _BudgetNativeBackend):
            rb.mark_rpc()
        return self._runtime.backend.perform(ref, 'AXPress')

    def _login_entry(self, action, budget, *, narrow=False, binding=None):
        # Internal compatibility for the existing login recovery/ledger flows.
        if action == 'login-open':
            from .login_entry import open_dialog
            return open_dialog(self, budget, narrow=narrow, binding=binding)
        return login_status.status(self, budget, inspect=action == 'login-inspect', narrow=narrow, binding=binding)

    def _require_readonly_operator(self, budget):
        rb.require(budget)
        self._thread()
        if not budget.read_only:
            _block('READ_BUDGET_UNSUPPORTED')
        if self._original is not None or self._attempt is not None:
            _block('NATIVE_SESSION_UNCERTAIN')

    def login_status(self, *, budget, inspect=False):
        self._require_readonly_operator(budget)
        return login_status.status(self, budget, inspect=inspect)

    def login(self, *, budget):
        self._require_readonly_operator(budget)
        from .login_auto import ensure
        return ensure(self, budget)

    def trade_page(self, action, *, budget):
        """Compatibility dispatcher for the original operator action API."""
        if action not in ('login-status','inspect','switch','login-inspect','login-open','ensure-login','retry-login','fresh-login'):
            rb.require(budget)
            self._thread()
            _block('READ_BUDGET_UNSUPPORTED')
        if action == 'ensure-login':
            return self.login(budget=budget)
        if action in ('login-status','login-inspect'):
            return self.login_status(budget=budget, inspect=action == 'login-inspect')
        self._require_readonly_operator(budget)
        if action=='retry-login':
            from .login_retry import retry
            return retry(self,budget)
        if action=='fresh-login':
            from .login_retry import fresh_login
            return fresh_login(self,budget)
        if action == 'login-open':
            return self._login_entry(action,budget)
        from .trade_navigation import trade_page
        return trade_page(self, action, budget)

    def read_budgeted(self,kind,*,budget):
        rb.require(budget)
        self._thread()
        if getattr(self,'_read_budget',None) is not None:_block('READ_BUDGET_UNSUPPORTED')
        self._read_budget=budget
        primary=None
        try:
            result=self.read(kind)
            budget.check()
            return result
        except BaseException as exc:
            primary=exc
            raise
        finally:
            try:
                if self._original is not None:
                    budget.set(phase='RESTORE',restore_required=True)
                    try:
                        with rb.at('RESTORE'):
                            budget.check()
                            self.restore()
                            budget.check()
                        budget.set(restored_verified=True)
                    except BaseException:
                        self._poisoned=True
                        budget.set(poisoned=True)
                        # Diagnostic failure must not replace the operation's
                        # primary or escape instead of the fixed restore error.
                        try:
                            budget.event('RESTORE_FAILED')
                        except BaseException:
                            pass
                        if primary is None:_block('NATIVE_SESSION_UNCERTAIN')
            finally:
                self._read_budget=None
                budget.set(phase='FINALIZE')

    def read(self, kind):
        self._thread()
        if kind == 'holdings-refresh-controls':
            rb.require(self._read_budget)
            return self.holdings_refresh.discover()
        if kind == 'health':
            return {'status': 'AVAILABLE', 'backend': 'direct-pymacauto', 'profile_id': self.profile['profile_id'],
                    'execution_enabled': False, 'runtime_transaction': False,
                    'transaction_blockers': ['BLOCKED_PROFILE_UNQUALIFIED', 'INCOMPLETE_ORDER_SCOPE',
                                             'BROKER_DAY_UNQUALIFIED', 'CASH_ASSOCIATION_UNQUALIFIED']}
        # Passive AX reads share the same Broker owner as navigation and trades.
        # Metadata health above deliberately does not construct or inspect GUI.
        budget=rb.current()
        if budget is None or getattr(self,'_read_budget',None) is not budget:_block('READ_BUDGET_UNSUPPORTED')
        rb.require(budget)
        self._owner_proof()
        if kind == 'form':
            return self._read_form()
        if kind == 'form-discovery':
            return self._read_form(side_aware=True)
        if kind in ('positions-panel', 'positions-panel-summary', 'available-cash') and (
                not budget.read_only or self.config.get('allow_trade') is not False):
            _block('UNSUPPORTED_READ_KIND')
        if kind not in ('current', 'positions', 'positions-panel', 'positions-panel-summary',
                        'positions-scope-diagnostic', 'orders', 'trades', 'account', 'available-cash'):
            _block('UNSUPPORTED_READ_KIND')
        try:
            if kind in ('positions', 'positions-panel', 'positions-panel-summary', 'positions-scope-diagnostic'):
                return holdings.read(self, kind)
            if kind == 'available-cash':
                return self._available_cash()
            if kind in ('orders', 'trades'):
                # No silent binding from configured raw shareholder tokens alone.
                # Prove the market/holder domain on the independent positions page.
                proof_capture = self._table('持仓')
                ph, pm = self._matrix(proof_capture['raw'])
                self._scoped_position_rows(ph, pm)
                if kind == 'trades':
                    fill_scope = self._fill_scope(proof_capture)
            capture = self._table({'orders': '委托', 'trades': '成交'}.get(kind))
            raw = capture['raw']
            headers, rows = self._matrix(raw)
            report = holdings.table_report(capture)
            if kind == 'orders':
                orders = contracts.normalize_orders(headers, rows, binding=self._binding(),
                                captured_start_ms=capture['start'], captured_end_ms=capture['end'])
                report['orders'] = [dict(contract_id=o.contract_id, symbol=o.symbol, name=row.get('证券名称'),
                        side=row['操作'], quantity=o.quantity, price=format(o.price, 'f'), status=o.raw_status,
                        order_date=o.trading_day, order_time=o.order_time) for o, row in zip(orders.orders, raw['rows'])]
            elif kind == 'trades':
                try:
                    snapshot = fills.normalize_native_trades(raw, scope=fill_scope,
                                captured_start_ms=capture['start'], captured_end_ms=capture['end'])
                except ValueError:
                    _block('INVALID_INDEPENDENT_TRADE_EVIDENCE')
                report['trades'] = fills.trade_payload(snapshot)
                report['fill_evidence'] = fills.evidence_payload(snapshot)
            elif kind == 'account':
                proof_capture = capture if capture['panel'] == '持仓' else self._table('持仓')
                ph, pm = self._matrix(proof_capture['raw'])
                self._scoped_position_rows(ph, pm)
                app, session, _ = self._connect()
                report.update(account_scope=self._scope_config()[0], cash_available=self._cash(app, session),
                              funding_account=None, binding={'domain': self._runtime.evidence_domain,
                                                           'broker': '天府证券', 'account_scope': self._scope_config()[0]})
                report['captured_end_ms'] = self._clock()
                if report['captured_end_ms'] - capture['start'] > 15000:
                    _block('STALE_READ_CAPTURE')
            if kind in ('orders', 'trades'):
                domain = self._runtime.evidence_domain
                report.update(status=domain + '_' + kind.upper() + '_READ_ONLY', mode=domain.lower(),
                              captured_at_ms=capture['start'])
                if self._clock() - capture['start'] > 15000:
                    _block('STALE_READ_CAPTURE')
            return report
        finally:
            if self._original is not None and getattr(self,'_read_budget',None) is None:
                self._restore_preserving_primary(sys.exception())

    def _scoped_position_rows(self, headers, rows, *, diagnostic=False, display_only=False):
        return holdings.scoped_position_rows(self, headers, rows, diagnostic=diagnostic, display_only=display_only)

    def _available_cash(self):
        # Strict account proof, current page only; no navigation fallback.
        app, session, identity = self._connect()
        root = app.backend.key(session.scope().root)
        front = self._owned_effect(self._runtime.front)
        capture = self._table(require_current='持仓')
        self._scoped_position_rows(*self._matrix(capture['raw']))
        amount = self._cash(app, session)
        if (self._owned_effect(self._runtime.app_identity) != identity
                or app.backend.key(session.scope().root) != root
                or self._owned_effect(self._runtime.front) != front
                or session.detect()['page'] != '持仓'):
            _block('NATIVE_CONTINUITY_CHANGED')
        end = self._clock()
        if end - capture['start'] > 15000:
            _block('STALE_READ_CAPTURE')
        return dict(status='AVAILABLE_CASH_READ_ONLY', cash_available=amount, panel='持仓',
                    captured_start_ms=capture['start'], captured_end_ms=end,
                    freshness_basis='CLIENT_UI_ONLY', upstream_freshness='UNVERIFIED',
                    monetary_authority='NONE', execution_enabled=False)

    def _qualified(self):
        if (not self.profile['transaction'].get('qualified') or not self.profile['funds'].get('qualified')
                or (self._injected_profile and getattr(self._runtime, 'evidence_domain', None) != 'SYNTHETIC')):
            _block('BLOCKED_PROFILE_UNQUALIFIED')

    def observe_cancel(self, target):
        from .native_cancel import observe
        return observe(self, target)

    def press_cancel(self, target):
        from .native_cancel import press
        return press(self, target)

    def open_cancel(self, target):
        from .native_cancel import open_dialog
        return open_dialog(self, target)

    def _selector(self, app, session, selector, root=None):
        from .._ax.query import QueryScope
        if not isinstance(selector, dict) or not selector:
            _block('BLOCKED_PROFILE_UNQUALIFIED')
        return QueryScope(app, root or session.scope().root, leaf_roles=('AXTable', 'AXMenu', 'AXMenuBar')).find(**selector)

    def _day(self, app, session):
        self._qualified()
        value = self._selector(app, session, self.profile['transaction']['trading_day']).get_value()
        return contracts.day(value)

    def _complete(self, app, session, name):
        selector = self.profile['transaction'].get('complete_' + name)
        if not selector:
            return False
        return self._selector(app, session, selector).get_value() == self.profile['transaction']['complete_' + name + '_value']

    def _intent(self, intent, *, observation=False):
        if not isinstance(intent, Mapping) or intent.get('mode') != 'real' or intent.get('order_type') != 'limit':
            _block('INVALID_NATIVE_INTENT')
        scope, holders = self._scope_config()
        symbol = contracts.symbol(intent.get('symbol'))
        region = '上海' if symbol.startswith(('5', '6')) else '深圳'
        holder = holders.get(region)
        if (holder is None or intent.get('shareholder_scope', holder) != holder
                or intent.get('account_scope', scope) != scope):
            _block('SHAREHOLDER_SCOPE_MISMATCH')
        if intent.get('side') not in ('buy', 'sell') or intent.get('source') not in ('manual', 'auto'):
            _block('INVALID_NATIVE_INTENT')
        if intent.get('source') == 'auto' and intent['side'] == 'sell':
            _block('SELL_RULE_NOT_CONFIGURED')
        if not (observation and intent['source'] == 'auto') and type(intent.get('price')) is not str:
            _block('INVALID_NATIVE_INTENT')
        result = copy.deepcopy(dict(intent))
        result.update(account_scope=scope, shareholder_scope=holder)
        return result

    def observe(self, intent):
        self._thread()
        if not self.config.get('allow_trade', False):
            _block('NATIVE_TRADE_DISABLED')
        frozen = self._intent(intent, observation=True)
        if self._attempt is not None:
            _block('NATIVE_ATTEMPT_ALREADY_OWNED')
        if frozen['client_order_id'] in self._used_ids:
            _block('NATIVE_ATTEMPT_ID_ALREADY_USED')
        start = self._clock()
        app, session, identity = self._connect()
        self._identity(app, session)
        self._qualified()
        self._session_identity = identity
        original = session.detect()['page']
        self._original = original
        try:
            day = self._day(app, session)
            pc = self._table('持仓')
            positions = self._positions(pc, day)
            wanted_region = '上海' if frozen['symbol'].startswith(('5', '6')) else '深圳'
            market_labels = ('沪Ａ', '沪A', '上海Ａ股', '上海A股') if wanted_region == '上海' else ('深Ａ', '深A', '深圳Ａ股', '深圳A股')
            if not any(p.market in market_labels and p.shareholder == frozen['shareholder_scope'] for p in positions.positions):
                _block('REQUESTED_MARKET_SHAREHOLDER_UNPROVEN')
            app, session, _ = self._connect()
            self._identity(app, session)
            cash = self._cash(app, session)
            positions_complete = self._complete(app, session, 'positions')
            oc = self._table('委托')
            app, session, _ = self._connect()
            orders_complete = self._complete(app, session, 'orders')
            headers, rows = self._matrix(oc['raw'])
            orders = contracts.normalize_orders(headers, rows, binding=self._binding(),
                         captured_start_ms=oc['start'], captured_end_ms=oc['end'],
                         coverage='COMPLETE_ACCOUNT_ORDERS' if orders_complete else 'AX_EXPOSED_ROWS_ONLY')
            if day != self._day(app, session):
                _block('BROKER_DAY_CHANGED')
            end = self._clock()
            if not contracts.fresh(start, end, end):
                _block('STALE_BASELINE')
            from .account_observation import build_account_observation
            result = build_account_observation(fixed_account=self._fixed_account, binding=self._binding(),
                      shareholder=frozen['shareholder_scope'], positions=positions, orders=orders,
                      broker_day=day, cash=cash, start=start, end=end, now=self._clock(),
                      original_panel=original, positions_complete=positions_complete, orders_complete=orders_complete)
        finally:
            self._restore_preserving_primary(sys.exception())
        self._attempt, self._baseline = frozen, copy.deepcopy(result)
        self._stage = 'OBSERVED'
        return result

    @staticmethod
    def _order(o):
        return {'contract_id': o.contract_id, 'symbol': o.symbol, 'side': o.side.lower(),
                'price': format(o.price, 'f'), 'quantity': o.quantity, 'order_date': o.trading_day,
                'order_time': o.order_time, 'broker_status': o.raw_status,
                'reported_filled': o.reported_filled, 'reported_remaining': o.reported_remaining,
                'cancelled': o.cancelled, 'reported_fill_price': None if o.reported_fill_price is None else format(o.reported_fill_price, 'f')}

    def _admit(self, intent, stages):
        self._thread()
        if not self.config.get('allow_trade', False):
            _block('NATIVE_TRADE_DISABLED')
        if not self.config.get('allow_navigation', False):
            _block('NAVIGATION_DISABLED')
        candidate = self._intent(intent)
        expected = dict(self._attempt or {})
        # Automatic sizing occurs in the engine only after observation; bind the
        # concrete price/quantity once at prepare, never at HTTP observation.
        if expected.get('source') == 'auto' and self._stage == 'OBSERVED':
            expected.update(price=candidate.get('price'), quantity=candidate.get('quantity'))
        if candidate != expected or self._stage not in stages:
            _block('NATIVE_ATTEMPT_STATE_MISMATCH')
        self._qualified()
        if not self._baseline['orders_complete']:
            _block('INCOMPLETE_ORDER_SCOPE')
        if not contracts.fresh(self._baseline['captured_start_ms'], self._baseline['captured_end_ms'], self._clock()):
            _block('STALE_BASELINE')

    def _form_fields(self, app, session, *, side):
        """Reuse reviewed association only behind existing profile admission.

        Descriptor is policy selection, not an imported receipt/qualification.
        Require the intent side's exact confirmation, never passive auto-detection.
        """
        cfg = self.profile['transaction']
        if 'form_descriptor' in cfg:
            self._qualified()
            from .form_discovery import Confirmation, POLICY
            if type(side) is not str or side not in ('buy', 'sell'):
                _block('BLOCKED_PROFILE_UNQUALIFIED')
            confirmation = Confirmation.BUY if side == 'buy' else Confirmation.SELL
            if (cfg['form_descriptor'] != {'policy': POLICY,
                                           'observed_confirmation': confirmation.value}
                    or cfg.get('fields') is not None):
                _block('BLOCKED_PROFILE_UNQUALIFIED')
            from .._ax.core import Element
            proof = self._discovered_form(app, session, side_aware=True, confirmation=confirmation)
            return {name: Element(app, ref) for name, ref in proof.fields.items()}
        selectors = cfg.get('fields')
        if not isinstance(selectors, dict) or set(selectors) != {'code', 'price', 'quantity'}:
            _block('BLOCKED_PROFILE_UNQUALIFIED')
        return {name: self._selector(app, session, sel) for name, sel in selectors.items()}

    def _form(self, intent, *, verify_values):
        app, session, _ = self._connect()
        nodes = self._identity(app, session)
        for label in ('限价委托', '代码', '价格', '数量', '方向'):
            if sum(n['role'] == 'AXStaticText' and n.get('value') == label for n in nodes) != 1:
                _block('LIMIT_FORM_LABELS_UNPROVEN')
        title = '确定买入' if intent['side'] == 'buy' else '确定卖出'
        opposite = '确定卖出' if intent['side'] == 'buy' else '确定买入'
        if any(n['role'] == 'AXButton' and n.get('title') == opposite for n in nodes):
            _block('OPPOSITE_FORM_VISIBLE')
        fields = self._form_fields(app, session, side=intent['side'])
        if set(fields) != {'code', 'price', 'quantity'} or len({app.backend.key(f.ref) for f in fields.values()}) != 3:
            _block('ORDER_FIELDS_AMBIGUOUS')
        for field in fields.values():
            if field.get('AXRole') != 'AXTextField' or field.get('AXSubrole') == 'AXSecureTextField' or field.get('AXEnabled') is not True:
                _block('ORDER_FIELD_UNSAFE')
        button = self._selector(app, session, {'role': 'AXButton', 'title': title, 'enabled': True})
        if verify_values:
            if (fields['code'].get_value() != intent['symbol']
                    or contracts.decimal_cell(fields['price'].get_value()) != Decimal(intent['price'])
                    or contracts.shares(fields['quantity'].get_value()) != intent['quantity']):
                _block('FORM_VALUE_MISMATCH')
            if intent['side'] == 'buy' and Decimal(self._cash(app, session)) < Decimal(intent['price']) * intent['quantity']:
                _block('INSUFFICIENT_CASH')
            if intent['side'] == 'sell':
                matches = [p for p in self._baseline['positions'] if p['symbol'] == intent['symbol']]
                if len(matches) != 1 or matches[0]['sellable_shares'] < intent['quantity']:
                    _block('INSUFFICIENT_SELLABLE')
        return app, session, fields, button

    def _proof(self, intent, start):
        return dict(real_identity_count=1, simulation_marker=False, account_scope=self._scope_config()[0],
                    shareholder_scope=self._attempt['shareholder_scope'], symbol=intent['symbol'], side=intent['side'],
                    order_type='limit', price=intent['price'], quantity=intent['quantity'],
                    unique_expected_control=True, opposite_control_absent=True,
                    captured_start_ms=start, captured_end_ms=self._clock())

    def prepare(self, intent):
        self._admit(intent, {'OBSERVED'})
        start = self._clock()
        # Validate the typed immutable intent before any AXSetValue.
        contracts.OrderIntent(self._binding(), self._baseline['broker_trading_day'], intent['symbol'],
                              intent['side'].upper(), Decimal(intent['price']), intent['quantity'])
        self._attempt = self._intent(intent)
        _, _, initial_fields, _ = self._form(intent, verify_values=False)
        self._form_original = {name: field.get_value() for name, field in initial_fields.items()}
        if any(type(value) is not str for value in self._form_original.values()):
            _block('ORIGINAL_FORM_UNREADABLE')
        self._original = self._baseline['original_panel']
        self._stage = 'PREPARING'  # no retry after partial input
        for name, value in [('code', intent['symbol']), ('price', intent['price']), ('quantity', str(intent['quantity']))]:
            self._admit(intent, {'PREPARING'})
            _, _, fields, _ = self._form(intent, verify_values=False)
            fields[name].set_value(value, verify_timeout=0)
        self._form(intent, verify_values=True)
        self._stage = 'PREPARED'
        return self._proof(intent, start)

    def _press(self, intent, element, next_stage):
        # Complete all reads FIRST; callback is adjacent to the native AXPress.
        if element.get('AXEnabled') is not True or element.get('AXSubrole') == 'AXSecureTextField':
            _block('PRESS_CONTROL_UNSAFE')
        if 'AXPress' not in element.backend.actions(element.ref):
            _block('PRESS_ACTION_UNAVAILABLE')
        if self._before_press is None:
            _block('PRESS_GUARD_REQUIRED')
        self._stage = next_stage  # consume even when callback/AX dispatch raises
        try:
            # Ownership proof precedes final Engine permission; no AX reads or
            # ownership filesystem work between that permission and AXPress.
            element.backend.press_with_permit(element.ref, self._before_press)
        except BaseException:
            self._poisoned = True
            raise

    def press_submit(self, intent):
        self._admit(intent, {'PREPARED'})
        _, _, _, button = self._form(intent, verify_values=True)
        self._press(intent, button, 'SUBMIT_DISPATCHED')

    def _sheet(self, intent):
        app, session, _ = self._connect()
        self._identity(app, session)
        cfg = self.profile['transaction']['confirmation']
        sheet = self._selector(app, session, cfg['sheet'])
        nodes = self._nodes(app, sheet.ref)
        texts = [n.get('value') for n in nodes if n['role'] == 'AXStaticText']
        side = '买入' if intent['side'] == 'buy' else '卖出'
        details = [v for v in texts if isinstance(v, str) and '股东账号:' in v]
        if texts.count(side + '委托') != 1 or len(details) != 1:
            _block('CONFIRMATION_MISMATCH')
        lines = [s.strip() for s in details[0].splitlines()]
        for field in ('股东账号:' + self._attempt['shareholder_scope'], '证券代码:' + intent['symbol'],
                      '报价方式:限价委托', side + '数量:' + str(intent['quantity'])):
            if lines.count(field) != 1:
                _block('CONFIRMATION_MISMATCH')
        prices = [s.removeprefix('当前价格:') for s in lines if s.startswith('当前价格:')]
        if len(prices) != 1 or contracts.decimal_cell(prices[0]) != Decimal(intent['price']):
            _block('CONFIRMATION_MISMATCH')
        self._selector(app, session, dict(cfg['cancel'], enabled=True), root=sheet.ref)
        return self._selector(app, session, dict(cfg['confirm'], enabled=True), root=sheet.ref)

    def confirmation(self, intent):
        self._admit(intent, {'SUBMIT_DISPATCHED'})
        start = self._clock()
        self._sheet(intent)
        self._stage = 'CONFIRMATION_OBSERVED'
        return self._proof(intent, start)

    def press_confirm(self, intent):
        self._admit(intent, {'CONFIRMATION_OBSERVED'})
        self._press(intent, self._sheet(intent), 'CONFIRM_DISPATCHED')

    def reconcile(self, intent, baseline):
        self._thread()
        if self._attempt != self._intent(intent) or self._stage != 'CONFIRM_DISPATCHED':
            _block('BASELINE_OR_INTENT_MISMATCH')
        # Engine hands recursive immutable mappings/tuples, not JSON dict lists.
        def plain(value):
            if isinstance(value, Mapping):
                return {k: plain(v) for k, v in value.items()}
            if isinstance(value, (tuple, list)):
                return [plain(v) for v in value]
            return value
        baseline = plain(baseline)
        if baseline != self._baseline:
            _block('BASELINE_OR_INTENT_MISMATCH')
        result = dict(client_order_id=intent['client_order_id'], status='UNKNOWN', submitted=True,
                      broker_contract_id=None, broker_status=None, symbol=intent['symbol'], side=intent['side'],
                      price=intent['price'], quantity=intent['quantity'], order_date=baseline['broker_trading_day'],
                      baseline_contract_ids=[o['contract_id'] for o in baseline['orders']], retryable=False,
                      independent_fill_verified=False, filled_quantity=None, buy_time=None, buy_price=None)
        self._qualified()
        hint_selector = self.profile['transaction'].get('contract_hint')
        if not hint_selector:
            result['reason'] = 'BROKER_CONTRACT_HINT_AND_INDEPENDENT_FILLS_UNQUALIFIED'
            return result
        app, session, _ = self._connect()
        self._identity(app, session)
        hint = contracts.token(self._selector(app, session, hint_selector).get_value())
        original = session.detect()['page']
        self._original = original
        try:
            capture = self._table('委托')
            app, session, _ = self._connect()
            complete = self._complete(app, session, 'orders')
            headers, rows = self._matrix(capture['raw'])
            after = contracts.normalize_orders(headers, rows, binding=self._binding(),
                        captured_start_ms=capture['start'], captured_end_ms=capture['end'],
                        coverage='COMPLETE_ACCOUNT_ORDERS' if complete else 'AX_EXPOSED_ROWS_ONLY')
            before_orders = tuple(contracts.BrokerOrder(o['contract_id'], o['order_date'], o['order_time'],
                        o['symbol'], o['side'].upper(), Decimal(o['price']), o['quantity'], o['broker_status'],
                        o['reported_filled'], o['reported_remaining'], o['cancelled'],
                        None if o['reported_fill_price'] is None else Decimal(o['reported_fill_price']))
                        for o in baseline['orders'])
            before = contracts.freeze_baseline(contracts.OrdersSnapshot(self._binding(),
                        baseline['captured_start_ms'], baseline['captured_end_ms'], before_orders,
                        'COMPLETE_ACCOUNT_ORDERS' if baseline['orders_complete'] else 'AX_EXPOSED_ROWS_ONLY'))
            typed = contracts.OrderIntent(self._binding(), baseline['broker_trading_day'], intent['symbol'],
                        intent['side'].upper(), Decimal(intent['price']), intent['quantity'])
            receipt = contracts.reconcile(typed, before, after, broker_contract_id=hint, now_ms=self._clock())
            result.update(status={'ORDER_ACCEPTED': 'BROKER_ORDER_VISIBLE_UNFILLED',
                                 'ORDER_CANCELLED': 'BROKER_ORDER_CANCELLED',
                                 'ORDER_REJECTED': 'BROKER_ORDER_REJECTED'}.get(receipt.status, 'UNKNOWN'),
                          broker_contract_id=receipt.broker_contract_id, broker_status=receipt.broker_state,
                          reason=receipt.reason)
            return result
        finally:
            self._restore_preserving_primary(sys.exception())

    def _restore_preserving_primary(self, primary):
        try:
            self.restore()
        except Exception:
            if primary is None:
                raise
            primary.add_note('Native restoration failed')

    def restore(self):
        self._thread()
        if self._cancel_context is not None:
            # No speculative navigation or dismissal over a cancellation sheet.
            from .native_cancel import finish
            finish(self)
        if self._original is None:
            return
        app, session, _ = self._connect()
        self._identity(app, session)
        if self._form_original is not None:
            if not self.config.get('allow_trade', False):
                _block('RESTORE_FORM_POLICY_DISABLED')
            # Never cancel/confirm an outstanding sheet merely to restore fields.
            if any(n['role'] == 'AXSheet' for n in self._nodes(app, session.scope().root)):
                _block('RESTORE_CONFIRMATION_PENDING')
            originals, self._form_original = self._form_original, None
            try:
                for name, value in originals.items():
                    app, session, _ = self._connect()
                    self._identity(app, session)
                    field = self._form_fields(app, session, side=self._attempt['side'])[name]
                    field.set_value(value, verify_timeout=0)
            except BaseException:
                self._poisoned = True
                raise
        if session.detect()['page'] != self._original:
            if not self.config.get('allow_navigation', False):
                _block('RESTORE_NAVIGATION_DISABLED')
            app.backend._switch_page(session, self._original)
        self._identity(app, session)
        if session.detect()['page'] != self._original:
            _block('RESTORE_UNVERIFIED')
        self._original = None
        if self._attempt is not None:
            self._used_ids.add(self._attempt['client_order_id'])
            self._attempt = self._baseline = None
            self._stage = 'NEW'
            self._session_identity = None
