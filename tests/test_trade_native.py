"""Real HTTP/Engine/native adapter over a synthetic, non-broker AX tree."""
from pathlib import Path
import unittest

import test_trade_api as wired
from synthetic_ax import SyntheticRuntime
from thsmacosauto._broker.native_backend import PymacautoBackend, _profile
from thsmacosauto._broker.single_fixed_account import IDENTITY_KIND
from thsmacosauto.provision_policy import create_policy


class NativeTradeHTTP(unittest.TestCase):
    setUp = wired.TradeHTTP.setUp
    stop = wired.TradeHTTP.stop
    call = wired.TradeHTTP.call
    post = wired.TradeHTTP.post
    submit = wired.TradeHTTP.submit

    def start(self):
        self.runtime = SyntheticRuntime()
        ax = self.ax = self.runtime.backend
        self.wrong_sheet = False
        self.row_button = True
        profile = _profile()
        profile['funds']['qualified'] = True
        profile['transaction'].update(qualified=True, fields={name: {'role':'AXTextField', 'identifier':name}
            for name in ('code', 'price', 'quantity')}, trading_day={'role':'AXStaticText', 'identifier':'day'},
            complete_positions={'role':'AXStaticText', 'identifier':'complete-pos'}, complete_positions_value='yes',
            complete_orders={'role':'AXStaticText', 'identifier':'complete-orders'}, complete_orders_value='yes',
            contract_hint={'role':'AXStaticText','identifier':'submit-contract'})
        profile['cancel'] = dict(qualified=True, entry={'role':'AXButton','title':'撤单'},
            sheet={'role':'AXSheet','identifier':'fixture-cancel-sheet'},
            detail={'role':'AXStaticText','identifier':'fixture-cancel-detail'},
            confirm={'role':'AXButton','title':'确认'})
        for page, identifier in [('持仓','_NS:42'), ('委托','_NS:38')]:
            ax.add('nav-'+page, AXRole='AXButton', AXTitle=page, AXIdentifier=identifier, AXEnabled=True)
        for ref, value in [('day','20261127'), ('complete-pos','yes'), ('complete-orders','yes')]:
            ax.add(ref, AXRole='AXStaticText', AXIdentifier=ref, AXValue=value)
        ax.add('cash-label', AXRole='AXStaticText', AXValue='可用金额')
        ax.add('cash', AXRole='AXStaticText', AXValue='10000.00')
        for ref in ('code','price','quantity'):
            ax.add(ref, AXRole='AXTextField', AXIdentifier=ref, AXValue='', AXEnabled=True)
        ax.is_settable = lambda ref, attr: ref in ('code','price','quantity') and attr == 'AXValue'
        for label in ('限价委托','代码','价格','数量','方向'):
            ax.add('label-'+label, AXRole='AXStaticText', AXValue=label)
        ax.add('submit', AXRole='AXButton', AXTitle='确定买入', AXEnabled=True)
        ax.add('unsafe-toolbar', AXRole='AXButton', AXTitle='撤买', AXEnabled=True)

        base_table = ax.table
        def table(page):
            base_table(page)
            if page == '持仓':
                # Both markets observed; the actual order target is the Shanghai row.
                ax.nodes['cell0_5']['AXValue'] = 'FIXTURE-HOLDER'
                tbl = ax.nodes['table']
                tbl['AXRows'].append('row1')
                ax.nodes['row1'] = {'AXIndex':1,'AXChildren':[]}
                for j, value in enumerate(('000001','300','300','9.00','深圳Ａ股','FIXTURE-SZ')):
                    node = f'cell1_{j}'
                    ax.nodes['row1']['AXChildren'].append(node)
                    ax.nodes[f'col{j}']['AXRows'].append(node)
                    ax.nodes[node] = {'AXRole':'AXStaticText','AXValue':value}
            if page == '委托':
                tbl = ax.nodes['table']
                j = len(tbl['AXColumns'])
                tbl['AXColumns'].append('cancel-column')
                ax.nodes['cancel-column'] = {'AXIndex':j,'AXHeader':'cancel-head','AXRows':[]}
                ax.nodes['cancel-head'] = {'AXTitle':'撤销数量'}
                for i, row in enumerate(tbl['AXRows']):
                    c = f'cancel-cell-{i}'
                    ax.add(c, row, AXRole='AXStaticText', AXValue='100' if self.cancelled and i==0 else '0')
                    ax.nodes['cancel-column']['AXRows'].append(c)
                    if self.row_button:
                        ax.nodes[c].update(AXRole='AXCell', AXParent=row, AXChildren=[c+'-text'])
                        ax.nodes[c+'-text'] = {'AXRole':'AXStaticText','AXValue':ax.nodes[c].pop('AXValue')}
                        # A button inside the text cell remains outside the data projection.
                        ax.add(f'cancel-row-{i}', c+'-text', AXRole='AXButton', AXTitle='撤单',
                               AXEnabled=True, AXParent=c+'-text')
                        ax.nodes[c+'-text']['AXParent'] = c
        self.cancelled = False
        ax.table = table
        ax.table('持仓')
        base_perform = ax.perform

        def perform(ref, action):
            if ref.startswith('nav-'):
                return base_perform(ref, action)
            ax.effects.append(('press', ref))
            if ref == 'submit':
                ax.add('order-sheet', AXRole='AXSheet', AXDescription='警告')
                ax.add('order-heading','order-sheet', AXRole='AXStaticText', AXValue='买入委托')
                ax.add('order-details','order-sheet', AXRole='AXStaticText', AXValue=
                       '股东账号:FIXTURE-HOLDER\n证券代码:600000\n报价方式:限价委托\n买入数量:100\n当前价格:10.00')
                ax.add('order-confirm','order-sheet', AXRole='AXButton', AXTitle='确认', AXEnabled=True)
                ax.add('order-dismiss','order-sheet', AXRole='AXButton', AXTitle='取消', AXEnabled=True)
            elif ref == 'order-confirm':
                ax.nodes['window']['AXChildren'].remove('order-sheet')
                ax.order_rows = [['20261127','09:30:00','600000','买入','100','10.00','FIXTURE-CONTRACT','已报']]
                ax.add('contract', AXRole='AXStaticText', AXIdentifier='submit-contract', AXValue='FIXTURE-CONTRACT')
            elif ref == 'cancel-row-0':
                contract = 'WRONG' if self.wrong_sheet else 'FIXTURE-CONTRACT'
                ax.add('cancel-sheet', AXRole='AXSheet', AXIdentifier='fixture-cancel-sheet', AXParent='window')
                ax.add('cancel-detail','cancel-sheet', AXRole='AXStaticText', AXIdentifier='fixture-cancel-detail', AXValue=
                       f'撤单委托\n合同编号:{contract}\n委托日期:20261127\n证券代码:600000\n方向:买入\n委托数量:100\n委托价格:10.00')
                ax.add('cancel-confirm','cancel-sheet', AXRole='AXButton', AXTitle='确认', AXEnabled=True)
            elif ref == 'cancel-confirm':
                ax.nodes['window']['AXChildren'].remove('cancel-sheet')
                self.cancelled = True
                ax.order_rows[0][-1] = '已撤'
                ax.table('委托')
            else:
                raise AssertionError(ref)
        ax.perform = perform
        policy = Path(self.config['account_policy_file'])
        create_policy(str(policy), 'FIXTURE-SZ', 'FIXTURE-HOLDER')
        self.service = wired.trade_api.create(self.config, synthetic_backend_factory=lambda lease:
            PymacautoBackend(dict(account_identity_kind=IDENTITY_KIND, private_policy_path=str(policy),
                                 allow_trade=True, allow_navigation=True), gui_owner=lease,
                            runtime_factory=lambda:self.runtime, profile=profile))
        import threading
        self.http = threading.Thread(target=self.service.server.serve_forever, kwargs={'poll_interval': .01})
        self.http.start()

    def test_submit_and_single_row_cancel_readback(self):
        self.submit()
        self.ax.order_rows.append([*self.ax.order_rows[0][:6], 'UNRELATED-CONTRACT', '已报'])
        status, response = self.post(wired.CANCEL, wired.CANCEL_BODY)
        self.assertEqual((status, response.get('status')), (200, 'BROKER_ORDER_CANCELLED'), response)
        self.assertEqual(self.ax.order_rows[1][-1], '已报')
        self.assertEqual(self.ax.nodes['table']['AXIdentifier'], '1537')
        self.assertEqual([x[1] for x in self.ax.effects if x[0]=='press' and not x[1].startswith('nav-')],
                         ['submit','order-confirm','cancel-row-0','cancel-confirm'])
        self.assertTrue(response['cancel_verified'])

    def test_refuses_mismatched_confirmation_without_confirm_or_replay(self):
        self.submit()
        self.wrong_sheet = True
        result = self.post(wired.CANCEL, wired.CANCEL_BODY)
        self.assertEqual((result[0], result[1].get('status')), (200, 'UNKNOWN'), result)
        self.assertIn(('press','cancel-row-0'), self.ax.effects)
        self.assertNotIn(('press','cancel-confirm'), self.ax.effects)
        before = list(self.ax.effects)
        self.assertEqual(self.post(wired.CANCEL, wired.CANCEL_BODY), result)
        self.assertEqual(self.ax.effects, before)

    def test_missing_row_control_never_falls_back_to_toolbar(self):
        self.submit()
        self.row_button = False
        result = self.post(wired.CANCEL, wired.CANCEL_BODY)
        self.assertNotEqual(result[1].get('status'), 'BROKER_ORDER_CANCELLED', result)
        self.assertFalse(any(ref.startswith('cancel-') or ref=='unsafe-toolbar' for kind,ref,*_ in self.ax.effects if kind=='press'))
