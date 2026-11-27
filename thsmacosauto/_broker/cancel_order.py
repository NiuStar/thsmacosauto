"""Single-contract cancellation; broker disappearance is not evidence of cancellation."""
import json
from decimal import Decimal

from . import business_contracts as bc
from .engine import ServiceError, immutable, owned
from .journal import Journal, JournalError, canonical

OPEN_STATES = frozenset({'未成交', '未成', '已报', '已申报', '正常', '部分成交', '部成'})


def matches(row, target):
    return (row.contract_id == target['broker_contract_id'] and row.trading_day == target['order_date']
            and row.symbol == target['symbol'] and row.side == target['side'].upper()
            and row.price == Decimal(target['price']) and row.quantity == target['quantity'])


def cancel(engine, request):
    engine._require_execution()
    rid = request['client_order_id']
    original = engine.journal.lookup(request['order_request_id'])
    if original is None:
        raise ServiceError('ORDER_NOT_FOUND', 404)
    intent = json.loads(original['payload'])
    receipt = json.loads(original['response'])
    if (original['source'] != 'manual' or type(intent) is not dict or set(intent) != engine.MANUAL_FIELDS
            or original['status'] != 'BROKER_ORDER_VISIBLE_UNFILLED' or receipt.get('submitted') is not True
            or receipt.get('broker_contract_id') != request['broker_contract_id']):
        raise ServiceError('CANCEL_TARGET_MISMATCH')
    scope = json.loads(original['scope']) if original['scope'] is not None else None
    if (type(scope) is not dict or type(scope.get('intent')) is not dict
            or scope['intent'].get('client_order_id') != request['order_request_id']
            or scope.get('shareholder_scope') != intent['shareholder_scope']
            or scope.get('broker_trading_day') != receipt.get('order_date')
            or any(receipt.get(k) != intent[k] for k in ('symbol', 'side', 'price', 'quantity'))):
        raise ServiceError('CANCEL_TARGET_MISMATCH')
    target = immutable({**request, 'account_scope': scope['account_scope'],
                        'shareholder_scope': scope['shareholder_scope'], 'order_date': scope['broker_trading_day'],
                        **{key: intent[key] for key in ('symbol', 'side', 'price', 'quantity')}})

    def result(status, reason, *, attempted=False, state=None):
        data = Journal.response(rid, status, reason)
        data.update(operation='CANCEL', order_request_id=request['order_request_id'],
                    broker_contract_id=target['broker_contract_id'], broker_status=state,
                    cancel_attempted=attempted, cancel_verified=status == 'BROKER_ORDER_CANCELLED')
        return data

    def snapshot():
        raw = owned(backend.observe_cancel(target))
        start, end = raw['captured_start_ms'], raw['captured_end_ms']
        if (raw.get('real_identity_count') != 1 or type(raw.get('real_identity_count')) is not int
                or raw.get('simulation_marker') is not False or raw.get('orders_complete') is not True
                or not bc.fresh(start, end, engine._clock())
                or raw.get('account_scope') != target['account_scope']
                or raw.get('broker_trading_day') != target['order_date']
                or raw.get('shareholder_scope') != target['shareholder_scope']):
            raise ServiceError('CANCEL_TARGET_MISMATCH')
        orders = [o for o in raw['orders'] if o['contract_id'] == target['broker_contract_id']]
        if len(orders) != 1:
            raise ServiceError('CANCEL_TARGET_MISMATCH')
        o = orders[0]
        row = bc.BrokerOrder(o['contract_id'], o['order_date'], o['order_time'], o['symbol'],
                             o['side'].upper(), bc.decimal_cell(o['price']), o['quantity'], o['broker_status'],
                             o.get('reported_filled'), o.get('reported_remaining'), o.get('cancelled'),
                             bc.decimal_cell(o['reported_fill_price']) if o.get('reported_fill_price') is not None else None)
        if not matches(row, target):
            raise ServiceError('CANCEL_TARGET_MISMATCH')
        return row, start, end

    # Use the existing durable single-owner ledger without a migration. The exact
    # payload shape and operation tag keep cancel receipts out of order adoption.
    try:
        replay = engine.journal.claim(rid, 'manual', request)
    except JournalError as exc:
        raise ServiceError(str(exc)) from None
    if replay is not None:
        return replay
    touched = False
    backend = None
    terminal = None
    try:
        if engine._fatal:
            raise ServiceError('ENGINE_FENCED')
        backend = engine._get_backend()
        row, start, end = snapshot()
        verdict = bc.state_verdict(row)
        if verdict == 'ORDER_CANCELLED':
            terminal = result('BROKER_ORDER_CANCELLED', 'ALREADY_CANCELLED', state=row.raw_status)
        elif verdict != 'ORDER_ACCEPTED' or row.raw_status not in OPEN_STATES:
            terminal = result('BLOCKED', 'ORDER_NOT_CANCELLABLE', state=row.raw_status)
        else:
            engine.journal.update(rid, result('SUBMITTING', 'CANCEL_ENTRY_MAY_HAVE_OPENED'),
                                  scope=canonical(dict(target)))
            engine._active = {'source': 'manual', 'generation': engine._control['generation'],
                              'state': 'SUBMITTING', 'start': start, 'end': end}
            backend.set_before_press(engine._before_press)
            touched = True
            engine._press('open_cancel', target)
            engine.journal.update(rid, result('CONFIRMING', 'CANCEL_MAY_HAVE_OCCURRED', attempted=True))
            engine._active['state'] = 'CONFIRMING'
            engine._press('press_cancel', target)
            after, _, _ = snapshot()
            if bc.state_verdict(after) == 'ORDER_CANCELLED':
                terminal = result('BROKER_ORDER_CANCELLED', 'EXACT_CONTRACT_CANCELLED',
                                  attempted=True, state=after.raw_status)
            else:
                terminal = result('UNKNOWN', 'CANCEL_OUTCOME_UNVERIFIED', attempted=True)
    except Exception as exc:
        reason = exc.code if isinstance(exc, ServiceError) else 'BACKEND_OR_EVIDENCE_FAILURE'
        terminal = result('UNKNOWN' if touched else 'BLOCKED', reason, attempted=touched)
    finally:
        try:
            if backend is not None:
                backend.restore()
        except Exception:
            terminal = result('UNKNOWN' if touched else 'BLOCKED', 'RESTORE_FAILED', attempted=touched)
        finally:
            engine._active = None
            if terminal is None:
                terminal = result('UNKNOWN', 'OWNER_UNEXPECTED_EXIT', attempted=touched)
            try:
                engine.journal.update(rid, terminal)
            except Exception:
                engine._fatal = True
                terminal = result('UNKNOWN', 'JOURNAL_PERSISTENCE_FAILED', attempted=touched)
    return terminal


def qualified_profile(profile):
    """Only reviewed source-owned selectors may opt a client build in."""
    cfg = profile.get('cancel')
    return (type(cfg) is dict and set(cfg) == {'qualified', 'entry', 'sheet', 'detail', 'confirm'}
            and cfg['qualified'] is True and all(type(cfg[k]) is dict and cfg[k]
                                               for k in ('entry', 'sheet', 'detail', 'confirm'))
            and cfg['entry'].get('role') == 'AXButton' and cfg['entry'].get('title') == '撤单'
            and cfg['confirm'].get('role') == 'AXButton' and cfg['confirm'].get('title') == '确认'
            and cfg['sheet'].get('role') == 'AXSheet' and cfg['detail'].get('role') == 'AXStaticText')
