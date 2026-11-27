"""User-approved buy-only sizing: fees unknown and not strategy-priced.

No execution or permission attestation. Keep the legacy fee-inclusive helper
unchanged for audit compatibility; the fixed adapter below checks notional only.
The zero argument is an internal arithmetic term, not a claim of zero fees.
Broker cash enforcement/rejections and UNKNOWN no-retry remain mandatory.
"""
from .order_sizing import size_order


def size_buy(*, last_price, previous_close, upper_limit, lower_limit, available_cash):
    value=size_order(last_price=last_price,previous_close=previous_close,
                     upper_limit=upper_limit,lower_limit=lower_limit,
                     available_cash=available_cash,fee_reserve='0.00')
    return {**value,'fee_reserve':None,'fee_status':'UNKNOWN_NOT_PRICED_BY_USER_POLICY',
            'cash_check_basis':'NOTIONAL_ONLY','automatic_sell_enabled':False,
            'execution_enabled':False}
