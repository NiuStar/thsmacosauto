"""Acceptance of every published HTTP route against the real broker and synthetic AX."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import quote

import test_native_http as fixture
from thsmacosauto import api
from thsmacosauto._broker import login_retry


class CapabilityMatrix(unittest.TestCase):
    setUp = fixture.NativeHTTP.setUp
    call = fixture.NativeHTTP.call

    def fixed_account(self):
        policy = Path(self.config['token_file']).with_name('fixed.json')
        fixture.create_policy(str(policy), 'SYNTHETIC-HOLDER')
        self.service.engine._factory = lambda: fixture.PymacautoBackend({
            'account_identity_kind': fixture.IDENTITY_KIND, 'private_policy_path': str(policy),
            'allow_navigation': True, 'allow_trade': False,
        }, gui_owner=self.service.lease, runtime_factory=lambda: self.runtime)
        table = self.ax.table

        def select(page):
            table(page)
            if page == '持仓':
                self.ax.nodes['cell0_4']['AXValue'] = '深圳Ａ股'

        self.ax.table = select
        select('持仓')

    def funds_table(self):
        ax = self.ax
        ax.add('funds', AXRole='AXTable', AXIdentifier='_NS:65', AXColumns=[], AXRows=[])
        for j in range(2):
            col = f'fund-col-{j}'
            ax.nodes['funds']['AXColumns'].append(col)
            ax.nodes[col] = {'AXRole': 'AXColumn', 'AXIndex': j, 'AXRows': []}
        for i in range(10):
            row = f'fund-row-{i}'
            ax.nodes['funds']['AXRows'].append(row)
            ax.nodes[row] = {'AXRole': 'AXRow', 'AXIndex': i, 'AXChildren': []}
            for j in range(2):
                cell, text = f'fund-cell-{i}-{j}', f'fund-text-{i}-{j}'
                ax.nodes[row]['AXChildren'].append(cell)
                ax.nodes[f'fund-col-{j}']['AXRows'].append(cell)
                ax.nodes[cell] = {'AXRole': 'AXCell', 'AXChildren': [text]}
                ax.nodes[text] = {'AXRole': 'AXStaticText', 'AXValue':
                                  ('可用金额' if i == 0 else f'测试字段{i}') if j == 0 else '10000.00'}

    def test_public_route_matrix(self):
        self.fixed_account()
        self.funds_table()
        ax = self.ax
        for page, identifier in [('持仓', '_NS:42'), ('委托', '_NS:38'), ('成交', '_NS:34')]:
            ax.add('nav-' + page, AXRole='AXButton', AXTitle=page, AXIdentifier=identifier, AXEnabled=True)
        ax.order_rows = [['20261113', '10:00:00', '600000', '买入', '100', '10.00', 'C-FIXTURE', '已成']]
        covered = set()

        def hit(method, path, expected, payload=None):
            with self.subTest(method=method, path=path):
                code, reply, headers = self.call(method, path, None if payload is None else json.dumps(payload))
                self.assertEqual((code, reply.get('status')), (200, expected), reply)
                self.assertIs(reply['execution_enabled'], False)
                self.assertEqual(headers['Cache-Control'], 'no-store')
                covered.add((method, path.split('?', 1)[0]))
                return reply

        def get(path, expected):
            return hit('GET', path, expected)

        get('/v1/health', 'ok')
        caps = get('/v1/capabilities', 'READ_ONLY')
        self.assertEqual(set(caps['get']), api.READS)
        self.assertEqual(set(caps['post']), set(api.POSTS))
        self.assertFalse(ax.calls)
        get('/v1/login/status', 'ALREADY_LOGGED_IN')
        hit('POST', '/v1/login', 'ALREADY_LOGGED_IN', {})
        hit('POST', api.OPERATOR, 'TRADE_PAGE_INSPECTED', {'action': 'inspect'})
        for path in ('/v1/holdings', '/v1/real/positions-panel', '/v1/real/positions'):
            reply = get(path, 'SYNTHETIC_POSITIONS_READ_ONLY' if path.endswith('/positions') else 'POSITIONS_PANEL_READ_ONLY')
            self.assertEqual([(r['total'], r['available']) for r in reply['positions']], [(200, 100)])
            self.assertIs(reply['positions_complete'], False)
        self.assertEqual(get('/v1/real/native-current-panel', 'READ_OK')['panel'], '持仓')
        self.assertEqual(get('/v1/real/positions-scope-diagnostic', 'POSITIONS_SCOPE_DIAGNOSTIC_READ_ONLY')['scope_status'],
                         'FIXED_SCOPE_COMPLETE_OBSERVATION')
        self.assertEqual(get('/v1/real/account-observation', 'READ_OK')['cash_available'], '10000.00')
        summary = get('/v1/holdings/summary', 'HOLDINGS_SUMMARY_READ_ONLY')
        self.assertEqual((summary['observed_row_count'], summary['held_symbol_count'],
                          summary['sellable_symbol_count']), (1, 1, 1))
        self.assertNotIn('positions', summary)
        self.assertEqual(summary['upstream_freshness'], 'UNVERIFIED')
        self.assertFalse(summary['positions_complete'])
        cash = get('/v1/account/available-cash', 'AVAILABLE_CASH_READ_ONLY')
        self.assertEqual(cash['cash_available'], '10000.00')
        self.assertEqual(cash['monetary_authority'], 'NONE')
        self.assertNotIn('account_scope', cash)
        self.add_form()
        form = get('/v1/real/form-discovery', 'SYNTHETIC_FORM_READ_ONLY')
        self.assertEqual(form['form']['observed_confirmation'], '确定买入')
        self.assertFalse(form['transaction_qualified'])
        self.assertEqual(set(form['form']['fields']), {'code', 'price', 'quantity'})
        self.assertFalse(any(ref.startswith('form-field-') and prop == 'AXValue' for ref, prop in ax.calls))
        self.assertEqual(ax.effects, [])
        self.assertEqual(get('/v1/real/orders', 'SYNTHETIC_ORDERS_READ_ONLY')['orders'][0]['contract_id'], 'C-FIXTURE')
        trade = get('/v1/real/trades', 'SYNTHETIC_TRADES_READ_ONLY')
        self.assertEqual(trade['trades'][0]['amount'], '1000.00')
        self.assertIs(trade['fill_evidence']['independent_fill_verified'], False)
        self.assertEqual(ax.effects, [('press','nav-委托'), ('press','nav-持仓'), ('press','nav-成交'), ('press','nav-持仓')])
        self.assertEqual(ax.nodes['table']['AXIdentifier'], '1537')

        holding = get('/v1/holdings/lookup?symbol=600000', 'VISIBLE_MATCH_FOUND')
        self.assertEqual((holding['position']['total'], holding['position']['available']), (200, 100))
        self.assertEqual(holding['upstream_freshness'], 'UNVERIFIED')
        order = get('/v1/real/orders/lookup?contract_id=C-FIXTURE', 'VISIBLE_MATCH_FOUND')
        self.assertEqual(order['order']['contract_id'], 'C-FIXTURE')
        fills = get('/v1/real/trades/lookup?contract_id=C-FIXTURE', 'VISIBLE_MATCH_FOUND')
        self.assertEqual([r['trade_id'] for r in fills['trades']], ['T-FIXTURE'])
        self.assertIs(fills['independent_fill_verified'], False)
        self.assertEqual(ax.effects, [('press','nav-委托'), ('press','nav-持仓'), ('press','nav-成交'), ('press','nav-持仓')]*2)

        get('/v1/holdings/refresh/status', 'REFRESH_NOT_CLAIMED')
        before = list(ax.effects)
        control = get('/v1/holdings/refresh/controls', 'REFRESH_CONTROL_QUALIFIED')
        legacy = get(api.REFRESH + '-controls', 'REFRESH_CONTROL_QUALIFIED')
        self.assertEqual(control['control_fingerprint'], legacy['control_fingerprint'])
        self.assertEqual(ax.effects, before)
        payload = {'request_id': 'matrix-1', 'control_fingerprint': control['control_fingerprint']}
        receipt = hit('POST', '/v1/holdings/refresh', 'REFRESH_DISPATCH_ACKNOWLEDGED', payload)
        calls = len(ax.calls)
        self.assertEqual(hit('POST', api.REFRESH, 'REFRESH_DISPATCH_ACKNOWLEDGED', payload), receipt)
        for path in ('/v1/holdings/refresh/status', api.REFRESH + '-status'):
            self.assertEqual(get(path, 'REFRESH_DISPATCH_ACKNOWLEDGED'), receipt)
        self.assertEqual(len(ax.calls), calls)
        self.assertEqual(ax.effects, [*before, ('press', 'refresh')])
        self.assertEqual(receipt['query_completion'], 'UNVERIFIED')
        self.assertIsNone(receipt['broker_as_of'])
        self.assertEqual(covered, {('GET', p) for p in api.READS} | {('POST', p) for p in api.POSTS})

    def test_scope_conflict_and_invalid_funds_are_not_empty_observations(self):
        self.fixed_account()
        self.funds_table()
        self.ax.nodes['fund-text-0-1']['AXValue'] = 'invalid'
        code, reply, _ = self.call('GET', '/v1/real/account-observation')
        self.assertEqual((code, reply['code']), (503, 'CASH_ASSOCIATION_UNPROVEN'))
        code, reply, _ = self.call('GET', '/v1/account/available-cash')
        self.assertEqual((code, reply['code']), (503, 'CASH_ASSOCIATION_UNPROVEN'))
        self.ax.nodes['cell0_5']['AXValue'] = 'SYNTHETIC-DIFFERENT-HOLDER'
        code, reply, _ = self.call('GET', '/v1/holdings')
        self.assertEqual((code, reply['code']), (503, 'FIXED_SCOPE_CHANGED'))
        self.assertNotIn('positions', reply)
        self.assertEqual(self.call('GET', '/v1/holdings/summary')[1]['code'], 'FIXED_SCOPE_CHANGED')
        self.assertEqual(self.ax.effects, [])

    def add_form(self):
        # Model the parent binding of existing toolbar buttons as well.
        for ref in self.ax.nodes['window']['AXChildren']:
            if self.ax.nodes[ref].get('AXRole') == 'AXButton':
                self.ax.nodes[ref]['AXParent'] = 'window'
        for i, (name, identifier, text) in enumerate((('code','_NS:14','代码'),
                ('price','_NS:37','价格'), ('quantity','_NS:65','数量'))):
            y = i * 25
            self.ax.add('form-label-'+name, AXRole='AXStaticText', AXIdentifier=identifier,
                        AXParent='window', AXValue=text, AXPosition={'x':0,'y':y}, AXSize={'width':30,'height':20})
            self.ax.add('form-field-'+name, AXRole='AXTextField', AXParent='window', AXEnabled=True,
                        AXValue='DO-NOT-READ', AXPosition={'x':40,'y':y}, AXSize={'width':100,'height':20})
        for i, label in enumerate(('买入', '卖出', '确定买入')):
            self.ax.add('form-button-'+str(i), AXRole='AXButton', AXTitle=label, AXParent='window',
                        AXEnabled=True, AXPosition={'x':0,'y':100+i*25}, AXSize={'width':100,'height':20})
        self.ax.add('limit-label', AXRole='AXStaticText', AXIdentifier='_NS:104', AXParent='window',
                    AXValue='限价委托', AXPosition={'x':0,'y':200}, AXSize={'width':100,'height':20})

    def test_summary_and_cash_never_navigate_or_submit(self):
        self.fixed_account()
        for path in ('/v1/holdings/summary', '/v1/account/available-cash', '/v1/real/form-discovery'):
            self.assertEqual(self.call('GET', path, auth=False)[0], 401)
            self.assertEqual(self.call('GET', path+'?symbol=600000')[0], 400)
        self.assertEqual(self.ax.calls, [])
        self.ax.nodes['cell0_2']['AXValue'] = '0'
        code, summary, _ = self.call('GET', '/v1/holdings/summary')
        self.assertEqual((code, summary['held_symbol_count'], summary['sellable_symbol_count']), (200, 1, 0))
        self.ax.table('委托')
        for path in ('/v1/holdings/summary', '/v1/account/available-cash'):
            code, body, _ = self.call('GET', path)
            self.assertEqual((code, body['code']), (503, 'POSITIONS_PANEL_REQUIRED'))
        self.assertEqual(self.ax.effects, [])

    def test_lookup_uses_exact_keys_and_preserves_scope_and_failures(self):
        # Only the Shanghai market is visible in this two-market configuration.
        policy = Path(self.config['token_file']).with_name('lookup-policy.json')
        fixture.create_policy(str(policy), 'SYNTHETIC-SZ', 'SYNTHETIC-HOLDER')
        self.service.engine._factory = lambda: fixture.PymacautoBackend({
            'account_identity_kind': fixture.IDENTITY_KIND, 'private_policy_path': str(policy),
            'allow_navigation': False, 'allow_trade': False,
        }, gui_owner=self.service.lease, runtime_factory=lambda: self.runtime)
        path = '/v1/holdings/lookup'
        self.assertEqual(self.call('GET', path+'?symbol=600000', auth=False)[0], 401)
        for suffix in ('', '?', '?symbol=60000', '?symbol=６０００００', '?symbol=600000&symbol=600001',
                       '?symbol=%20600000', '?symbol=600000&foo=1', '?symbol=%ZZ', '?contract_id=x'):
            # HTTP targets are ASCII; encode the non-ASCII example for the wire.
            with self.subTest(suffix=suffix):
                self.assertEqual(self.call('GET', path+quote(suffix, safe='?=&%'))[0], 400)
        self.assertEqual(self.ax.calls, [])
        self.ax.nodes['cell0_0']['AXValue'] = '000001'
        self.ax.nodes['cell0_4']['AXValue'] = '深圳Ａ股'
        self.ax.nodes['cell0_5']['AXValue'] = 'SYNTHETIC-SZ'
        self.ax.nodes['cell0_1']['AXValue'] = '0'
        self.ax.nodes['cell0_2']['AXValue'] = '0'
        code, hit, _ = self.call('GET', path+'?symbol=000001')
        self.assertEqual((code, hit['status'], hit['position']['symbol'], hit['position']['total']),
                         (200, 'VISIBLE_MATCH_FOUND', '000001', 0))
        code, miss, _ = self.call('GET', path+'?symbol=000002')
        self.assertEqual((code, miss['status'], miss['position'], miss['scope_status']),
                         (200, 'NOT_FOUND_IN_VISIBLE_SCOPE', None, 'FIXED_SCOPE_INCOMPLETE_OBSERVATION'))
        self.assertIs(miss['positions_complete'], False)
        self.ax.nodes['cell0_5']['AXValue'] = 'SYNTHETIC-OTHER'
        code, error, _ = self.call('GET', path+'?symbol=000002')
        self.assertEqual((code, error['code']), (503, 'FIXED_SCOPE_CHANGED'))
        self.assertNotIn('position', error)
        self.assertEqual(self.ax.effects, [])

    def test_lookup_with_navigation_disabled_does_not_turn_refusal_into_miss(self):
        self.fixed_account()
        factory = self.service.engine._factory
        def without_navigation():
            backend = factory()
            backend.config['allow_navigation'] = False
            return backend
        self.service.engine._factory = without_navigation
        for kind in ('orders', 'trades'):
            code, error, _ = self.call('GET', f'/v1/real/{kind}/lookup?contract_id=C-FIXTURE')
            self.assertEqual((code, error['code']), (503, 'NAVIGATION_DISABLED'))
            self.assertNotIn('match_count', error)
        self.assertEqual(self.ax.effects, [])

    def test_contract_lookup_returns_only_selected_rows_and_all_matching_fills(self):
        self.fixed_account()
        ax = self.ax
        for page, identifier in [('持仓', '_NS:42'), ('委托', '_NS:38'), ('成交', '_NS:34')]:
            ax.add('nav-' + page, AXRole='AXButton', AXTitle=page, AXIdentifier=identifier, AXEnabled=True)
        ax.order_rows = [['20261113','10:00:00','600000','买入','100','10.00',cid,'已成']
                         for cid in ('C:1', 'C:10')]
        ax.trade_rows = [['20261113',time,'600000','买入','50','10.00','500.00',cid,tid]
                         for time, cid, tid in [('10:00:00','C:1','T-01'), ('10:00:01','C:10','T-OTHER'),
                                                ('10:00:02','C:1','T-02')]]
        for kind in ('orders', 'trades'):
            path = f'/v1/real/{kind}/lookup'
            for suffix in ('', '?contract_id=', '?contract_id='+('a'*129), '?contract_id=C%2f1',
                           '?contract_id=C+1', '?contract_id=C&symbol=600000'):
                self.assertEqual(self.call('GET', path+suffix)[0], 400)
        self.assertEqual(ax.calls, [])
        for kind in ('orders', 'trades'):
            with self.subTest(kind=kind):
                path = f'/v1/real/{kind}/lookup?contract_id='
                code, data, _ = self.call('GET', path+'C%3A1')
                self.assertEqual((code, data['status'], data['contract_id']), (200, 'VISIBLE_MATCH_FOUND', 'C:1'))
                rows = [data['order']] if kind == 'orders' else data['trades']
                self.assertTrue(all(row['contract_id']=='C:1' for row in rows))
                if kind == 'trades':
                    self.assertEqual([r['trade_id'] for r in rows], ['T-01', 'T-02'])
                self.assertNotIn('fill_evidence', data)
                self.assertNotIn('table', data)
                self.assertFalse(data['account_or_history_complete'])
                self.assertNotIn('C:10', json.dumps(data))
                code, data, _ = self.call('GET', path+'c:1')
                self.assertEqual((code, data['status']), (200, 'NOT_FOUND_IN_VISIBLE_SCOPE'))
                self.assertEqual(data['order'] if kind == 'orders' else data['trades'], None if kind == 'orders' else [])
                self.assertEqual(ax.nodes['table']['AXIdentifier'], '1537')
        # A bad nonmatching row is still a source failure, not a successful lookup.
        ax.trade_rows[1][4] = 'bad-quantity'
        code, error, _ = self.call('GET', '/v1/real/trades/lookup?contract_id=C:1')
        self.assertEqual((code, error['code']), (503, 'INVALID_INDEPENDENT_TRADE_EVIDENCE'))
        self.assertNotIn('trades', error)
        self.assertEqual(ax.nodes['table']['AXIdentifier'], '1537')
        self.assertTrue(all(kind == 'press' and ref.startswith('nav-') for kind, ref in ax.effects))

    def test_historical_actions_keep_spent_authorization_and_attempt_intact(self):
        owner = Path(self.config['owner_directory'])
        records = {login_retry.ORIGINAL: b'{"schema":"THS_LOGIN_ATTEMPT_V1","state":"UNKNOWN"}',
                   login_retry.CLAIM: b'{"schema":"THS_LOGIN_RETRY_V1","state":"CLAIMED"}',
                   login_retry.ATTEMPT: b'{"schema":"THS_LOGIN_ATTEMPT_V1","state":"UNKNOWN"}'}
        for name, value in records.items():
            path = owner / name
            path.touch(mode=0o600)
            path.write_bytes(value)
        before = {name: (owner / name).stat().st_mtime_ns for name in records}
        self.ax.nodes['window']['AXChildren'].remove('broker')
        self.ax.add('entry', AXRole='AXButton', AXTitle='立即登录', AXEnabled=True)
        with patch('thsmacosauto._broker.login_auto.read_password', side_effect=AssertionError('secret read')) as reader:
            old_status = self.call('POST', api.OPERATOR, '{"action":"login-status"}')
            self.assertEqual((old_status[0], old_status[1]['code']), (400, 'BAD_REQUEST'))
            new_status = self.call('GET', '/v1/login/status')
            self.assertEqual((new_status[0], new_status[1]['status']), (200, 'LOGIN_ENTRY_INSPECTED'))
            for action, expected in [('login-inspect', 'LOGIN_RESULT_UNAVAILABLE'),
                                     ('ensure-login', 'LOGIN_RETRY_USED'), ('retry-login', 'LOGIN_RETRY_USED'),
                                     ('fresh-login', 'LOGIN_FRESH_BLOCKED')]:
                with self.subTest(action=action):
                    code, data, _ = self.call('POST', api.OPERATOR, json.dumps({'action': action}))
                    self.assertEqual((code, data.get('status')), (200, expected), data)
                    self.assertIs(data['press_attempted'], False)
            reader.assert_not_called()
        self.assertEqual(self.ax.effects, [])
        self.assertEqual({name: (owner / name).read_bytes() for name in records}, records)
        self.assertEqual({name: (owner / name).stat().st_mtime_ns for name in records}, before)
