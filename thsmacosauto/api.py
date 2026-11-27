"""Standalone loopback service; all native calls use one existing Broker worker."""

from pathlib import Path
import re
from types import MappingProxyType

from ._broker import engine, gui_ownership, readonly_budget, server
from ._broker.private_policy_read import literal

LOGIN = '/v1/login'
LOGIN_STATUS = '/v1/login/status'
OPERATOR = '/v1/operator/trade-page'  # Legacy action envelope.
HOLDINGS = '/v1/holdings'
HOLDINGS_REFRESH = HOLDINGS + '/refresh'
REFRESH = '/v1/real/holdings-refresh'
REFRESH_FIELDS = frozenset({'request_id', 'control_fingerprint'})
REFRESH_STATUS = frozenset({HOLDINGS_REFRESH + '/status', REFRESH + '-status'})
TABLE_READS = MappingProxyType({
    HOLDINGS: 'positions-panel',
    HOLDINGS + '/summary': 'positions-panel-summary',
    '/v1/real/positions-panel': 'positions-panel',
    '/v1/real/positions': 'positions',
    '/v1/real/positions-scope-diagnostic': 'positions-scope-diagnostic',
    HOLDINGS_REFRESH + '/controls': 'holdings-refresh-controls',
    REFRESH + '-controls': 'holdings-refresh-controls',
    '/v1/real/native-current-panel': 'current',
    '/v1/real/account-observation': 'account',
    '/v1/account/available-cash': 'available-cash',
    '/v1/real/form-discovery': 'form-discovery',
    '/v1/real/orders': 'orders',
    '/v1/real/trades': 'trades',
})
LOOKUPS = MappingProxyType({
    HOLDINGS + '/lookup': (HOLDINGS, 'symbol', r'[0-9]{6}', 'positions', 'position'),
    '/v1/real/orders/lookup': ('/v1/real/orders', 'contract_id', server.REQUEST_ID_PATTERN, 'orders', 'order'),
    '/v1/real/trades/lookup': ('/v1/real/trades', 'contract_id', server.REQUEST_ID_PATTERN, 'trades', 'trades'),
})
READS = frozenset({'/v1/health', '/v1/capabilities', LOGIN_STATUS, *REFRESH_STATUS, *TABLE_READS, *LOOKUPS})
POSTS = MappingProxyType({LOGIN: frozenset(), OPERATOR: frozenset({'action'}),
                          HOLDINGS_REFRESH: REFRESH_FIELDS, REFRESH: REFRESH_FIELDS})


class Handler(server.RequestHandler):
    get_paths = READS
    post_keys = POSTS
    get_queries = MappingProxyType({path: spec[1:3] for path, spec in LOOKUPS.items()})


def _lookup_result(report, spec, value):
    _, key, _, source_rows, output = spec
    matches = [row for row in report[source_rows] if row[key] == value]
    if output != 'trades' and len(matches) > 1:
        raise engine.ServiceError('BACKEND_UNAVAILABLE', 503)
    result = {k: report[k] for k in (
        'mode', 'panel', 'captured_start_ms', 'captured_end_ms', 'captured_at_ms',
        'query_filters', 'scope_status', 'observed_market_count', 'missing_market_count',
        'positions_complete', 'freshness_basis', 'upstream_freshness', 'broker_as_of_ms',
    ) if k in report}
    result.update(status='VISIBLE_MATCH_FOUND' if matches else 'NOT_FOUND_IN_VISIBLE_SCOPE',
                  match_count=len(matches), completeness_scope='AX_EXPOSED_ROWS_ONLY',
                  account_or_history_complete=False, execution_enabled=False)
    result[key] = value
    result[output] = matches if output == 'trades' else next(iter(matches), None)
    if output == 'trades':
        result.update(native_fill_qualified=False, independent_fill_verified=False)
    return result


