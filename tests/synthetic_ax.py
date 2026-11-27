"""SYNTHETIC AX fixtures; real pinned pymacauto, never macOS acceptance."""
import copy
import json
import os
from pathlib import Path
import sys
import threading
import unittest




class SyntheticAX:
    def __init__(self):
        self.calls = []
        self.effects = []
        self.nodes = {
            'app': {'AXWindows': ['window']},
            'window': {'AXRole': 'AXWindow', 'AXIdentifier': '主窗口', 'AXChildren': ['content', 'broker']},
            'content': {'AXRole': 'AXScrollArea', 'AXIdentifier': '_NS:210', 'AXChildren': ['table']},
            'broker': {'AXRole': 'AXStaticText', 'AXValue': '天府证券'},
        }
        self.table('持仓')

    def table(self, page):
        headers, values = {
            '持仓': (['证券代码', '股票余额', '可用余额', '成本价', '交易市场', '股东账户'],
                     [['600000', '200', '100', '10.50', '上海Ａ股', 'SYNTHETIC-HOLDER']]),
            '委托': (['委托日期', '委托时间', '证券代码', '操作', '委托数量', '委托价格', '合同编号', '备注'], []),
            '成交': (['成交日期', '成交时间', '证券代码', '操作', '成交数量', '成交均价', '成交金额', '合同编号', '成交编号'],
                     [['20261113', '10:00:00', '600000', '买入', '100', '10.00', '1000.00', 'C-FIXTURE', 'T-FIXTURE']]),
        }[page]
        if page == '委托':
            values = getattr(self, 'order_rows', [])
        ident = {'持仓': '1537', '委托': '1539', '成交': '1538'}[page]
        self.nodes['table'] = {'AXRole': 'AXTable', 'AXIdentifier': ident, 'AXColumns': [], 'AXRows': []}
        for j, header in enumerate(headers):
            col, head = f'col{j}', f'head{j}'
            self.nodes['table']['AXColumns'].append(col)
            self.nodes[col] = {'AXIndex': j, 'AXHeader': head, 'AXRows': []}
            self.nodes[head] = {'AXTitle': header}
        for i, row in enumerate(values):
            ref = f'row{i}'
            self.nodes['table']['AXRows'].append(ref)
            self.nodes[ref] = {'AXIndex': i, 'AXChildren': []}
            for j, value in enumerate(row):
                cell = f'cell{i}_{j}'
                self.nodes[ref]['AXChildren'].append(cell)
                self.nodes[f'col{j}']['AXRows'].append(cell)
                self.nodes[cell] = {'AXRole': 'AXStaticText', 'AXValue': value}

    def trusted(self, prompt=False):
        assert prompt is False
        return True

    def apps(self):
        return [{'pid': 456, 'bundle_id': 'cn.com.10jqka.macstockPro'}]

    def root(self, pid):
        assert pid == 456
        return 'app'

    def key(self, ref):
        return ref

    def get(self, ref, attr):
        self.calls.append((ref, attr))
        return self.nodes[ref].get(attr)

    def actions(self, ref):
        return ['AXPress'] if self.nodes[ref].get('AXRole') == 'AXButton' else []

    def set(self, ref, attr, value):
        self.effects.append(('set', ref, value))
        self.nodes[ref][attr] = value

    def perform(self, ref, action):
        self.effects.append(('press', ref))
        if ref.startswith('nav-'):
            self.table(ref[4:])
        if ref == 'submit':
            self.show_sheet()
        if ref == 'confirm':
            self.nodes['window']['AXChildren'].remove('sheet')
            self.order_rows = [['20261113', '10:00:00', '600000', '买入', '100', '10.00', 'NEW-FIXTURE', '已成']]
            self.add('contract-hint', AXRole='AXStaticText', AXIdentifier='fixture-contract', AXValue='NEW-FIXTURE')

    def add(self, ref, parent='window', **attrs):
        self.nodes[ref] = attrs
        self.nodes[parent].setdefault('AXChildren', []).append(ref)

class SyntheticRuntime:
    evidence_domain = 'SYNTHETIC'

    def __init__(self):
        self.backend = SyntheticAX()
        self.identity = {'pid': 456, 'bundle_id': 'cn.com.10jqka.macstockPro', 'version': '5.3.5',
                         'build': '7.0.62', 'path': '/Applications/同花顺.app', 'owner_uid': os.getuid()}

    def app_identity(self):
        return dict(self.identity)

    def front(self):
        return 123
