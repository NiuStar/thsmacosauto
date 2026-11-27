"""Isolated sizing policy only: no network, broker transport or live enable path."""
from decimal import Decimal, InvalidOperation, ROUND_FLOOR

CENT = Decimal('0.01')


def money(value, *, allow_zero=False):
    if not isinstance(value, str):
        raise ValueError('DECIMAL_STRING_REQUIRED')
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError('INVALID_MONEY') from None
    if not number.is_finite() or number < 0 or (not allow_zero and number == 0):
        raise ValueError('INVALID_MONEY')
    if number != number.quantize(CENT):
        raise ValueError('INVALID_PRICE_TICK')
    return number


def size_order(*, last_price, previous_close, upper_limit, lower_limit, available_cash, fee_reserve):
    """Caller must supply verified prices/cash and broker-specific fee reserve.

    Tier uses final limit price. No smaller order on insufficient cash, no
    clipping to upper limit, no fee estimation or treating missing funds as 0.
    A valid size is not a signal, authorization or broker execution preflight.
    """
    last = money(last_price)
    upper = money(upper_limit)
    lower = money(lower_limit)
    cash = money(available_cash, allow_zero=True)
    fee = money(fee_reserve, allow_zero=True)
    if lower > upper or not lower <= last <= upper:
        raise ValueError('INVALID_PRICE_BOUNDS')
    price = last + CENT
    reference = money(previous_close)
    # Exact price comparison, not a rounded display percentage. The caller
    # must verify same-symbol/date exchange previous-close reference upstream.
    threshold = reference * Decimal('1.18')
    if last >= threshold:
        return {'status': 'SKIP', 'reason': 'CURRENT_GAIN_NOT_BELOW_18_PERCENT'}
    if price >= threshold:
        return {'status': 'SKIP', 'reason': 'LIMIT_GAIN_NOT_BELOW_18_PERCENT'}
    if price > upper:
        return {'status': 'SKIP', 'reason': 'LIMIT_PRICE_ABOVE_UPPER_LIMIT'}
    quantity = (int((Decimal('3000') / (price * 100)).to_integral_value(
        rounding=ROUND_FLOOR)) * 100 if price <= Decimal('15') else 100)
    notional = price * quantity
    if notional + fee > cash:
        return {'status': 'SKIP', 'reason': 'INSUFFICIENT_AVAILABLE_CASH'}
    return {'status': 'SIZE_ONLY_NOT_ORDER_READY', 'price': format(price, '.2f'),
            'quantity': quantity, 'notional': format(notional, '.2f'),
            'fee_reserve': format(fee, '.2f'), 'execution_enabled': False}
