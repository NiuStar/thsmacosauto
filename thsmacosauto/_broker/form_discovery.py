"""Read-only THS observed-form association; never action/transaction authority.

Uses only backend.get/key. NativeBackend.key is CFHash/CFEqual-backed; callers
must not substitute stringified references. Only explicit window direct children
are searched. No AXTextField values or unrelated static text are read.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum
import math
from types import MappingProxyType

from .._ax.errors import AttributeUnsupported


class DiscoveryError(Exception):
    """Bounded structural error code, never private native exception text."""


POLICY = 'THS_SAME_ROW_V1'
ROW_CENTER_TOLERANCE = 4.0
MAX_LABEL_FIELD_GAP = 64.0
MAX_CHILDREN = 512
LABELS = {'code': ('_NS:14', '代码'), 'price': ('_NS:37', '价格'),
          'quantity': ('_NS:65', '数量')}


def _fail(code='FORM_ASSOCIATION_UNPROVEN'):
    raise DiscoveryError(code)


def _get(backend, ref, attr):
    try:
        return backend.get(ref, attr)
    except AttributeUnsupported:
        return None


def _rect(backend, ref):
    position = _get(backend, ref, 'AXPosition')
    size = _get(backend, ref, 'AXSize')
    if not isinstance(position, dict) or not isinstance(size, dict):
        _fail()
    values = [position.get('x'), position.get('y'), size.get('width'), size.get('height')]
    try:
        valid = all(type(v) in (int, float) and math.isfinite(v) for v in values)
    except (OverflowError, TypeError):
        valid = False
    if not valid or values[2] <= 0 or values[3] <= 0:
        _fail()
    x, y, width, height = values
    if not all(math.isfinite(v) for v in (x + width, y + height, y + height / 2)):
        _fail()
    return {'x': x, 'y': y, 'width': width, 'height': height}


def _same_row(label, target):
    gap = target['x'] - (label['x'] + label['width'])
    center = lambda r: r['y'] + r['height'] / 2
    return (0 <= gap <= MAX_LABEL_FIELD_GAP
            and abs(center(label) - center(target)) <= ROW_CENTER_TOLERANCE
            and max(label['y'], target['y']) < min(label['y'] + label['height'],
                                                                target['y'] + target['height']))


@dataclass(frozen=True, repr=False)
class Discovery:
    """Ephemeral bindings, not serializable/persistent action locators."""
    fields: object = field(repr=False)
    _facts: dict = field(repr=False)
    _keys: tuple = field(repr=False)
    _backend: object = field(repr=False)

    def facts(self):
        """JSON-safe structure only; never reference keys/values/order intent."""
        return copy.deepcopy(self._facts)


class Confirmation(Enum):
    BUY = '确定买入'
    SELL = '确定卖出'


def _sample(backend, scope, *, confirmation=Confirmation.SELL):
    if (_get(backend, scope, 'AXRole') != 'AXWindow'
            or _get(backend, scope, 'AXIdentifier') != '主窗口'):
        _fail('FORM_SCOPE_UNPROVEN')
    scope_key = backend.key(scope)
    children = _get(backend, scope, 'AXChildren')
    if not isinstance(children, (list, tuple)) or len(children) > MAX_CHILDREN:
        _fail('FORM_SCOPE_INCOMPLETE')
    nodes, seen, topology, editable = [], set(), [], []
    for ref in children:
        key = backend.key(ref)
        if key == scope_key or key in seen:
            _fail('FORM_SCOPE_INCOMPLETE')
        seen.add(key)
        role = _get(backend, ref, 'AXRole')
        subrole = _get(backend, ref, 'AXSubrole')
        topology.append((key, role, subrole))
        if role not in ('AXStaticText', 'AXTextField', 'AXButton'):
            continue
        if subrole == 'AXSecureTextField':
            # No values/labels/children of secure elements are inspected.
            continue
        identifier = _get(backend, ref, 'AXIdentifier')
        relevant_label = (role == 'AXStaticText'
                          and identifier in {v[0] for v in LABELS.values()} | {'_NS:104'})
        if role == 'AXStaticText' and not relevant_label:
            continue
        parent = _get(backend, ref, 'AXParent')
        parent_key = None if parent is None else backend.key(parent)
        if parent_key != scope_key:
            _fail('FORM_PARENT_MISMATCH')
        node = {'ref': ref, 'key': key, 'role': role, 'identifier': identifier}
        if role in ('AXStaticText', 'AXButton'):
            node['text'] = _get(backend, ref, 'AXValue' if relevant_label else 'AXTitle')
        if role == 'AXTextField' or relevant_label or node.get('text') in ('买入', '卖出', '确定卖出', '确定买入'):
            node['bounds'] = _rect(backend, ref)
            if role != 'AXStaticText':
                enabled = _get(backend, ref, 'AXEnabled')
                if enabled is not True:
                    _fail()
            if role == 'AXTextField':
                # Freeze every sampled field, not only the three row matches.
                if identifier is not None and type(identifier) is not str:
                    _fail()
                editable.append((key, role, subrole, parent_key, identifier, enabled,
                                 tuple(node['bounds'][k] for k in ('x', 'y', 'width', 'height'))))
        nodes.append(node)

    def unique(role, text, identifier=None):
        found = [n for n in nodes if n['role'] == role and n.get('text') == text
                 and (identifier is None or n['identifier'] == identifier)]
        if len(found) != 1:
            _fail()
        return found[0]

    fields, descriptors, keys = {}, {}, [scope_key]
    for name, (identifier, text) in LABELS.items():
        label = unique('AXStaticText', text, identifier)
        candidates = [n for n in nodes if n['role'] == 'AXTextField'
                      and _same_row(label['bounds'], n['bounds'])]
        if len(candidates) != 1:
            _fail()
        target = candidates[0]
        fields[name] = target['ref']
        keys.extend([label['key'], target['key']])
        descriptors[name] = {
            'label': {'role': 'AXStaticText', 'identifier': identifier, 'text': text,
                      'bounds': label['bounds']},
            'field': {'role': 'AXTextField', 'identifier': target['identifier'],
                      'bounds': target['bounds']},
        }
    if len({backend.key(ref) for ref in fields.values()}) != len(LABELS):
        _fail()
    confirmations = [n for n in nodes if n['role'] == 'AXButton'
                     and n.get('text') in {c.value for c in Confirmation}]
    if (len(confirmations) != 1 or (confirmation is not None
            and confirmations[0]['text'] != confirmation.value)):
        _fail()
    observed = confirmations[0]['text']
    controls = [unique('AXButton', text) for text in ('买入', '卖出', observed)]
    controls.append(unique('AXStaticText', '限价委托', '_NS:104'))
    keys.extend(n['key'] for n in controls)
    facts = {
        'association': 'SAME_PARENT_LABEL_GEOMETRY', 'policy': POLICY,
        'scope': {'role': 'AXWindow', 'identifier': '主窗口', 'children_only': True},
        'fields': descriptors,
        'controls': [{'role': n['role'], 'identifier': n['identifier'],
                      'text': n['text'], 'bounds': n['bounds']} for n in controls],
        'observed_confirmation': observed, 'transaction_qualified': False,
        'execution_enabled': False, 'monetary_authority': 'NONE',
    }
    return Discovery(MappingProxyType(fields), facts,
                     (scope_key, frozenset(topology), frozenset(editable), *keys[1:]), backend)


def _discover(backend, scope, *, confirmation):
    if confirmation is not None and type(confirmation) is not Confirmation:
        _fail()
    first = _sample(backend, scope, confirmation=confirmation)
    second = _sample(backend, scope, confirmation=confirmation)
    if first._keys != second._keys or first._facts != second._facts:
        _fail('FORM_STRUCTURE_CHANGED')
    return second


def discover(backend, scope):
    """Two fresh read-only samples of one caller-resolved main AXWindow.

    This observed sell-form shape does not prove active direction, callback
    acceptance, broker/account identity, authentication or permission to fill.
    """
    return _discover(backend, scope, confirmation=Confirmation.SELL)


def re_resolve(backend, scope, previous):
    """Re-run role + label + geometry matching, then require binding continuity.

    No ID-only lookup, cached successful locator or persisted reference is used.
    Callers must discard a failed proof; this still grants no write authority.
    """
    current = discover(backend, scope)
    if (not isinstance(previous, Discovery) or previous._backend is not backend
            or previous._keys != current._keys or previous._facts != current._facts):
        _fail('FORM_STRUCTURE_CHANGED')
    return current
