"""Native single-row cancellation. No coordinate, row-selection or batch fallback."""
from decimal import Decimal

from .._ax import read_table
from .._ax.query import QueryScope
from . import business_contracts as bc
from .cancel_order import OPEN_STATES, matches, qualified_profile
from .login_result import modal_census, Unrecognized
from .native_errors import _block


def _continuity(owner, app, session):
    identity, root, front, _ = owner._cancel_context
    if (owner._owned_effect(owner._runtime.app_identity) != identity
            or app.backend.key(session.scope().root) != root
            or owner._owned_effect(owner._runtime.front) != front):
        _block('CANCEL_CONTEXT_CHANGED')


def _orders(owner, app, session):
    start = owner._clock()
    _continuity(owner, app, session)
    owner._identity(app, session)
    panel = session.detect()
    if panel['page'] != '委托' or not owner._complete(app, session, 'orders'):
        _block('INCOMPLETE_ORDER_SCOPE')
    table = panel['table']
    key = app.backend.key
    before = tuple(key(r) for r in owner._get(table, 'AXRows'))
    raw = read_table(app.backend, table, timeout=3)
    rows = tuple(owner._get(table, 'AXRows'))
    after = session.detect()
    day = owner._day(app, session)
    if (tuple(map(key, rows)) != before or len(rows) != raw['row_count'] or
            after['page'] != '委托' or key(after['table']) != key(table) or
            not owner._complete(app, session, 'orders')):
        _block('CANCEL_CONTEXT_CHANGED')
    _continuity(owner, app, session)
    headers, data = owner._matrix(raw)
    orders = bc.normalize_orders(headers, data, binding=owner._binding(), captured_start_ms=start,
                                captured_end_ms=owner._clock(), coverage='COMPLETE_ACCOUNT_ORDERS')
    if any(o.trading_day != day for o in orders.orders):
        _block('BROKER_DAY_CHANGED')
    return day, orders.orders, rows


def observe(owner, target):
    owner._thread()
    if owner.config.get('allow_trade') is not True or owner.config.get('allow_navigation') is not True:
        _block('NATIVE_TRADE_DISABLED')
    if not qualified_profile(owner.profile):
        _block('NATIVE_CANCEL_PROFILE_UNQUALIFIED')
    start = owner._clock()
    app, session, identity = owner._connect()
    owner._qualified()
    root = session.scope().root
    front = owner._owned_effect(owner._runtime.front)
    if not owner._navigation_modal_clear(app, session, identity, root, front):
        _block('CANCEL_CONTEXT_CHANGED')
    if owner._cancel_context is None:
        if owner._attempt is not None or owner._original is not None:
            _block('NATIVE_SESSION_UNCERTAIN')
        owner._cancel_context = (identity, app.backend.key(root), front, dict(target))
        pc = owner._table('持仓')
        _continuity(owner, app, session)
        owner._scoped_position_rows(*owner._matrix(pc['raw']))
        scope, holders = owner._scope_config()
        market = '上海' if target['symbol'].startswith(('5', '6')) else '深圳'
        if scope != target['account_scope'] or holders.get(market) != target['shareholder_scope']:
            _block('CANCEL_TARGET_MISMATCH')
        owner._table('委托')
    if dict(target) != owner._cancel_context[3]:
        _block('CANCEL_TARGET_MISMATCH')
    app, session, _ = owner._connect()
    day, orders, _ = _orders(owner, app, session)
    return dict(account_scope=owner._scope_config()[0], shareholder_scope=target['shareholder_scope'],
                broker_trading_day=day, real_identity_count=1, simulation_marker=False,
                orders_complete=True, orders=[owner._order(o) for o in orders],
                captured_start_ms=start, captured_end_ms=owner._clock())


def _find(app, root, selector):
    return QueryScope(app, root, leaf_roles=('AXTable', 'AXMenu', 'AXMenuBar')).find(**selector)


