import http.client
import json
import secrets
import tempfile
import threading
import unittest
from pathlib import Path

from thsmacosauto import api


class Backend:
    def __init__(self, effects):
        self.effects = effects

    def read_budgeted(self, kind, *, budget):
        self.effects.append(('read', kind, threading.get_ident()))
        return {'status': 'POSITIONS_PANEL_READ_ONLY', 'positions_complete': False,
                'positions': [{'symbol': '000001', 'total': 100, 'available': 0}],
                'execution_enabled': False}

    def trade_page(self, action, *, budget):
        self.effects.append(('operator', action, threading.get_ident()))
        return {'status': 'ALREADY_LOGGED_IN', 'press_attempted': False, 'execution_enabled': False}


class API(unittest.TestCase):
    def setUp(self):
        self.effects = []
        self.start(lambda *_a, **_kw: Backend(self.effects))

    def start(self, factory):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.token = secrets.token_hex(32)
        for name in ('bearer', 'account.json'):
            (base/name).touch(mode=0o600)
        (base/'bearer').write_text(self.token)
        (base/'owner').mkdir(mode=0o700)
        self.config = dict(port=0, token_file=str(base/'bearer'), owner_directory=str(base/'owner'),
                           account_policy_file=str(base/'account.json'), writer_inventory_complete=True,
                           known_writer_markers=[], allow_navigation=True)
        self.service = api.create(self.config, backend_factory=factory)
        self.addCleanup(self.service.close)
        t = threading.Thread(target=self.service.server.serve_forever, kwargs={'poll_interval': 0.01})
        t.start()
        self.addCleanup(t.join)
        self.addCleanup(self.service.server.shutdown)

    def call(self, method, path, body=None, *, auth=True, origin=None):
        headers = {'Content-Type': 'application/json'}
        if auth:
            headers['Authorization'] = 'Bearer ' + self.token
        if origin:
            headers['Origin'] = origin
        with_connection = http.client.HTTPConnection('127.0.0.1', self.service.server.server_port, timeout=5)
        try:
            with_connection.request(method, path, body, headers)
            r = with_connection.getresponse()
            return r.status, json.loads(r.read()), dict(r.getheaders())
        finally:
            with_connection.close()

    def test_versioned_positions_and_explicit_login_share_one_worker(self):
        status, data, headers = self.call('GET', '/v1/real/positions-panel')
        self.assertEqual(status, 200)
        self.assertEqual(data['positions'][0]['available'], 0)
        self.assertFalse(data['positions_complete'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        status, data, _ = self.call('POST', '/v1/operator/trade-page', '{"action":"ensure-login"}')
        self.assertEqual((status, data['status']), (200, 'ALREADY_LOGGED_IN'))
        self.assertEqual([(kind, key) for kind, key, _ in self.effects],
                         [('read', 'positions-panel'), ('operator','ensure-login')])
        self.assertEqual(len({thread for _, _, thread in self.effects}), 1)
        self.assertNotEqual(self.effects[0][2], threading.get_ident())

    def test_login_status_is_passive(self):
        status, data, _ = self.call('GET', '/v1/login/status')
        self.assertEqual((status, data['status']), (200, 'ALREADY_LOGGED_IN'))
        self.assertEqual(self.effects[0][:2], ('operator', 'login-status'))

    def test_auth_and_unpublished_actions_do_not_reach_backend(self):
        self.assertEqual(self.call('GET', '/v1/health', auth=False)[0], 401)
        self.assertEqual(self.call('POST', '/v1/operator/trade-page', '{"action":"ensure-login"}', origin='https://example.invalid')[0], 403)
        for path in ('/v1/real/orders/submit', '/v1/control/resume', '/v1/auto/buy', '/v1/shadow/evidence'):
            self.assertEqual(self.call('POST', path, '{}')[0], 404)
        self.assertEqual(self.call('POST', '/v1/operator/trade-page', '{"action":"ensure-login","password":"fixture"}')[0], 400)
        self.assertEqual(self.call('GET', '/v1/login/status?account=other')[0], 400)
        self.assertEqual(self.effects, [])

    def test_capabilities_and_health_do_not_construct_backend(self):
        for path in ('/v1/health', '/v1/capabilities'):
            status, result, _ = self.call('GET', path)
            self.assertEqual(status, 200)
            self.assertIs(result['execution_enabled'], False)
        self.assertEqual(self.effects, [])

    def test_owner_lock_and_disabled_trade_configuration(self):
        with self.assertRaisesRegex(RuntimeError, 'GUI_OWNER_BUSY'):
            api.create(self.config, backend_factory=Backend)
        for field, value in (('allow_trade', True), ('operation_mode','TRANSACTIONAL'), ('host','0.0.0.0'), ('writer_inventory_complete', False)):
            with self.assertRaises(ValueError):
                api.create({**self.config, field: value}, backend_factory=Backend)


if __name__ == '__main__':
    unittest.main()
