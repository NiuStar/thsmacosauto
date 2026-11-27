"""Holdings refresh control discovery, one-shot dispatch and receipt state."""
import copy
import threading
import time

from . import readonly_budget as rb
from .native_errors import _block


class HoldingsRefresh:
    def __init__(self, backend):
        self.backend = backend
        self._refresh_claimed = False
        self._refresh_request = self._refresh_receipt = self._refresh_proof = self._refresh_token = None

    def receipt(self):
        return copy.deepcopy(self._refresh_receipt)

    def _refresh_observe(self):
        """Current holdings only; no navigation, setters, values in discovery scan."""
        from .._ax import read_table
        app, session, identity = self.backend._connect()
        front = self.backend._owned_effect(self.backend._runtime.front)
        self.backend._identity(app, session)
        panel = session.detect()
        if panel['page'] != '持仓':
            _block('REFRESH_HOLDINGS_REQUIRED')
        if not self.backend.config.get('allow_navigation', False):
            _block('REFRESH_DISABLED')
        if not self.backend._navigation_modal_clear(app, session, identity, session.scope().root, front):
            _block('REFRESH_STRUCTURE_UNPROVEN')
        button, root = self._refresh_scan(app, session, panel)
        raw = read_table(app.backend, panel['table'])
        headers, rows = self.backend._matrix(raw)
        self.backend._scoped_position_rows(headers, rows)
        after = session.detect()
        self.backend._identity(app, session)
        if (after['page'] != '持仓' or after['headers'] != panel['headers']
                or app.backend.key(after['table']) != app.backend.key(panel['table'])
                or self.backend._owned_effect(self.backend._runtime.app_identity) != identity
                or self.backend._owned_effect(self.backend._runtime.front) != front):
            _block('NATIVE_CONTINUITY_CHANGED')
        final_button, final_root = self._refresh_scan(app, session, after)
        if (final_button != button or final_root != root
                or not self.backend._navigation_modal_clear(app, session, identity, session.scope().root, front)):
            _block('REFRESH_CONTROL_CHANGED')
        return dict(app=app, session=session, identity=identity, front=front,
                    root=root, table=app.backend.key(panel['table']),
                    button=button, table_summary={'sha256':raw['sha256'],'row_count':raw['row_count']})

    def _refresh_scan(self, app, session, panel):
        root = session.scope().root
        queue, seen, buttons, table_parents = [(root, None, 0)], set(), [], []
        while queue:
            rb.check()
            ref, parent, depth = queue.pop(0)
            key = app.backend.key(ref)
            if key in seen:
                _block('REFRESH_STRUCTURE_UNPROVEN')
            seen.add(key)
            if len(seen) > 512 or depth > 16:
                _block('AX_TRAVERSAL_LIMIT')
            subrole = self.backend._get(ref, 'AXSubrole')
            if subrole == 'AXSecureTextField':
                continue
            role = self.backend._get(ref, 'AXRole')
            if role in ('AXMenu', 'AXMenuBar', 'AXTextField', 'AXTextArea'):
                continue
            if role in ('AXSheet', 'AXDialog'):
                _block('REFRESH_STRUCTURE_UNPROVEN')
            if role == 'AXTable':
                if key == app.backend.key(panel['table']):
                    table_parents.append(parent)
                continue
            if role in ('AXList', 'AXOutline'):
                continue
            if role == 'AXButton':
                labels = {a:self.backend._get(ref, a) for a in ('AXTitle','AXDescription','AXHelp')}
                allowed = ('刷新', '刷新持仓', '持仓刷新', 'Refresh', 'Refresh holdings')
                values = [v for v in labels.values() if v not in (None, '')]
                if values and all(type(v) is str and v in allowed for v in values):
                    buttons.append(dict(ref=ref, key=key, parent=parent, labels=labels,
                        enabled=self.backend._get(ref,'AXEnabled') is True,
                        actions=list(app.backend.actions(ref))))
                continue
            children = self.backend._get(ref, 'AXChildren') or []
            if not isinstance(children, (list, tuple)):
                _block('REFRESH_STRUCTURE_UNPROVEN')
            queue.extend((child, key, depth+1) for child in children)
        if len(table_parents) != 1 or table_parents[0] is None:
            _block('REFRESH_STRUCTURE_UNPROVEN')
        candidates = [b for b in buttons if b['parent'] == table_parents[0]]
        if len(candidates) != 1:
            _block('REFRESH_CONTROL_UNQUALIFIED')
        button = candidates[0]
        if not button['enabled'] or 'AXPress' not in button['actions']:
            _block('REFRESH_CONTROL_UNQUALIFIED')
        return button, app.backend.key(root)

    def discover(self):
        if self._refresh_claimed:
            return dict(status='REFRESH_ALREADY_CLAIMED', one_shot=True,
                        receipt=self.receipt(), execution_enabled=False)
        proof = self._refresh_observe()
        old = self._refresh_proof
        if old is not None and not self._refresh_same(old, proof):
            self._refresh_token = None
        if self._refresh_token is None:
            import secrets
            self._refresh_token = secrets.token_hex(32)
        self._refresh_proof = proof
        self._refresh_discovered_ns = time.monotonic_ns()
        return dict(status='REFRESH_CONTROL_QUALIFIED', control_fingerprint=self._refresh_token,
                    control={'role':'AXButton', **proof['button']['labels'], 'enabled':True,
                             'supported_action':'AXPress','association':'DIRECT_SIBLING_OF_HOLDINGS_TABLE'},
                    table={'row_count':proof['table_summary']['row_count']}, token_ttl_seconds=30, one_shot=True,
                    query_performed=False, upstream_freshness='UNVERIFIED', broker_as_of=None,
                    execution_enabled=False)

    @staticmethod
    def _refresh_same(left, right):
        return (all(left[k] == right[k] for k in ('identity','front','root','table'))
                and all(left['button'][k] == right['button'][k]
                        for k in ('key','parent','labels','enabled','actions')))

    def dispatch(self, payload, *, budget):
        rb.require(budget)
        if threading.get_ident() != self.backend._owner:
            _block('GUI_OWNER_THREAD_MISMATCH')
        if self._refresh_claimed:
            if (payload['request_id'], payload['control_fingerprint']) == self._refresh_request:
                return copy.deepcopy(self._refresh_receipt)
            _block('REFRESH_ALREADY_CLAIMED')
        if self.backend._poisoned:
            _block('NATIVE_SESSION_UNCERTAIN')
        if (self._refresh_token is None or payload['control_fingerprint'] != self._refresh_token
                or time.monotonic_ns()-self._refresh_discovered_ns > 30_000_000_000):
            _block('REFRESH_TOKEN_INVALID')
        # Consume BEFORE observations/dispatch. Every subsequent error is terminal;
        # no rediscovery or retry can mint a second action in this owner lifetime.
        self._refresh_claimed = True
        self._refresh_request = (payload['request_id'], payload['control_fingerprint'])
        self._refresh_receipt = dict(status='REFRESH_UNKNOWN', request_id=payload['request_id'],
            dispatch_attempted=False, dispatch_acknowledged=False, retry_allowed=False,
            query_completion='UNVERIFIED', upstream_freshness='UNVERIFIED', broker_as_of=None,
            execution_enabled=False, monetary_authority='NONE')
        receipt = self._refresh_receipt
        try:
            with rb.at('CONTINUITY'):
                budget.check()
                proof = self._refresh_observe()
                if not self._refresh_same(self._refresh_proof, proof):
                    _block('REFRESH_CONTROL_CHANGED')
                receipt['before_table'] = {'row_count':proof['table_summary']['row_count']}
                def permit():
                    # No AX observations between final lease proof and native input.
                    budget.check()
                    if time.monotonic_ns()-self._refresh_discovered_ns > 30_000_000_000:
                        _block('REFRESH_TOKEN_INVALID')
                    receipt['dispatch_attempted'] = True
                    budget.set(dispatch_attempted=True)
                    return True
                proof['app'].backend.press_with_permit(proof['button']['ref'], permit)
                receipt['dispatch_acknowledged'] = True
                after = self._refresh_observe()
                if not self._refresh_same(proof, after):
                    _block('REFRESH_CONTROL_CHANGED')
                budget.check()
                receipt['after_table'] = {'row_count':after['table_summary']['row_count']}
                receipt['table_observation'] = ('CLIENT_TABLE_UNCHANGED'
                    if proof['table_summary'] == after['table_summary'] else 'CLIENT_TABLE_CHANGED')
                receipt['status'] = 'REFRESH_DISPATCH_ACKNOWLEDGED'
        except BaseException:
            receipt['status'] = 'REFRESH_UNKNOWN' if receipt['dispatch_attempted'] else 'REFRESH_BLOCKED'
            self.backend._poisoned = receipt['dispatch_attempted']
            budget.set(poisoned=self.backend._poisoned)
            raise
        finally:
            self._refresh_proof = None
            self._refresh_token = None
        return copy.deepcopy(receipt)
