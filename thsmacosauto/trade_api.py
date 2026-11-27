"""Separate manual trade composition; the normal service remains read-only."""
from pathlib import Path
import re
import sqlite3

from . import api
from ._broker import engine, gui_ownership, server, state_lifecycle
from ._broker.cancel_order import cancel
from ._broker.private_policy_read import literal

SUBMIT = '/v1/real/orders/submit'
CANCEL = '/v1/real/orders/cancel'
STATUS = '/v1/real/orders/status'
JOURNAL_STATUS = '/v1/real/orders/journal-status'
CANCEL_FIELDS = frozenset({'client_order_id', 'order_request_id', 'broker_contract_id'})


class TradeHandler(server.RequestHandler):
    get_paths = frozenset({'/v1/health', '/v1/capabilities', STATUS, JOURNAL_STATUS})
    post_keys = {SUBMIT: engine.Engine.MANUAL_FIELDS, CANCEL: CANCEL_FIELDS}


class TradeEngine(engine.Engine):
    def dispatch(self, method, path, payload=None):
        if self._closed or self._close_state != 'OPEN':
            raise engine.ServiceError('ENGINE_CLOSED', 503)
        if method == 'GET' and path == '/v1/capabilities':
            return {'status': 'MANUAL_TRADE_API', 'execution_domain': self.journal.state_domain,
                    'execution_enabled': False, 'native_readiness': 'NOT_CHECKED',
                    'get': sorted(TradeHandler.get_paths), 'post': sorted(TradeHandler.post_keys)}
        if method == 'GET' and path == JOURNAL_STATUS:
            try:
                result = dict(status='TRADE_JOURNAL_READ_ONLY', **self.journal.status_counts())
            except sqlite3.Error:
                raise engine.ServiceError('TRANSACTION_STATE_UNAVAILABLE', 503) from None
        elif method == 'POST' and path == CANCEL:
            if type(payload) is not dict or payload.keys() != CANCEL_FIELDS:
                raise engine.ServiceError('INVALID_REQUEST', 400)
            request = {k: engine.identifier(v) for k, v in payload.items()}
            if request['client_order_id'] == request['order_request_id']:
                raise engine.ServiceError('INVALID_REQUEST', 400)
            result = self._run(lambda: cancel(self, request))
        elif (method == 'POST' and path == SUBMIT or method == 'GET' and
              (path == '/v1/health' or path.startswith(STATUS + '?'))):
            result = super().dispatch(method, path, payload)
        else:
            raise engine.ServiceError('NOT_FOUND', 404)
        return {**result, 'execution_domain': self.journal.state_domain, 'execution_enabled': False}


def create(config, *, synthetic_backend_factory=None):
    """Bind only with admitted state and a qualified built-in profile (REAL).

    A backend override is accepted only for independently admitted SYNTHETIC state.
    Neither HTTP parameters nor the normal service CLI can select a backend.
    """
    trade_fields = {'state_directory', 'state_identity', 'authority_file', 'authority_sha256'}
    if type(config) is not dict or not {'state_directory', 'state_identity'} <= config.keys():
        raise ValueError('TRADE_STATE_CONFIG_REQUIRED')
    state = str(literal(config['state_directory']))
    identity = config['state_identity']
    if type(identity) is not str or not state_lifecycle.TOKEN.fullmatch(identity):
        raise ValueError('INVALID_STATE_IDENTITY')
    authority = None
    if synthetic_backend_factory is None:
        if not {'authority_file', 'authority_sha256'} <= config.keys():
            raise ValueError('REAL_TRANSACTION_AUTHORITY_REQUIRED')
        digest = config['authority_sha256']
        if type(digest) is not str or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('REAL_TRANSACTION_AUTHORITY_REQUIRED')
        authority = (str(literal(config['authority_file'])), digest)
    elif {'authority_file', 'authority_sha256'} & config.keys():
        raise ValueError('SYNTHETIC_FORBIDS_REAL_AUTHORITY')

    from ._broker.native_backend import PymacautoBackend, _profile
    from ._broker.cancel_order import qualified_profile
    if authority is not None:
        profile = _profile()
        tx = profile['transaction']
        if (tx.get('qualified') is not True or
                profile['funds'].get('qualified') is not True or not qualified_profile(profile)
                or any(not tx.get(k) for k in ('trading_day', 'complete_orders', 'complete_positions', 'contract_hint'))
                or not (tx.get('fields') or tx.get('form_descriptor'))):
            raise ValueError('NATIVE_TRADE_PROFILE_UNQUALIFIED')
    service_cfg = {k: v for k, v in config.items() if k not in trade_fields}
    cfg, auth = api.preflight(service_cfg, check_policy=authority is not None)
    if Path(state) == Path(cfg['owner_directory']):
        raise ValueError('STATE_DIRECTORY_MUST_BE_DISTINCT')
    if authority is not None and cfg.get('allow_navigation') is not True:
        raise ValueError('TRADE_REQUIRES_NAVIGATION')
    admitted = state_lifecycle.admit(state, identity, transaction_authority=authority)
    domain = 'REAL' if authority is not None else 'SYNTHETIC'
    if admitted['domain'] != domain:
        raise ValueError('TRANSACTION_DOMAIN_MISMATCH')
    writers = [gui_ownership.KnownWriter(str(i), path) for i, path in enumerate(cfg['known_writer_markers'])]
    lease = gui_ownership.acquire_gui_owner(cfg['owner_directory'], known_writers=writers,
                                           inventory_complete=True, candidate_enabled=True)
    service = server.Service(lease)
    try:
        def construct():
            lease.assert_owned()
            if synthetic_backend_factory is not None:
                return synthetic_backend_factory(lease)
            return PymacautoBackend({'allow_trade': True, 'allow_navigation': True,
                                     'account_identity_kind': 'USER_CONFIRMED_SINGLE_ACCOUNT',
                                     'private_policy_path': cfg['account_policy_file']}, gui_owner=lease)

        service.engine = TradeEngine.__new__(TradeEngine)
        TradeEngine.__init__(service.engine, Path(state), construct, operation_mode='TRANSACTIONAL',
                             transaction_identity=identity, transaction_authority=authority)
        if service.engine.journal.state_domain != domain:
            raise ValueError('TRANSACTION_DOMAIN_MISMATCH')
        service.server = server.make_server(cfg, service.engine, handler=TradeHandler, token_loader=lambda _: auth)
        return service
    except BaseException:
        service.close()
        raise
