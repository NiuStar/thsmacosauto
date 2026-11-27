"""Standalone loopback service; all native calls use one existing Broker worker."""
import copy
from pathlib import Path
import re
from types import MappingProxyType

from ._broker import engine, gui_ownership, readonly_budget, server
from ._broker.private_policy_read import literal

READS = frozenset({'/v1/health', '/v1/capabilities', '/v1/login/status',
                   '/v1/real/native-current-panel', '/v1/real/positions-panel',
                   '/v1/real/positions', '/v1/real/positions-scope-diagnostic',
                   '/v1/real/account-observation', '/v1/real/orders', '/v1/real/trades',
                   '/v1/real/holdings-refresh-controls', '/v1/real/holdings-refresh-status'})
OPERATOR = '/v1/operator/trade-page'
REFRESH = '/v1/real/holdings-refresh'
POSTS = MappingProxyType({OPERATOR: frozenset({'action'}), REFRESH: frozenset({'request_id', 'control_fingerprint'})})


class Handler(server.RequestHandler):
    get_paths = READS
    post_keys = POSTS


class ReadOnlyEngine(engine.Engine):
    READS = {**engine.Engine.READS, '/v1/real/holdings-refresh-controls': 'holdings-refresh-controls'}

    def _refresh(self, payload):
        budget = readonly_budget.ReadBudget(read_only=True)

        def operation():
            with readonly_budget.bind(budget):
                budget.check()
                if payload is None:
                    receipt = getattr(self._backend, '_refresh_receipt', None)
                    return copy.deepcopy(receipt) if receipt else {'status': 'REFRESH_NOT_CLAIMED', 'execution_enabled': False}
                return self._get_backend().holdings_refresh_budgeted(payload, budget=budget)

        return self._run_budgeted(operation, None, [False, {}], budget)

    def dispatch(self, method, path, payload=None):
        if method == 'GET' and path in READS:
            if path == '/v1/login/status':
                return super().dispatch('POST', OPERATOR, {'action': 'login-status'})
            if path == '/v1/real/holdings-refresh-status':
                return self._refresh(None)
            if path == '/v1/capabilities':
                return {'status': 'READ_ONLY', 'execution_enabled': False,
                        'get': sorted(READS), 'post': sorted(POSTS),
                        'navigation_enabled': self.navigation_enabled,
                        'login_secret_configured': self.login_secret_configured,
                        'native_readiness': 'NOT_CHECKED'}
            return super().dispatch(method, path, payload)
        if method == 'POST' and path == REFRESH:
            if (type(payload) is not dict or payload.keys() != POSTS[REFRESH]
                    or any(type(payload[k]) is not str or re.fullmatch(pattern, payload[k]) is None
                           for k, pattern in (('request_id', r'[A-Za-z0-9_.:-]{1,128}'),
                                              ('control_fingerprint', r'[0-9a-f]{64}')))):
                raise engine.ServiceError('INVALID_REQUEST', 400)
            return self._refresh(payload)
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
