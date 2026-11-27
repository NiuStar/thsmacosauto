"""Pure account-facts builder called by the native observation, not an authority importer."""
from . import business_contracts as bc

def build_account_observation(*, fixed_account, binding, shareholder, positions, orders,
                              broker_day, cash, start, end, now, original_panel,
                              positions_complete, orders_complete):
    bc.day(broker_day)
    if not bc.fresh(start,end,now):raise ValueError('STALE_BASELINE')
    if positions.binding != binding or orders.binding != binding or positions.trading_day != broker_day:
        raise ValueError('ACCOUNT_OR_DAY_MISMATCH')
    if not (start<=positions.captured_start_ms<=positions.captured_end_ms<=end
            and start<=orders.captured_start_ms<=orders.captured_end_ms<=end):
        raise ValueError('CAPTURE_MISMATCH')
    if any(o.trading_day!=broker_day for o in orders.orders):raise ValueError('BROKER_DAY_CHANGED')
    bc.decimal_cell(cash);bc.token(original_panel);bc.token(shareholder)
    if fixed_account is not None:
        scopes={}
        for p in positions.positions:
            if p.market not in bc.MARKET_LABELS or p.shareholder is None:raise ValueError('FIXED_SCOPE_CHANGED')
            market='上海' if p.market in ('沪Ａ','沪A','上海Ａ股','上海A股') else '深圳'
            if market in scopes and scopes[market]!=p.shareholder:raise ValueError('FIXED_SCOPE_CHANGED')
            scopes[market]=p.shareholder
        key=fixed_account.verify_visible(binding.broker,scopes)
        if binding.account_scope!=key or shareholder not in scopes.values():raise ValueError('FIXED_SCOPE_CHANGED')
    # Preserve exposed-row scope. Policy never promotes completeness or freshness.
    return dict(real_identity_count=1,simulation_marker=False,account_scope=binding.account_scope,
        shareholder_scope=shareholder,broker_trading_day=broker_day,cash_available=cash,
        captured_start_ms=start,captured_end_ms=end,original_panel=original_panel,
        positions=[dict(symbol=p.symbol,total_shares=p.total,sellable_shares=p.sellable) for p in positions.positions],
        positions_complete=positions_complete is True and positions.coverage=='COMPLETE_ACCOUNT_POSITIONS',
        orders_complete=orders_complete is True and orders.coverage=='COMPLETE_ACCOUNT_ORDERS',
        orders=[dict(contract_id=o.contract_id,symbol=o.symbol,side=o.side.lower(),price=format(o.price,'f'),
            quantity=o.quantity,order_date=o.trading_day,order_time=o.order_time,broker_status=o.raw_status,
            reported_filled=o.reported_filled,reported_remaining=o.reported_remaining,cancelled=o.cancelled,
            reported_fill_price=None if o.reported_fill_price is None else format(o.reported_fill_price,'f')) for o in orders.orders])
