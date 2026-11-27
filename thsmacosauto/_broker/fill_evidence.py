"""Independent AX trade observations, NEVER execution or funding-identity authority.

The current native source has no qualified fill admission. Even matching REAL
rows remain REFERENCE_ONLY/PARTIAL_SCOPE, with public execution fills null.
Producer timestamps are audit metadata; only original caller capture intervals
consume the unchanged 5000ms chain window. No clock reads, state writes or GUI.
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, localcontext
from fractions import Fraction
import hashlib
import json
import math
import re

from . import business_contracts as b

REQUIRED = ('成交日期', '成交时间', '证券代码', '操作', '成交数量', '成交均价', '成交金额', '合同编号', '成交编号')
MAX_ROWS = 5000
MAX_COLUMNS = 32
MAX_CELL = 256


def sha(value):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('INVALID_SOURCE_SHA')


def clock(value):
    if type(value) is not str or not re.fullmatch(r'[0-9]{2}:[0-9]{2}:[0-9]{2}', value):
        raise ValueError('INVALID_TRADE_TIME')
    datetime.strptime(value, '%H:%M:%S')
    return value


def positive(value):
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise ValueError('INVALID_TRADE_DECIMAL')


@dataclass(frozen=True)
class ScopeRecord:
    """Explicit producer reference, NOT account/funding/native qualification.

    The holdings capture observes market/holder associations; account_scope is
    configured context. Neither its digest nor domain='REAL' attests identity.
    """
    binding: b.AccountBinding
    producer_kind: str
    source_sha256: str
    captured_start_ms: int
    captured_end_ms: int
    market_holders: tuple[tuple[str, str], ...]

    def __post_init__(self):
        if type(self.binding) is not b.AccountBinding or self.producer_kind != 'POSITION_HOLDER_REFERENCE_V1':
            raise ValueError('INVALID_SCOPE_PRODUCER')
        sha(self.source_sha256)
        b.times(self.captured_start_ms, self.captured_end_ms)
        if type(self.market_holders) is not tuple or not self.market_holders:
            raise ValueError('EXPLICIT_HOLDER_REFERENCE_REQUIRED')
        seen = set()
        for pair in self.market_holders:
            if type(pair) is not tuple or len(pair) != 2:
                raise ValueError('INVALID_HOLDER_REFERENCE')
            market, holder = pair
            if market not in b.MARKET_LABELS or market in seen:
                raise ValueError('INVALID_HOLDER_REFERENCE')
            b.token(holder)
            seen.add(market)


@dataclass(frozen=True)
class Trade:
    trade_id: str
    contract_id: str
    trading_day: str
    trade_time: str
    symbol: str
    side: str
    quantity: int
    price: Decimal
    amount: Decimal

    def __post_init__(self):
        b.token(self.trade_id)
        b.token(self.contract_id)
        b.day(self.trading_day)
        clock(self.trade_time)
        b.symbol(self.symbol)
        if self.side not in ('BUY', 'SELL') or type(self.quantity) is not int or not 0 < self.quantity <= 10**12:
            raise ValueError('INVALID_TRADE_QUANTITY_OR_SIDE')
        positive(self.price)
        positive(self.amount)
        # 成交均价 may be display-rounded. No schema-qualified exact identity
        # amount == quantity * price (nor any fee formula) has been established.


def parse_rows(headers, rows):
    if (type(headers) is not list or not 0 < len(headers) <= MAX_COLUMNS
            or any(type(h) is not str or len(h) > MAX_CELL for h in headers)
            or type(rows) is not list or len(rows) > MAX_ROWS):
        raise ValueError('TRADE_TABLE_BOUND_EXCEEDED')
    if any(type(row) is not list or any(type(v) is not str or len(v) > MAX_CELL for v in row) for row in rows):
        raise ValueError('TRADE_CELL_BOUND_EXCEEDED')
    parsed = b.table(headers, rows, REQUIRED)
    out, ids = [], set()
    for row in parsed:
        if row['操作'] not in ('买入', '卖出'):
            raise ValueError('INVALID_TRADE_SIDE')
        t = Trade(row['成交编号'], row['合同编号'], row['成交日期'], row['成交时间'], row['证券代码'],
                  {'买入': 'BUY', '卖出': 'SELL'}[row['操作']], b.shares(row['成交数量']),
                  b.decimal_cell(row['成交均价']), b.decimal_cell(row['成交金额']))
        if t.trade_id in ids:
            raise ValueError('DUPLICATE_TRADE_ID')
        ids.add(t.trade_id)
        out.append(t)
    return tuple(out)


def digest(headers, rows):
    return hashlib.sha256(json.dumps({'headers': list(headers), 'matrix': [list(r) for r in rows]},
                                    ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class TradesSnapshot:
    scope: ScopeRecord
    captured_start_ms: int
    captured_end_ms: int
    raw_captured_at: str
    elapsed_seconds: int | float
    raw_sha256: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    trades: tuple[Trade, ...]
    coverage: str = field(default='AX_EXPOSED_ROWS_ONLY', init=False)
    native_fill_qualified: bool = field(default=False, init=False)

    def __post_init__(self):
        if type(self.scope) is not ScopeRecord:
            raise ValueError('SCOPE_RECORD_REQUIRED')
        b.times(self.captured_start_ms, self.captured_end_ms)
        if type(self.raw_captured_at) is not str or len(self.raw_captured_at) > 64:
            raise ValueError('INVALID_PRODUCER_TIMESTAMP')
        stamp = datetime.fromisoformat(self.raw_captured_at)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError('INVALID_PRODUCER_TIMESTAMP')
        if (type(self.elapsed_seconds) not in (int, float) or not math.isfinite(self.elapsed_seconds)
                or self.elapsed_seconds < 0):
            raise ValueError('INVALID_PRODUCER_ELAPSED')
        if (type(self.headers) is not tuple or type(self.rows) is not tuple
                or any(type(r) is not tuple for r in self.rows) or type(self.trades) is not tuple):
            raise ValueError('IMMUTABLE_TRADES_REQUIRED')
        sha(self.raw_sha256)
        if digest(self.headers, self.rows) != self.raw_sha256:
            raise ValueError('NATIVE_CONTENT_SHA_MISMATCH')
        expected = parse_rows(list(self.headers), [list(r) for r in self.rows])
        if self.trades != expected or any(type(t) is not Trade for t in self.trades):
            raise ValueError('TRADE_MATRIX_MISMATCH')


def normalize_native_trades(raw, *, scope, captured_start_ms, captured_end_ms):
    if type(raw) is not dict or raw.get('status') != 'READ_OK':
        raise ValueError('NATIVE_READ_NOT_OK')
    if (raw.get('stable_double_read') is not True or raw.get('row_column_crosscheck') is not True
            or raw.get('completeness_scope') != 'AX_EXPOSED_ROWS_ONLY'):
        raise ValueError('NATIVE_SCOPE_OR_STABILITY_UNAVAILABLE')
    headers, rows = raw.get('headers'), raw.get('rows')
    if (type(headers) is not list or type(rows) is not list or len(rows) > MAX_ROWS
            or not 0 < len(headers) <= MAX_COLUMNS or any(type(h) is not str or len(h) > MAX_CELL for h in headers)):
        raise ValueError('INVALID_NATIVE_TRADE_SCHEMA')
    if (type(raw.get('row_count')) is not int or raw['row_count'] != len(rows)
            or type(raw.get('column_count')) is not int or raw['column_count'] != len(headers)):
        raise ValueError('NATIVE_DIMENSION_MISMATCH')
    matrix = []
    for row in rows:
        if type(row) is not dict or set(row) != set(headers):
            raise ValueError('NATIVE_ROW_KEYS_MISMATCH')
        matrix.append([row[h] for h in headers])
    trades = parse_rows(headers, matrix)
    return TradesSnapshot(scope, captured_start_ms, captured_end_ms, raw.get('captured_at'),
                          raw.get('elapsed_seconds'), raw.get('sha256'), tuple(headers),
                          tuple(tuple(r) for r in matrix), trades)


def scope_payload(scope):
    return dict(binding=dict(domain=scope.binding.domain, broker=scope.binding.broker,
                             account_scope=scope.binding.account_scope), producer_kind=scope.producer_kind,
                source_sha256=scope.source_sha256, captured_start_ms=scope.captured_start_ms,
                captured_end_ms=scope.captured_end_ms, market_holders=[list(p) for p in scope.market_holders])


def trade_payload(snapshot):
    return [dict(trade_id=t.trade_id, contract_id=t.contract_id, symbol=t.symbol,
                 name=dict(zip(snapshot.headers, row)).get('证券名称'), side={'BUY':'买入', 'SELL':'卖出'}[t.side],
                 quantity=t.quantity, price=format(t.price, 'f'), amount=format(t.amount, 'f'),
                 trade_date=t.trading_day, trade_time=t.trade_time) for t, row in zip(snapshot.trades, snapshot.rows)]


def evidence_payload(snapshot):
    return dict(schema='AX_TRADE_REFERENCE_V1', scope=scope_payload(snapshot.scope),
                raw_captured_at=snapshot.raw_captured_at, elapsed_seconds=snapshot.elapsed_seconds,
                raw_sha256=snapshot.raw_sha256, headers=list(snapshot.headers), rows=[list(r) for r in snapshot.rows],
                coverage=snapshot.coverage, native_fill_qualified=False, independent_fill_verified=False)


def normalize_trade_read(report):
    """Checked readonly read('trades') projection → immutable independent rows."""
    if type(report) is not dict:
        raise ValueError('TRADE_READ_REQUIRED')
    e = report.get('fill_evidence')
    if (type(e) is not dict or e.get('schema') != 'AX_TRADE_REFERENCE_V1'
            or e.get('coverage') != 'AX_EXPOSED_ROWS_ONLY' or e.get('native_fill_qualified') is not False
            or e.get('independent_fill_verified') is not False):
        raise ValueError('UNQUALIFIED_TRADE_REFERENCE_REQUIRED')
    s = e['scope']
    scope = ScopeRecord(b.AccountBinding(**s['binding']), s['producer_kind'], s['source_sha256'],
                        s['captured_start_ms'], s['captured_end_ms'], tuple(tuple(p) for p in s['market_holders']))
    headers, matrix = e['headers'], e['rows']
    b.table(headers, matrix, REQUIRED)
    snapshot = normalize_native_trades(dict(status='READ_OK', headers=headers,
                rows=[dict(zip(headers, r)) for r in matrix], row_count=len(matrix), column_count=len(headers),
                sha256=e['raw_sha256'], captured_at=e['raw_captured_at'], elapsed_seconds=e['elapsed_seconds'],
                stable_double_read=report['table']['stable_double_read'],
                row_column_crosscheck=report['table']['row_column_crosscheck'],
                completeness_scope=report['completeness_scope']), scope=scope,
                captured_start_ms=report['captured_start_ms'], captured_end_ms=report['captured_end_ms'])
    if (report.get('execution_enabled') is not False or report.get('panel') != '成交'
            or report.get('status') != scope.binding.domain + '_TRADES_READ_ONLY'
            or report['table']['sha256'] != snapshot.raw_sha256 or report.get('trades') != trade_payload(snapshot)
            or report['table']['row_count'] != len(matrix) or report['table']['column_count'] != len(headers)):
        raise ValueError('TRADE_READ_PROJECTION_MISMATCH')
    return snapshot


@dataclass(frozen=True)
class FillReference:
    status: str
    reason: str
    observed_partial_quantity: int | None = None
    observed_weighted_price: Decimal | None = None
    observed_time_range: tuple[str, str] | None = None
    observed_trade_ids: tuple[str, ...] = ()
    observed_weighted_price_ratio: tuple[int, int] | None = None
    coverage: str = field(default='AX_EXPOSED_ROWS_ONLY', init=False)
    independent_fill_verified: bool = field(default=False, init=False)
    execution_enabled: bool = field(default=False, init=False)
    retryable: bool = field(default=False, init=False)
    filled_quantity: None = field(default=None, init=False)
    buy_time: None = field(default=None, init=False)
    buy_price: None = field(default=None, init=False)


def reconcile_fills(intent, before, after, *, orders_before, orders_after, broker_contract_id,
                    shareholder, intent_bound_ms, now_ms):
    """Reference matching only. Does not clear UNKNOWN or mutate order receipts.

    Full visible quantity is still a partial-scope observation, never a full-fill
    proof. An empty/mismatched result is unavailable (None), not zero fills.
    """
    if (type(intent) is not b.OrderIntent or type(before) is not TradesSnapshot or type(after) is not TradesSnapshot
            or type(orders_before) is not b.OrderBaseline or type(orders_after) is not b.OrdersSnapshot):
        raise ValueError('TYPED_FILL_RECONCILIATION_REQUIRED')
    b.token(broker_contract_id)
    b.token(shareholder)
    if type(intent_bound_ms) is not int or type(now_ms) is not int:
        raise ValueError('INVALID_CHAIN_CLOCK')
    unknown = lambda reason: FillReference('UNKNOWN', reason)
    if before.scope.binding != intent.binding or after.scope.binding != intent.binding:
        return unknown('ACCOUNT_OR_DOMAIN_MISMATCH')
    if before.scope.market_holders != after.scope.market_holders:
        return unknown('SCOPE_REFERENCE_CHANGED')
    region = ('沪Ａ', '沪A', '上海Ａ股', '上海A股') if intent.symbol.startswith(('5', '6')) else ('深Ａ', '深A', '深圳Ａ股', '深圳A股')
    holders = {h for m, h in after.scope.market_holders if m in region}
    if holders != {shareholder}:
        return unknown('SHAREHOLDER_REFERENCE_MISMATCH')
    captures = ((before.scope.captured_start_ms, before.scope.captured_end_ms),
                (after.scope.captured_start_ms, after.scope.captured_end_ms),
                (before.captured_start_ms, before.captured_end_ms),
                (orders_before.snapshot.captured_start_ms, orders_before.snapshot.captured_end_ms),
                (orders_after.captured_start_ms, orders_after.captured_end_ms),
                (after.captured_start_ms, after.captured_end_ms))
    if not all(b.fresh(start, end, now_ms) for start, end in captures):
        return unknown('STALE_OR_FUTURE_CHAIN')
    if not (before.scope.captured_end_ms <= before.captured_start_ms
            and after.scope.captured_end_ms <= after.captured_start_ms
            and max(before.captured_end_ms, orders_before.snapshot.captured_end_ms) <= intent_bound_ms
            <= orders_after.captured_start_ms and orders_after.captured_end_ms <= after.captured_start_ms):
        return unknown('CAPTURE_OR_INTENT_ORDER_INVALID')
    receipt = b.reconcile(intent, orders_before, orders_after, broker_contract_id=broker_contract_id, now_ms=now_ms)
    if receipt.status not in ('ORDER_ACCEPTED', 'ORDER_CANCELLED'):
        return unknown('NO_UNIQUE_MATCHING_ORDER_REFERENCE')
    prior = {t.trade_id: t for t in before.trades}
    current = {t.trade_id: t for t in after.trades}
    if any(current.get(tid) != t for tid, t in prior.items()):
        return unknown('BASELINE_TRADE_DISAPPEARED_OR_CHANGED')
    order = next(o for o in orders_after.orders if o.contract_id == broker_contract_id)
    matches = []
    for t in after.trades:
        if t.contract_id != broker_contract_id:
            continue
        if t.trade_id in prior:
            return unknown('CONTRACT_ALREADY_IN_FILL_BASELINE')
        if (t.trading_day, t.symbol, t.side) != (intent.trading_day, intent.symbol, intent.side):
            return unknown('TRADE_INTENT_MISMATCH')
        if t.trade_time < order.order_time:
            return unknown('TRADE_PRECEDES_ORDER')
        if (intent.side == 'BUY' and t.price > intent.limit_price) or (intent.side == 'SELL' and t.price < intent.limit_price):
            return unknown('TRADE_LIMIT_CONTRADICTION')
        matches.append(t)
    if not matches:
        return unknown('NO_NEW_MATCHING_TRADE')
    quantity = sum(t.quantity for t in matches)
    # Match the existing broker-state grammar; unavailable counts stay unavailable.
    # Only explicit remaining/cancelled counts constrain the observed quantity.
    if (order.raw_status in ('未成交', '未成', '已报', '已申报', '正常')
            or quantity > intent.quantity or (order.reported_filled is not None and quantity > order.reported_filled)
            or quantity + sum(v for v in (order.reported_remaining, order.cancelled) if v is not None) > intent.quantity):
        return unknown('TRADE_QUANTITY_CONTRADICTION')
    exact = sum((Fraction(t.price) * t.quantity for t in matches), Fraction(0)) / quantity
    with localcontext() as ctx:
        ctx.prec = 100  # display quotient; exact rational retained separately
        weighted = Decimal(exact.numerator) / Decimal(exact.denominator)
    stamps = [t.trading_day + 'T' + t.trade_time for t in matches]
    return FillReference('REFERENCE_ONLY', 'MATCHED_ROWS_PARTIAL_SCOPE_NOT_EXECUTION_PROOF', quantity,
                         weighted, (min(stamps), max(stamps)), tuple(t.trade_id for t in matches),
                         (exact.numerator, exact.denominator))
