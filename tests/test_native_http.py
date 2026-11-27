"""Real HTTP + Broker worker + AX adapter with synthetic desktop elements."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_api
from synthetic_ax import SyntheticRuntime
from thsmacosauto import api
from thsmacosauto._broker.native_backend import PymacautoBackend
from thsmacosauto._broker.login_auto import LoginFailure, read_password
from thsmacosauto._broker.single_fixed_account import IDENTITY_KIND
from thsmacosauto.provision_policy import create_policy


class NativeHTTP(unittest.TestCase):
    call = test_api.API.call

    def setUp(self):
        self.runtime = SyntheticRuntime()
        self.ax = self.runtime.backend
        # Use the original single-market fixture mapping for refresh qualification.
        self.ax.add('refresh', 'content', AXRole='AXButton', AXDescription='刷新持仓', AXEnabled=True)
        def factory(_cfg, *, gui_owner):
            return PymacautoBackend(dict(account_scope='fixture-account',
                shareholder_scopes={'上海':'SYNTHETIC-HOLDER'}, allow_navigation=True, allow_trade=False),
                gui_owner=gui_owner, runtime_factory=lambda:self.runtime)
        test_api.API.start(self, factory)

    def discover(self):
        status, row, _ = self.call('GET', '/v1/real/holdings-refresh-controls')
        self.assertEqual((status, row.get('status')), (200, 'REFRESH_CONTROL_QUALIFIED'), row)
        self.assertEqual(row['table'], {'row_count': 1})
        self.assertEqual(self.ax.effects, [])
        return json.dumps({'request_id':'test-once','control_fingerprint':row['control_fingerprint']})

    def test_refresh_once_and_readback_without_upstream_claim(self):
        body = self.discover()
        first = self.call('POST', api.REFRESH, body)[:2]
        self.assertEqual((first[0], first[1].get('status')), (200, 'REFRESH_DISPATCH_ACKNOWLEDGED'), first)
        self.assertEqual(first[1]['query_completion'], 'UNVERIFIED')
        self.assertEqual(self.call('POST', api.REFRESH, body)[:2], first)
        self.assertEqual(self.call('GET', '/v1/real/holdings-refresh-status')[:2], first)
        self.assertEqual(self.ax.effects, [('press','refresh')])

    def test_uncertain_refresh_retains_receipt_without_retry(self):
        body = self.discover()
        def uncertain(ref, action):
            self.ax.effects.append(('press', ref))
            raise RuntimeError('PRIVATE_FIXTURE_NOT_EXPORTED')
        self.ax.perform = uncertain
        status, result, _ = self.call('POST', api.REFRESH, body)
        self.assertEqual(status, 503)
        self.assertNotIn('PRIVATE_FIXTURE_NOT_EXPORTED', json.dumps(result))
        receipt = self.call('GET', '/v1/real/holdings-refresh-status')[:2]
        self.assertEqual((receipt[0], receipt[1]['status']), (200, 'REFRESH_UNKNOWN'))
        self.assertEqual(self.call('POST', api.REFRESH, body)[:2], receipt)
        self.assertEqual(self.ax.effects, [('press','refresh')])

    def test_sibling_dialog_blocks_action(self):
        body = self.discover()
        self.ax.nodes['modal'] = dict(AXRole='AXWindow', AXSubrole='AXDialog', AXChildren=[])
        self.ax.nodes['app']['AXWindows'].append('modal')
        self.assertEqual(self.call('POST', api.REFRESH, body)[0], 503)
        self.assertEqual(self.call('GET', '/v1/real/holdings-refresh-status')[1]['status'], 'REFRESH_BLOCKED')
        self.assertEqual(self.ax.effects, [])

    def test_token_expiring_during_revalidation_is_never_pressed(self):
        body = self.discover()
        backend = self.service.engine._backend
        observe = backend._refresh_observe

        def expire_after_observation():
            proof = observe()
            backend._refresh_discovered_ns -= 31_000_000_000
            return proof

        with patch.object(backend, '_refresh_observe', expire_after_observation):
            self.assertEqual(self.call('POST', api.REFRESH, body)[1]['code'], 'REFRESH_TOKEN_INVALID')
        self.assertEqual(self.call('GET', '/v1/real/holdings-refresh-status')[1]['status'], 'REFRESH_BLOCKED')
        self.assertEqual(self.ax.effects, [])

    def test_no_navigation_on_wrong_panel(self):
        self.ax.table('委托')
        self.assertEqual(self.call('GET','/v1/real/holdings-refresh-controls')[1]['code'], 'REFRESH_HOLDINGS_REQUIRED')
        self.assertEqual(self.ax.effects, [])

    def test_partial_positions_are_read_only_and_unverified(self):
        policy = Path(self.config['account_policy_file']).with_name('fixed.json')
        create_policy(str(policy), 'SYNTHETIC-SZ', 'SYNTHETIC-HOLDER')

        def fixed_factory():
            return PymacautoBackend(dict(account_identity_kind=IDENTITY_KIND,
                private_policy_path=str(policy), allow_navigation=False, allow_trade=False),
                gui_owner=self.service.lease, runtime_factory=lambda:self.runtime)

        self.service.engine._factory = fixed_factory
        self.ax.nodes['cell0_2']['AXValue'] = '0'
        status, data, _ = self.call('GET', '/v1/real/positions-panel')
        self.assertEqual(status, 200, data)
        self.assertEqual((data['positions'][0]['total'], data['positions'][0]['available']), (200, 0))
        self.assertEqual(data['scope_status'], 'FIXED_SCOPE_INCOMPLETE_OBSERVATION')
        self.assertEqual(data['upstream_freshness'], 'UNVERIFIED')
        self.assertFalse(data['positions_complete'])
        self.assertNotIn('SYNTHETIC-HOLDER', json.dumps(data))
        self.assertEqual(self.ax.effects, [])

class CLI(unittest.TestCase):
    def test_explicit_login_secret_is_private_and_strict(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp).resolve()/'login.json'
            secret.touch(mode=0o600)
            secret.write_text('{"schema":"THS_LOGIN_SECRET_V1","password":"SYNTHETIC"}')
            self.assertEqual(read_password(str(secret)), 'SYNTHETIC')
            secret.write_text('{"schema":"THS_LOGIN_SECRET_V1","password":"A","password":"B"}')
            with self.assertRaisesRegex(LoginFailure, 'CREDENTIAL_REQUIRED'):
                read_password(str(secret))

    def test_config_preflight_does_not_claim_owner_or_run_gui(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            owner = root/'owner'
            owner.mkdir(0o700)
            token, policy, cfg = (root/name for name in ('token','policy.json','config.json'))
            for p in (token, cfg):
                p.touch(mode=0o600)
            token.write_text('synthetic-test-bearer-not-a-credential-fixture')
            create_policy(str(policy), 'SYNTHETIC-SZ', 'SYNTHETIC-HOLDER')
            with self.assertRaises(FileExistsError):
                create_policy(str(policy), 'OTHER-SYNTHETIC')
            cfg.write_text(json.dumps(dict(token_file=str(token), owner_directory=str(owner),
                           account_policy_file=str(policy), writer_inventory_complete=True,
                           known_writer_markers=[])))
            command = [sys.executable, '-B', '-m', 'thsmacosauto', '--config', str(cfg), '--check-config']
            result = subprocess.run(command, capture_output=True, timeout=6, check=True)
            self.assertEqual(json.loads(result.stdout)['status'], 'CONFIG_VALID')
            self.assertFalse(list(owner.iterdir()))
            policy.chmod(0o644)
            self.assertNotEqual(subprocess.run(command, capture_output=True, timeout=6).returncode, 0)
            self.assertFalse(list(owner.iterdir()))
