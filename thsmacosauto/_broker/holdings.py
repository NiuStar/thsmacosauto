"""Holdings observations and account-scope proof. No refresh or login actions."""
from . import business_contracts as contracts
from .native_errors import _block


def table_report(capture):
    raw = capture['raw']
    return {'status': 'READ_OK', 'execution_enabled': False, 'panel': capture['panel'],
              'captured_start_ms': capture['start'], 'captured_end_ms': capture['end'],
              'completeness_scope': 'AX_EXPOSED_ROWS_ONLY', 'account_or_history_complete': False,
              'query_filters': 'CURRENT_UI_FILTERS_NOT_CHANGED', 'cross_table_atomic': False,
              'table': {k: raw[k] for k in ('row_count', 'column_count', 'sha256', 'stable_double_read',
                                           'row_column_crosscheck', 'completeness_scope')}}


def scoped_position_rows(owner, headers, rows, *, diagnostic=False, display_only=False):
    parsed = contracts.table(headers, rows, ('证券代码', '股票余额', '可用余额', '成本价', '交易市场', '股东账户'))
    if display_only and owner._fixed_account is None:
        _block('FIXED_POLICY_UNAVAILABLE')
    scope=owner._verify_fixed_rows(((r['交易市场'], r['股东账户']) for r in parsed),diagnostic=diagnostic or display_only)
    if display_only and scope['scope_status'] not in ('FIXED_SCOPE_COMPLETE_OBSERVATION','FIXED_SCOPE_INCOMPLETE_OBSERVATION'):
        _block('FIXED_SCOPE_CHANGED')
    _, holders = owner._scope_config()
    if not parsed and not (diagnostic or display_only):
        _block('SHAREHOLDER_SCOPE_UNPROVEN_EMPTY_POSITIONS')
    scoped, symbols = [], set()
    for row in parsed:
        market = row['交易市场']
        region = '上海' if market in ('沪Ａ', '沪A', '上海Ａ股', '上海A股') else '深圳'
        if market not in contracts.MARKET_LABELS or (not diagnostic and holders.get(region) != row['股东账户']):
            _block('SHAREHOLDER_SCOPE_MISMATCH')
        symbol = contracts.symbol(row['证券代码'])
        total, sellable = contracts.shares(row['股票余额']), contracts.shares(row['可用余额'])
        if sellable > total or symbol in symbols:
            _block('INCOHERENT_OR_DUPLICATE_POSITION')
        symbols.add(symbol)
        number = lambda key: None if key not in row else format(contracts.decimal_cell(row[key]), 'f')
        scoped.append({'symbol': symbol, 'name': row.get('证券名称'), 'total': total, 'available': sellable,
                       'cost': number('成本价'), 'market_value': number('市值'), 'current_price': number('市价'),
                       'today_qty': None, 'yesterday_qty': None})
    return (scoped,scope) if display_only else scope if diagnostic else scoped


def read(owner, kind):
    display_only = kind in ('positions-panel', 'positions-panel-summary')
    capture = owner._table(require_current='持仓') if display_only else owner._table('持仓')
    headers, rows = owner._matrix(capture['raw'])
    if kind == 'positions-scope-diagnostic':
        if owner._fixed_account is None:
            _block('FIXED_POLICY_UNAVAILABLE')
        scope = scoped_position_rows(owner, headers, rows, diagnostic=True)
        if owner._clock() - capture['start'] > 15000:
            _block('STALE_READ_CAPTURE')
        return dict(status='POSITIONS_SCOPE_DIAGNOSTIC_READ_ONLY', **scope, observed_row_count=len(rows),
                    completeness_scope='AX_EXPOSED_ROWS_ONLY', account_or_history_complete=False,
                    execution_enabled=False)
    report = table_report(capture)
    if display_only:
        visible, scope = scoped_position_rows(owner, headers, rows, display_only=True)
        # Do not export a raw-table hash derived in part from holder IDs.
        report['table'].pop('sha256')
        report.update(status='POSITIONS_PANEL_READ_ONLY', mode=owner._runtime.evidence_domain.lower(),
                      freshness_basis='CLIENT_TABLE_ONLY', upstream_freshness='UNVERIFIED', broker_as_of_ms=None,
                      captured_at_ms=capture['start'], positions=visible, positions_complete=False, **scope)
    else:
        report.update(account_scope=owner._scope_config()[0], positions=scoped_position_rows(owner, headers, rows),
                      positions_complete=False)
        domain = owner._runtime.evidence_domain
        report.update(status=domain + '_POSITIONS_READ_ONLY', mode=domain.lower(), captured_at_ms=capture['start'])
    if owner._clock() - capture['start'] > 15000:
        _block('STALE_READ_CAPTURE')
    if kind == 'positions-panel-summary':
        positions = report.pop('positions')
        report.update(status='HOLDINGS_SUMMARY_READ_ONLY', observed_row_count=len(positions),
                      held_symbol_count=sum(p['total'] > 0 for p in positions),
                      sellable_symbol_count=sum(p['available'] > 0 for p in positions))
    return report