def _sheet(owner, app, session, target):
    root = session.scope().root
    _, modals = modal_census(owner, app, root)
    cfg = owner.profile['cancel']
    sheet = _find(app, root, cfg['sheet'])
    root_key = app.backend.key(root)
    if len(modals) != 1 or modals[0][1:5] != (app.backend.key(sheet.ref), 'AXSheet', root_key, root_key):
        _block('CANCEL_CONFIRMATION_MISMATCH')
    text = _find(app, sheet.ref, cfg['detail']).get_value()
    side = '买入' if target['side'] == 'buy' else '卖出'
    expected = {'撤单委托', '合同编号:' + target['broker_contract_id'], '委托日期:' + target['order_date'],
                '证券代码:' + target['symbol'], '方向:' + side, '委托数量:' + str(target['quantity']),
                '委托价格:' + format(Decimal(target['price']), '.2f')}
    lines = text.splitlines() if type(text) is str else []
    if len(lines) != len(expected) or set(lines) != expected:
        _block('CANCEL_CONFIRMATION_MISMATCH')
    action = _find(app, sheet.ref, {**cfg['confirm'], 'enabled': True})
    if app.backend.actions(action.ref) != ['AXPress']:
        _block('CANCEL_CONFIRMATION_MISMATCH')
    return sheet.ref, action.ref


def _require_stage(owner, target, stage):
    if (owner._cancel_context is None or dict(target) != owner._cancel_context[3]
            or owner._cancel_stage != stage or owner._before_press is None):
        _block('CANCEL_TARGET_MISMATCH')


def open_dialog(owner, target):
    _require_stage(owner, target, 'READY')
    app, session, identity = owner._connect()
    root = session.scope().root
    key = app.backend.key

    def entry():
        if not owner._navigation_modal_clear(app, session, identity, root, owner._cancel_context[2]):
            _block('CANCEL_CONTEXT_CHANGED')
        day, orders, rows = _orders(owner, app, session)
        candidates = [i for i, order in enumerate(orders) if matches(order, target)
                      and order.raw_status in OPEN_STATES and bc.state_verdict(order) == 'ORDER_ACCEPTED']
        if day != target['order_date'] or len(candidates) != 1:
            _block('CANCEL_TARGET_MISMATCH')
        row = rows[candidates[0]]
        btn = _find(app, row, {**owner.profile['cancel']['entry'], 'enabled': True})
        # Prove the active control belongs exclusively to this row, not to a
        # toolbar aliased into the row subtree or a different order row.
        child = btn.ref
        roles = []
        for _ in range(3):
            parent = owner._get(child, 'AXParent')
            if (parent is None or owner._get(parent, 'AXSubrole') == 'AXSecureTextField'
                    or sum(key(ref) == key(child) for ref in owner._get(parent, 'AXChildren') or ()) != 1):
                _block('CANCEL_CONTROL_UNQUALIFIED')
            if key(parent) == key(row):
                break
            roles.append(owner._get(parent, 'AXRole'))
            child = parent
        else:
            _block('CANCEL_CONTROL_UNQUALIFIED')
        if roles not in ([], ['AXCell'], ['AXStaticText', 'AXCell']):
            _block('CANCEL_CONTROL_UNQUALIFIED')
        if app.backend.actions(btn.ref) != ['AXPress']:
            _block('CANCEL_CONTROL_UNQUALIFIED')
        return row, btn.ref

    first = entry()
    second = entry()
    if tuple(map(key, first)) != tuple(map(key, second)):
        _block('CANCEL_CONTEXT_CHANGED')
    _continuity(owner, app, session)
    # Entry is qualified only as an exact-row control that opens confirmation.
    owner._cancel_stage = 'ENTRY_DISPATCHED'
    app.backend.press_with_permit(second[1], owner._before_press)


def press(owner, target):
    _require_stage(owner, target, 'ENTRY_DISPATCHED')
    app, session, _ = owner._connect()
    key = app.backend.key
    first = _sheet(owner, app, session, target)
    second = _sheet(owner, app, session, target)
    if tuple(map(key, first)) != tuple(map(key, second)):
        _block('CANCEL_CONFIRMATION_MISMATCH')
    _continuity(owner, app, session)
    owner._cancel_stage = 'CONFIRM_DISPATCHED'
    app.backend.press_with_permit(second[1], owner._before_press)


def finish(owner):
    app, session, _ = owner._connect()
    _continuity(owner, app, session)
    try:
        _, modals = modal_census(owner, app, session.scope().root)
    except Unrecognized:
        _block('RESTORE_CONFIRMATION_PENDING')
    if modals:
        _block('RESTORE_CONFIRMATION_PENDING')
    owner._cancel_context = None
    owner._cancel_stage = 'READY'
