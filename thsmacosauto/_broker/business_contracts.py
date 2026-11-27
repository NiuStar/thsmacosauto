"""Pure, immutable THS business data. Never execution or identity attestation."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from decimal import Decimal
import re

from .order_sizing import size_order
from .buy_only_policy import size_buy

# Exact labels from account-observation/src/broker/account_observation.rs:market.
MARKET_LABELS = frozenset(('深Ａ', '深A', '深圳Ａ股', '深圳A股', '沪Ａ', '沪A', '上海Ａ股', '上海A股'))


def token(value):
    if type(value) is not str or not value or value != value.strip() or any(ch.isspace() for ch in value):
        raise ValueError('INVALID_IDENTITY')
    return value


def day(value):
    if type(value) is not str or not re.fullmatch(r'20[0-9]{6}', value):
        raise ValueError('INVALID_TRADING_DAY')
    datetime.strptime(value, '%Y%m%d')
    return value


def symbol(value):
    if type(value) is not str or not re.fullmatch(r'[0-9]{6}', value):
        raise ValueError('INVALID_SYMBOL')
    return value


def decimal_cell(value):
    """Explicit unsigned UI grammar, including checked thousands grouping."""
    if type(value) is not str or len(value) > 40 or not re.fullmatch(
            r'(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?', value):
        raise ValueError('INVALID_DECIMAL_CELL')
    return Decimal(value.replace(',', ''))


def normalize_available_cash(value):
    """Exact observed 可用金额 only; no source/identity attestation or zero fallback."""
    return decimal_cell(value)


def shares(value):
    result = decimal_cell(value)
    if result != result.to_integral_value():
        raise ValueError('NONINTEGRAL_SHARES')
    return int(result)


def times(start, end):
    if type(start) is not int or type(end) is not int or not 0 <= start <= end:
        raise ValueError('INVALID_CAPTURE_INTERVAL')


def table(headers, rows, required):
    if (type(headers) is not list or not headers or any(type(h) is not str or not h or h != h.strip() for h in headers)
            or len(set(headers)) != len(headers) or type(rows) is not list
            or not set(required) <= set(headers)):
        raise ValueError('INVALID_TABLE_HEADERS')
    if any(type(row) is not list or len(row) != len(headers) or any(type(v) is not str for v in row) for row in rows):
        raise ValueError('INVALID_TABLE_ROW')
    return tuple(dict(zip(headers, row)) for row in rows)


@dataclass(frozen=True)
class AccountBinding:
    """Caller-supplied observed broker/single-account scope, NOT a funding ID."""
    domain: str
    broker: str
    account_scope: str

    def __post_init__(self):
        if self.domain not in ('REAL', 'SYNTHETIC', 'SIM', 'UNKNOWN'):
            raise ValueError('INVALID_DOMAIN')
        token(self.broker)
        token(self.account_scope)


@dataclass(frozen=True)
class Position:
    symbol: str
    total: int
    sellable: int
    cost: Decimal
    market: str | None
    shareholder: str | None

    def __post_init__(self):
        symbol(self.symbol)
        if (type(self.total) is not int or type(self.sellable) is not int
                or not 0 <= self.sellable <= self.total):
            raise ValueError('INCOHERENT_POSITION')
        if type(self.cost) is not Decimal or not self.cost.is_finite() or self.cost < 0:
            raise ValueError('INVALID_POSITION_COST')
        if self.market is not None and self.market not in MARKET_LABELS:
            raise ValueError('UNSUPPORTED_MARKET_LABEL')
        if self.shareholder is not None:
            token(self.shareholder)


@dataclass(frozen=True)
class PositionsSnapshot:
    binding: AccountBinding
    trading_day: str
    captured_start_ms: int
    captured_end_ms: int
    positions: tuple[Position, ...]
    coverage: str = 'AX_EXPOSED_ROWS_ONLY'

    def __post_init__(self):
        if type(self.binding) is not AccountBinding or type(self.positions) is not tuple:
            raise ValueError('IMMUTABLE_POSITIONS_REQUIRED')
        day(self.trading_day)
        times(self.captured_start_ms, self.captured_end_ms)
        if self.coverage not in ('AX_EXPOSED_ROWS_ONLY', 'COMPLETE_ACCOUNT_POSITIONS'):
            raise ValueError('INVALID_COVERAGE')
        seen = set()
        for p in self.positions:
            if type(p) is not Position or (p.symbol, p.market, p.shareholder) in seen:
                raise ValueError('INVALID_OR_DUPLICATE_POSITION')
            seen.add((p.symbol, p.market, p.shareholder))


def normalize_positions(headers, rows, *, binding, trading_day, captured_start_ms, captured_end_ms):
    if type(binding) is not AccountBinding:
        raise ValueError('BINDING_REQUIRED')
    day(trading_day)
    times(captured_start_ms, captured_end_ms)
    parsed = table(headers, rows, ('证券代码', '股票余额', '可用余额', '成本价'))
    out, seen = [], set()
    for row in parsed:
        code = symbol(row['证券代码'])
        total, sellable = shares(row['股票余额']), shares(row['可用余额'])
        market = token(row['交易市场']) if '交易市场' in row else None
        if market is not None and market not in MARKET_LABELS:
            raise ValueError('UNSUPPORTED_MARKET_LABEL')
        holder = token(row['股东账户']) if '股东账户' in row else None
        key = (code, market, holder)
        if key in seen or sellable > total:
            raise ValueError('INCOHERENT_POSITION')
        seen.add(key)
        out.append(Position(code, total, sellable, decimal_cell(row['成本价']), market, holder))
    return PositionsSnapshot(binding, trading_day, captured_start_ms, captured_end_ms, tuple(out))


@dataclass(frozen=True)
class OrderIntent:
    binding: AccountBinding
    trading_day: str
    symbol: str
    side: str
    limit_price: Decimal
    quantity: int
    order_type: str = 'LIMIT'

    def __post_init__(self):
        if type(self.binding) is not AccountBinding or self.binding.domain not in ('REAL', 'SYNTHETIC'):
            raise ValueError('ACTIONABLE_BINDING_REQUIRED')
        day(self.trading_day)
        symbol(self.symbol)
        if self.side not in ('BUY', 'SELL') or self.order_type != 'LIMIT':
            raise ValueError('EXACT_LIMIT_SIDE_REQUIRED')
        p = self.limit_price
        if (type(p) is not Decimal or not p.is_finite() or p <= 0 or p > Decimal('1000000')
                or p % Decimal('0.01') != 0):
            raise ValueError('EXACT_CENT_LIMIT_REQUIRED')
        if type(self.quantity) is not int or not 0 < self.quantity <= 10**12:
            raise ValueError('POSITIVE_INTEGER_QUANTITY_REQUIRED')
        if self.side == 'BUY' and self.quantity % 100:
            raise ValueError('BUY_BOARD_LOT_REQUIRED')


@dataclass(frozen=True)
class BrokerOrder:
    contract_id: str
    trading_day: str
    order_time: str
    symbol: str
    side: str
    price: Decimal
    quantity: int
    raw_status: str | None
    reported_filled: int | None
    reported_remaining: int | None
    cancelled: int | None
    reported_fill_price: Decimal | None

    def __post_init__(self):
        token(self.contract_id)
        day(self.trading_day)
        symbol(self.symbol)
        if type(self.order_time) is not str or not re.fullmatch(r'[0-9]{2}:[0-9]{2}:[0-9]{2}', self.order_time):
            raise ValueError('INVALID_ORDER_TIME')
        datetime.strptime(self.order_time, '%H:%M:%S')
        if self.side not in ('BUY', 'SELL') or type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError('INVALID_BROKER_ORDER')
        if type(self.price) is not Decimal or not self.price.is_finite() or self.price <= 0:
            raise ValueError('INVALID_BROKER_PRICE')
        if self.raw_status is not None and type(self.raw_status) is not str:
            raise ValueError('INVALID_BROKER_STATE')
        values = (self.reported_filled, self.reported_remaining, self.cancelled)
        if any(v is not None and (type(v) is not int or not 0 <= v <= self.quantity) for v in values):
            raise ValueError('INCOHERENT_QUANTITIES')
        if sum(v for v in values if v is not None) > self.quantity:
            raise ValueError('INCOHERENT_QUANTITIES')
        if self.reported_fill_price is not None and (type(self.reported_fill_price) is not Decimal
                or not self.reported_fill_price.is_finite() or self.reported_fill_price < 0):
            raise ValueError('INVALID_BROKER_FILL_PRICE')


@dataclass(frozen=True)
class OrdersSnapshot:
    binding: AccountBinding
    captured_start_ms: int
    captured_end_ms: int
    orders: tuple[BrokerOrder, ...]
    coverage: str = 'AX_EXPOSED_ROWS_ONLY'

    def __post_init__(self):
        if type(self.binding) is not AccountBinding or type(self.orders) is not tuple:
            raise ValueError('IMMUTABLE_ORDERS_REQUIRED')
        times(self.captured_start_ms, self.captured_end_ms)
        if self.coverage not in ('AX_EXPOSED_ROWS_ONLY', 'COMPLETE_ACCOUNT_ORDERS'):
            raise ValueError('INVALID_COVERAGE')
        if any(type(o) is not BrokerOrder for o in self.orders):
            raise ValueError('BROKER_ORDER_REQUIRED')
        if len(set(o.contract_id for o in self.orders)) != len(self.orders):
            raise ValueError('DUPLICATE_CONTRACT')


def aliases(row, names):
    found = [row[k] for k in names if k in row]
    if len(set(found)) > 1:
        raise ValueError('CONFLICTING_COLUMNS')
    return found[0] if found else None


def normalize_orders(headers, rows, *, binding, captured_start_ms, captured_end_ms,
                     coverage='AX_EXPOSED_ROWS_ONLY'):
    if type(binding) is not AccountBinding:
        raise ValueError('BINDING_REQUIRED')
    times(captured_start_ms, captured_end_ms)
    if coverage not in ('AX_EXPOSED_ROWS_ONLY', 'COMPLETE_ACCOUNT_ORDERS'):
        raise ValueError('INVALID_COVERAGE')
    parsed = table(headers, rows, ('委托日期', '委托时间', '证券代码', '操作', '委托数量', '委托价格', '合同编号'))
    out, ids = [], set()
    for row in parsed:
        contract = token(row['合同编号'])
        if contract in ids:
            raise ValueError('DUPLICATE_CONTRACT')
        ids.add(contract)
        date = day(row['委托日期'])
        clock = row['委托时间']
        if not re.fullmatch(r'[0-9]{2}:[0-9]{2}:[0-9]{2}', clock):
            raise ValueError('INVALID_ORDER_TIME')
        datetime.strptime(clock, '%H:%M:%S')
        code = symbol(row['证券代码'])
        if row['操作'] not in ('买入', '卖出'):
            raise ValueError('INVALID_BROKER_SIDE')
        side = {'买入': 'BUY', '卖出': 'SELL'}[row['操作']]
        qty, price = shares(row['委托数量']), decimal_cell(row['委托价格'])
        if not qty or price <= 0:
            raise ValueError('INVALID_ORDER_AMOUNT')
        state = aliases(row, ('备注', '委托状态'))
        filled = shares(row['成交数量']) if '成交数量' in row else None
        remainder = aliases(row, ('未成交数量', '剩余数量'))
        remaining = shares(remainder) if remainder is not None else None
        cancelled = shares(row['撤销数量']) if '撤销数量' in row else None
        # Missing/dash display fill price is explicitly unavailable, not zero fills.
        fp = row.get('成交价格')
        fill_price = None if fp in (None, '', '--', '-') else decimal_cell(fp)
        if any(v is not None and v > qty for v in (filled, remaining, cancelled)):
            raise ValueError('INCOHERENT_QUANTITIES')
        if sum(v for v in (filled, remaining, cancelled) if v is not None) > qty:
            raise ValueError('INCOHERENT_QUANTITIES')
        out.append(BrokerOrder(contract, date, clock, code, side, price, qty, state,
                               filled, remaining, cancelled, fill_price))
    return OrdersSnapshot(binding, captured_start_ms, captured_end_ms, tuple(out), coverage)


@dataclass(frozen=True)
class OrderBaseline:
    snapshot: OrdersSnapshot
    contract_ids: frozenset[str]

    def __post_init__(self):
        if (type(self.snapshot) is not OrdersSnapshot or type(self.snapshot.orders) is not tuple
                or type(self.contract_ids) is not frozenset
                or self.contract_ids != frozenset(o.contract_id for o in self.snapshot.orders)
                or len(self.contract_ids) != len(self.snapshot.orders)):
            raise ValueError('INVALID_BASELINE')
        for contract in self.contract_ids:
            token(contract)


def freeze_baseline(snapshot):
    if type(snapshot) is not OrdersSnapshot:
        raise ValueError('ORDERS_SNAPSHOT_REQUIRED')
    return OrderBaseline(snapshot, frozenset(o.contract_id for o in snapshot.orders))


@dataclass(frozen=True)
class Receipt:
    status: str
    reason: str
    domain: str
    broker_contract_id: str | None = None
    broker_state: str | None = None
    filled_quantity: None = None
    buy_time: None = None
    buy_price: None = None
    retryable: bool = False
    execution_enabled: bool = False


def fresh(start, end, now, max_age_ms=5000):
    times(start, end)
    if type(now) is not int or type(max_age_ms) is not int or not 0 < max_age_ms <= 5000:
        raise ValueError('INVALID_FRESHNESS_CLOCK')
    return end <= now and 0 <= now - start <= max_age_ms


def state_verdict(order):
    """Exact known broker states only; status ≠ independent fill evidence."""
    s, q = order.raw_status, order.quantity
    f, r, x, fp = order.reported_filled, order.reported_remaining, order.cancelled, order.reported_fill_price
    if f is not None and f > 0 and fp == 0:
        return None
    if s in ('未成交', '未成', '已报', '已申报', '正常'):
        if f not in (None, 0) or r not in (None, q) or x not in (None, 0) or fp not in (None, Decimal(0)):
            return None
        return 'ORDER_ACCEPTED'
    if s in ('已成', '全部成交', '完全成交'):
        if f not in (None, q) or r not in (None, 0) or x not in (None, 0) or fp == 0:
            return None
        return 'ORDER_ACCEPTED'
    if s in ('部分成交', '部成'):
        if ((f is not None and not 0 < f < q) or (r is not None and not 0 < r < q)
                or x not in (None, 0) or fp == 0):
            return None
        if f is not None and r is not None and f + r != q:
            return None
        return 'ORDER_ACCEPTED'
    if s in ('已撤', '已撤销'):
        # Require explicit cancellation evidence, not just a terminal label.
        # The historical 11-column schema omits f/r: x == q suffices without
        # filling those missing fields. Partial cancellation requires f + x == q.
        if r not in (None, 0) or x is None or x <= 0:
            return None
        if (f is None and x != q) or (f is not None and f + x != q):
            return None
        return 'ORDER_CANCELLED'
    if s in ('已废', '废单'):
        if f not in (None, 0) or r not in (None, 0) or fp not in (None, Decimal(0)):
            return None
        return 'ORDER_REJECTED'
    return None


def reconcile(intent, before, after, *, broker_contract_id, now_ms, max_age_ms=5000):
    if type(intent) is not OrderIntent or type(before) is not OrderBaseline or type(after) is not OrdersSnapshot:
        raise ValueError('TYPED_RECONCILIATION_INPUT_REQUIRED')
    unknown = lambda why: Receipt('UNKNOWN', why, intent.binding.domain)
    token(broker_contract_id)
    if before.snapshot.binding != intent.binding or after.binding != intent.binding:
        return unknown('ACCOUNT_OR_DOMAIN_MISMATCH')
    if before.snapshot.coverage != 'COMPLETE_ACCOUNT_ORDERS' or after.coverage != 'COMPLETE_ACCOUNT_ORDERS':
        return unknown('INCOMPLETE_ORDER_SCOPE')
    if not fresh(after.captured_start_ms, after.captured_end_ms, now_ms, max_age_ms):
        return unknown('STALE_OR_FUTURE_CAPTURE')
    if not fresh(before.snapshot.captured_start_ms, before.snapshot.captured_end_ms,
                 now_ms, max_age_ms):
        return unknown('STALE_BASELINE')
    if before.snapshot.captured_end_ms > after.captured_start_ms:
        return unknown('CAPTURE_ORDER_INVALID')
    after_ids = frozenset(o.contract_id for o in after.orders)
    if not before.contract_ids <= after_ids:
        return unknown('BASELINE_IDS_DISAPPEARED')
    candidates = [o for o in after.orders if o.contract_id not in before.contract_ids
                  and (o.trading_day, o.symbol, o.side, o.price, o.quantity) ==
                  (intent.trading_day, intent.symbol, intent.side, intent.limit_price, intent.quantity)]
    if len(candidates) > 1:
        return unknown('AMBIGUOUS_NEW_ORDERS')
    if not candidates or candidates[0].contract_id != broker_contract_id:
        return unknown('NO_UNIQUE_NEW_CONTRACT_MATCH')
    order = candidates[0]
    verdict = state_verdict(order)
    if verdict is None:
        return unknown('UNPROVEN_OR_INCOHERENT_BROKER_STATE')
    return Receipt(verdict, 'UNIQUE_NEW_MATCHING_BROKER_ORDER', intent.binding.domain,
                   order.contract_id, order.raw_status)


def blocked(reason):
    return {'status': 'BLOCKED', 'reason': reason, 'execution_enabled': False}


def automatic_sell_capability():
    return blocked('SELL_RULE_NOT_CONFIGURED')


def position_scope_check(intent, positions, now_ms):
    if type(intent) is not OrderIntent or type(positions) is not PositionsSnapshot:
        raise ValueError('TYPED_POSITION_INPUT_REQUIRED')
    if positions.binding != intent.binding or positions.trading_day != intent.trading_day:
        return 'ACCOUNT_DAY_OR_DOMAIN_MISMATCH'
    if not fresh(positions.captured_start_ms, positions.captured_end_ms, now_ms):
        return 'STALE_OR_FUTURE_CAPTURE'
    return None


def validate_manual_sell(intent, positions, *, now_ms):
    reason = position_scope_check(intent, positions, now_ms)
    if reason:
        return blocked(reason)
    if intent.side != 'SELL':
        return blocked('MANUAL_SELL_INTENT_REQUIRED')
    matches = [p for p in positions.positions if p.symbol == intent.symbol]
    if len(matches) != 1:
        return blocked('POSITION_MISSING_OR_AMBIGUOUS')
    if intent.quantity > matches[0].sellable:
        return blocked('INSUFFICIENT_SELLABLE')
    return {'status': 'BUSINESS_CHECK_ONLY', 'reason': 'EXPLICIT_SELLABLE_SUFFICIENT',
            'execution_enabled': False}


def size_automatic_buy(intent, positions, *, now_ms, last_price, previous_close,
                       upper_limit, lower_limit, available_cash):
    """Conservative preflight only; quotes/cash provenance remains upstream."""
    reason = position_scope_check(intent, positions, now_ms)
    if reason:
        return blocked(reason)
    if intent.side != 'BUY' or not intent.symbol.startswith(('300', '301')):
        return blocked('CHINEXT_BUY_ONLY')
    if any(p.symbol == intent.symbol and p.total > 0 for p in positions.positions):
        return blocked('NO_ADD_TO_EXISTING_POSITION')
    # Current AX pages cannot establish absence; deliberately no scope upgrade API.
    if positions.coverage != 'COMPLETE_ACCOUNT_POSITIONS':
        return blocked('INCOMPLETE_POSITION_SCOPE')
    prices = dict(last_price=last_price, previous_close=previous_close,
                  upper_limit=upper_limit, lower_limit=lower_limit, available_cash=available_cash)
    prices = {name: format(decimal_cell(value), 'f') for name, value in prices.items()}
    result = size_buy(**prices)
    if result['status'] == 'SIZE_ONLY_NOT_ORDER_READY' and (
            Decimal(result['price']) != intent.limit_price or result['quantity'] != intent.quantity):
        return blocked('INTENT_SIZING_MISMATCH')
    return result


NATIVE_POSITION_HEADERS = (
    '证券代码', '证券名称', '市价', '盈亏', '当日盈亏', '浮动盈亏比(%)', '实际数量',
    '股票余额', '可用余额', '冻结数量', '成本价', '市值', '仓位占比(%)', '交易市场', '股东账户')


@dataclass(frozen=True)
class NativePositions:
    positions: PositionsSnapshot
    raw_captured_at: str
    raw_sha256: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


def normalize_native_positions(raw, *, binding, trading_day, captured_start_ms, captured_end_ms):
    """pymacauto 0.4.0 tables.read_table dict-rows → strict matrix contracts.

    captured_at is the producer's END timestamp, never an earliest-time substitute.
    The caller must preserve its capture-start/end interval from the native host.
    """
    if type(raw) is not dict or raw.get('status') != 'READ_OK':
        raise ValueError('NATIVE_READ_NOT_OK')
    if (raw.get('row_column_crosscheck') is not True or raw.get('stable_double_read') is not True
            or raw.get('completeness_scope') != 'AX_EXPOSED_ROWS_ONLY'):
        raise ValueError('NATIVE_SCOPE_OR_STABILITY_UNAVAILABLE')
    headers, raw_rows = raw.get('headers'), raw.get('rows')
    if type(headers) is not list or tuple(headers) != NATIVE_POSITION_HEADERS or type(raw_rows) is not list:
        raise ValueError('NATIVE_POSITION_SCHEMA_MISMATCH')
    if (type(raw.get('row_count')) is not int or raw['row_count'] != len(raw_rows)
            or type(raw.get('column_count')) is not int or raw['column_count'] != len(headers)):
        raise ValueError('NATIVE_DIMENSION_MISMATCH')
    matrix = []
    for row in raw_rows:
        if type(row) is not dict or set(row) != set(headers):
            raise ValueError('NATIVE_ROW_KEYS_MISMATCH')
        matrix.append([row[h] for h in headers])
    table(headers, matrix, NATIVE_POSITION_HEADERS)
    canonical = json.dumps({'headers': headers, 'matrix': matrix}, ensure_ascii=False,
                           separators=(',', ':')).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    if raw.get('sha256') != digest:
        raise ValueError('NATIVE_CONTENT_SHA_MISMATCH')
    stamp = raw.get('captured_at')
    if type(stamp) is not str:
        raise ValueError('NATIVE_CAPTURE_TIME_UNAVAILABLE')
    parsed = datetime.fromisoformat(stamp)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('NATIVE_CAPTURE_TIME_UNAVAILABLE')
    elapsed = raw.get('elapsed_seconds')
    if type(elapsed) not in (float, int) or not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError('NATIVE_ELAPSED_UNAVAILABLE')
    snapshot = normalize_positions(headers, matrix, binding=binding, trading_day=trading_day,
                                   captured_start_ms=captured_start_ms, captured_end_ms=captured_end_ms)
    return NativePositions(snapshot, stamp, digest, tuple(headers), tuple(tuple(r) for r in matrix))
