"""Real loopback HTTP, worker and private journal; no brokerage connections."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

import test_api
from thsmacosauto import api, trade_api
from thsmacosauto._broker.state_lifecycle import provision_synthetic


SUBMIT = '/v1/real/orders/submit'
CANCEL = '/v1/real/orders/cancel'
STATUS = '/v1/real/orders/status?client_order_id='
ORDER = dict(client_order_id='fixture-order', mode='real', order_type='limit', symbol='600000',
             side='buy', price='10.00', quantity=100, shareholder_scope='FIXTURE-HOLDER')
CANCEL_BODY = dict(client_order_id='fixture-cancel', order_request_id='fixture-order',
                   broker_contract_id='FIXTURE-CONTRACT')


class BrokerFixture:
    def __init__(self):
        self.effects = []
        self.rows = []
        self.fail_after_cancel = False
        self.disappear_after_cancel = False

    def snapshot(self):
        now = time.time_ns() // 1_000_000
        return dict(real_identity_count=1, simulation_marker=False, account_scope='FIXTURE-SCOPE',
                    shareholder_scope='FIXTURE-HOLDER', broker_trading_day='20261127',
                    captured_start_ms=now, captured_end_ms=now, cash_available='10000.00',
                    positions=[], positions_complete=True, orders=[dict(r) for r in self.rows],
                    orders_complete=True, original_panel='持仓')

    def observe(self, intent):
        return self.snapshot()

    def observe_cancel(self, target):
        return self.snapshot()

    def set_before_press(self, permit):
        self.permit = permit

    def prepare(self, intent):
        return {**intent, **{k: v for k, v in self.snapshot().items() if k in (
            'real_identity_count', 'simulation_marker', 'captured_start_ms', 'captured_end_ms')},
            'unique_expected_control': True, 'opposite_control_absent': True}

    confirmation = prepare

    def press_submit(self, intent):
        assert self.permit() is True
        self.effects.append(('submit', threading.get_ident()))

    def press_confirm(self, intent):
        assert self.permit() is True
        self.effects.append(('confirm', threading.get_ident()))
        self.rows.append(dict(contract_id='FIXTURE-CONTRACT', order_date='20261127', order_time='09:30:00',
                              symbol=intent['symbol'], side=intent['side'], price=intent['price'],
                              quantity=intent['quantity'], broker_status='已报'))

    def reconcile(self, intent, baseline):
        return {**intent, 'status': 'BROKER_ORDER_VISIBLE_UNFILLED', 'submitted': True,
                'broker_contract_id': 'FIXTURE-CONTRACT', 'broker_status': '已报', 'order_date': '20261127',
                'baseline_contract_ids': [o['contract_id'] for o in baseline['orders']], 'retryable': False}

    def press_cancel(self, target):
        snapshot = self.engine.journal.lookup(target['client_order_id'])
        assert snapshot['status'] == 'CONFIRMING'
        assert self.permit() is True
        self.effects.append(('cancel', threading.get_ident()))
        matches = [r for r in self.rows if r['contract_id'] == target['broker_contract_id']]
        assert len(matches) == 1
        if self.fail_after_cancel:
            raise RuntimeError('fixture lost acknowledgment')
        if self.disappear_after_cancel:
            self.rows.remove(matches[0])
        else:
            matches[0].update(broker_status='已撤', cancelled=target['quantity'], reported_remaining=0)

    def open_cancel(self, target):
        assert self.engine.journal.lookup(target['client_order_id'])['status'] == 'SUBMITTING'
        assert self.permit() is True

    def restore(self):
        pass


class TradeHTTP(unittest.TestCase):
    call = test_api.API.call

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.token = 'fixture-' + 'a'*32
        token = self.base/'token'; token.touch(mode=0o600); token.write_text(self.token)
        owner = self.base/'owner'; owner.mkdir(mode=0o700)
        self.config = dict(host='127.0.0.1', port=0, token_file=str(token), owner_directory=str(owner),
                           account_policy_file=str(self.base/'account.json'),
                           writer_inventory_complete=True, known_writer_markers=[],
                           state_directory=str(self.base/'state'), state_identity='fixture-identity')
        provision_synthetic(self.config['state_directory'], identity=self.config['state_identity'], source='http-test')
        self.backend = BrokerFixture()
        self.start()
        self.addCleanup(self.stop)

    def start(self):
        self.service = trade_api.create(self.config, synthetic_backend_factory=lambda lease: self.backend)
        self.backend.engine = self.service.engine
        self.http = threading.Thread(target=self.service.server.serve_forever, kwargs={'poll_interval': .01})
        self.http.start()

    def stop(self):
        self.service.server.shutdown()
        self.http.join(3)
        self.assertFalse(self.http.is_alive())
        self.service.close()

    def post(self, route, data, **kwargs):
        return self.call('POST', route, json.dumps(data), **kwargs)[:2]

    def submit(self):
        code, result = self.post(SUBMIT, ORDER)
        self.assertEqual((code, result.get('status')), (200, 'BROKER_ORDER_VISIBLE_UNFILLED'), result)
        self.assertEqual(result['broker_contract_id'], CANCEL_BODY['broker_contract_id'])
        self.assertEqual(result['execution_domain'], 'SYNTHETIC')
        return result

    def test_order_then_cancel_exact_contract_and_restart_replay(self):
        original = self.submit()
        self.assertEqual(self.post(SUBMIT, ORDER), (200, original))
        self.backend.rows.append({**self.backend.rows[0], 'contract_id': 'UNRELATED-CONTRACT'})
        result = self.post(CANCEL, CANCEL_BODY)
        self.assertEqual((result[0], result[1].get('status')), (200, 'BROKER_ORDER_CANCELLED'), result)
        self.assertEqual(result[1]['broker_contract_id'], CANCEL_BODY['broker_contract_id'])
        self.assertEqual(self.backend.rows[1]['broker_status'], '已报')
        self.assertEqual(self.call('GET', STATUS + 'fixture-cancel')[:2], result)
        self.assertEqual(self.post(CANCEL, {**CANCEL_BODY, 'client_order_id': 'another-id'})[1]['reason'],
                         'ALREADY_CANCELLED')
        self.stop()
        self.start()
        self.assertEqual(self.post(CANCEL, CANCEL_BODY), result)
        self.assertEqual(self.post(SUBMIT, ORDER), (200, original))
        self.assertEqual([action for action, _ in self.backend.effects], ['submit', 'confirm', 'cancel'])
        self.assertEqual(len({tid for _, tid in self.backend.effects}), 1)

    def test_refuses_unknown_contract_wrong_row_and_extra_parameters(self):
        self.assertEqual(self.post(SUBMIT, ORDER, auth=False)[0], 401)
        self.assertEqual(self.post(CANCEL, CANCEL_BODY, origin='https://example.invalid')[0], 403)
        self.assertEqual(self.post(CANCEL, {**CANCEL_BODY, 'side': 'all'})[0], 400)
        self.assertEqual(self.post('/v1/auto/buy', {})[0], 404)
        self.assertEqual(self.post(CANCEL, CANCEL_BODY)[1]['code'], 'ORDER_NOT_FOUND')
        self.assertEqual(self.backend.effects, [])
        self.submit()
        self.assertEqual(self.post(CANCEL, {**CANCEL_BODY, 'broker_contract_id': 'OTHER'})[1]['code'], 'CANCEL_TARGET_MISMATCH')
        self.backend.rows[0]['symbol'] = '600001'
        result = self.post(CANCEL, CANCEL_BODY)
        self.assertEqual((result[0], result[1].get('status'), result[1].get('reason')),
                         (200, 'BLOCKED', 'CANCEL_TARGET_MISMATCH'), result)
        self.assertEqual([action for action, _ in self.backend.effects], ['submit', 'confirm'])

    def test_disappearing_order_is_unknown_and_does_not_retry(self):
        self.submit()
        self.backend.disappear_after_cancel = True
        response = self.post(CANCEL, CANCEL_BODY)
        self.assertEqual((response[0], response[1].get('status')), (200, 'UNKNOWN'), response)
        self.assertEqual(self.post(CANCEL, CANCEL_BODY), response)
        self.assertEqual(self.call('GET', STATUS + 'fixture-cancel')[:2], response)
        self.assertEqual(self.post(CANCEL, {**CANCEL_BODY, 'client_order_id': 'another-id'})[1]['code'], 'ACCOUNT_UNKNOWN')
        self.assertEqual([action for action, _ in self.backend.effects], ['submit', 'confirm', 'cancel'])

    def test_lost_cancel_ack_and_restart_do_not_replay(self):
        self.submit()
        self.backend.fail_after_cancel = True
        response = self.post(CANCEL, CANCEL_BODY)
        self.assertEqual((response[0], response[1]['status']), (200, 'UNKNOWN'))
        self.assertTrue(response[1]['cancel_attempted'])
        self.stop()
        self.start()
        self.assertEqual(self.call('GET', STATUS + 'fixture-cancel')[:2], response)
        self.assertEqual(self.post(CANCEL, CANCEL_BODY), response)
        self.assertEqual(self.post(SUBMIT, {**ORDER, 'client_order_id': 'another-order'})[1]['code'],
                         'ACCOUNT_UNKNOWN')
        self.assertEqual([action for action, _ in self.backend.effects], ['submit', 'confirm', 'cancel'])

    def test_journal_status_only_counts_without_native_or_receipts(self):
        path = '/v1/real/orders/journal-status'
        self.assertEqual(self.call('GET', path, auth=False)[0], 401)
        self.assertEqual(self.call('GET', path+'?client_order_id=fixture-order')[0], 400)
        code, initial, _ = self.call('GET', path)
        self.assertEqual((code, initial['request_count'], initial['unresolved_count']), (200, 0, 0))
        self.assertIsNone(self.service.engine._backend)
        self.submit()
        self.backend.fail_after_cancel = True
        self.assertEqual(self.post(CANCEL, CANCEL_BODY)[1]['status'], 'UNKNOWN')
        self.stop()
        self.start()
        code, result, _ = self.call('GET', path)
        self.assertEqual((code, result['request_count'], result['unresolved_count'], result['in_flight_count']),
                         (200, 2, 1, 0))
        self.assertTrue(result['new_requests_blocked_by_unknown'])
        self.assertEqual(result['execution_domain'], 'SYNTHETIC')
        self.assertNotIn('fixture-order', json.dumps(result))
        self.assertIsNone(self.service.engine._backend)

    def test_real_trade_refuses_synthetic_state_before_binding(self):
        with self.assertRaisesRegex(ValueError, 'REAL_TRANSACTION_AUTHORITY_REQUIRED'):
            trade_api.create(self.config)
        with self.assertRaisesRegex(ValueError, 'NATIVE_TRADE_PROFILE_UNQUALIFIED'):
            trade_api.create({**self.config, 'authority_file': str(self.base/'not-admitted.json'),
                              'authority_sha256': '0'*64})
        self.assertNotIn(SUBMIT, api.POSTS)
        self.assertNotIn(CANCEL, api.POSTS)
        self.assertEqual(self.backend.effects, [])