class ReadOnlyEngine(engine.Engine):
    READS = TABLE_READS

    def _run_refresh(self, operation):
        budget = readonly_budget.ReadBudget(read_only=True)
        def bound():
            with readonly_budget.bind(budget):
                budget.check()
                return operation(budget)
        return self._run_budgeted(bound, None, [False, {}], budget)

    def _refresh_status(self, _budget):
        refresh = getattr(self._backend, 'holdings_refresh', None)
        receipt = refresh.receipt() if refresh is not None else None
        return receipt or {'status': 'REFRESH_NOT_CLAIMED', 'execution_enabled': False}

    def dispatch(self, method, path, payload=None):
        route, _, query = path.partition('?')
        if method == 'GET' and route in LOOKUPS:
            spec = LOOKUPS[route]
            try:
                value = server.single_query(query, *spec[1:3])[spec[1]]
            except server.Rejected as exc:
                raise engine.ServiceError(exc.code, 400) from None
            # The source read owns capture, validation and panel restoration.
            return _lookup_result(super().dispatch(method, spec[0]), spec, value)
        if method == 'GET' and path in READS:
            if path == LOGIN_STATUS:
                return self._run_operator(lambda backend, budget: backend.login_status(budget=budget))
            if path in REFRESH_STATUS:
                return self._run_refresh(self._refresh_status)
            if path == '/v1/capabilities':
                return {'status': 'READ_ONLY', 'execution_enabled': False,
                        'get': sorted(READS), 'post': sorted(POSTS),
                        'navigation_enabled': self.navigation_enabled,
                        'login_secret_configured': self.login_secret_configured,
                        'native_readiness': 'NOT_CHECKED'}
            return super().dispatch(method, path, payload)
        if method == 'POST' and path == LOGIN:
            if type(payload) is not dict or payload:
                raise engine.ServiceError('INVALID_REQUEST', 400)
            return self._run_operator(lambda backend, budget: backend.login(budget=budget))
        if method == 'POST' and path in (HOLDINGS_REFRESH, REFRESH):
            if (type(payload) is not dict or payload.keys() != REFRESH_FIELDS
                    or any(type(payload[k]) is not str or re.fullmatch(pattern, payload[k]) is None
                           for k, pattern in (('request_id', r'[A-Za-z0-9_.:-]{1,128}'),
                                              ('control_fingerprint', r'[0-9a-f]{64}')))):
                raise engine.ServiceError('INVALID_REQUEST', 400)
            return self._run_refresh(lambda budget: self._get_backend().holdings_refresh.dispatch(payload, budget=budget))
        if method == 'POST' and path == OPERATOR:
            return super().dispatch(method, path, payload)
        raise engine.ServiceError('NOT_FOUND', 404)


def validate(config):
    required = {'token_file', 'owner_directory', 'account_policy_file',
                'writer_inventory_complete', 'known_writer_markers'}
    optional = {'host', 'port', 'allow_navigation', 'login_secret_file'}
    if type(config) is not dict or not required <= config.keys() <= required | optional:
        raise ValueError('INVALID_CONFIG_FIELDS')
    if config['writer_inventory_complete'] is not True:
        raise ValueError('GUI_WRITER_INVENTORY_REQUIRED')
    if type(config.get('allow_navigation', False)) is not bool:
        raise ValueError('INVALID_NAVIGATION_CONFIG')
    for key in ('token_file', 'owner_directory', 'account_policy_file', 'login_secret_file'):
        if key in config:
            literal(config[key])
    if 'login_secret_file' in config and not config.get('allow_navigation', False):
        raise ValueError('LOGIN_REQUIRES_NAVIGATION')
    markers = config['known_writer_markers']
    if (type(markers) is not list or len(markers) > 64 or any(type(x) is not str for x in markers)
            or len(markers) != len(set(markers))):
        raise ValueError('INVALID_WRITER_MARKERS')
    for path in markers:
        literal(path)
    cfg = {'host': '127.0.0.1', 'port': 18880, **config, 'known_writer_markers': tuple(markers)}
    server._config(cfg)
    return cfg


def preflight(config, *, check_policy=True):
    cfg = validate(config)
    authorization = server._load_token(cfg['token_file'])
    if len(authorization) < len(b'Bearer ') + 32:
        raise ValueError('TOKEN_TOO_SHORT')
    if check_policy:
        from ._broker.single_fixed_account import SingleFixedAccount
        SingleFixedAccount.from_private_policy(cfg['account_policy_file'])
    directory, _ = gui_ownership._walk(Path(cfg['owner_directory']), private=True)
    import os
    os.close(directory)
    return cfg, authorization


def create(config, *, backend_factory=None):
    """Bind only after obtaining the canonical lease; synthetic injection is Python-only."""
    cfg, authorization = preflight(config, check_policy=backend_factory is None)
    writers = [gui_ownership.KnownWriter(str(i), path)
               for i, path in enumerate(cfg['known_writer_markers'])]
    lease = gui_ownership.acquire_gui_owner(cfg['owner_directory'], known_writers=writers,
                                           inventory_complete=True, candidate_enabled=True)
    service = server.Service(lease)
    try:
        from ._broker.native_backend import PymacautoBackend
        factory = backend_factory or PymacautoBackend
        native = dict(allow_trade=False, allow_navigation=cfg.get('allow_navigation', False),
                      account_identity_kind='USER_CONFIRMED_SINGLE_ACCOUNT',
                      private_policy_path=cfg['account_policy_file'])
        if 'login_secret_file' in cfg:
            native['login_secret_file'] = cfg['login_secret_file']

        def construct():
            lease.assert_owned()
            return factory(native, gui_owner=lease)

        service.engine = ReadOnlyEngine.__new__(ReadOnlyEngine)
        ReadOnlyEngine.__init__(service.engine, Path(cfg['owner_directory']), construct,
                                operation_mode='READ_ONLY')
        service.engine.navigation_enabled = native['allow_navigation']
        service.engine.login_secret_configured = 'login_secret_file' in native
        service.server = server.make_server(cfg, service.engine, handler=Handler,
                                            token_loader=lambda _: authorization)
        return service
    except BaseException:
        service.close()
        raise
