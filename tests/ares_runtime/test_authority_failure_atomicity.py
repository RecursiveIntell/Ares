"""Pure authority failure atomicity and temporal composition controls."""
import copy
import unittest
from ares_runtime import authority as AUTH

def state(authority):
    return copy.deepcopy(authority.__dict__)

class AuthorityProbes(unittest.TestCase):
    def parent(self, uses=2):
        return AUTH.AuthorityScopeV1(scope={'tool': 'write_file', 'target': 'path:/fixture', 'use_count': uses}, generation=1, holder='holder:root')

    def reserved(self):
        parent = self.parent()
        parent.reserve(consumption_ref='consume:1', args_digest='sha256:args', target_ref='path:/fixture')
        return parent

    def test_rejected_child_keeps_every_parent_field(self):
        for holder in ['', 7, []]:
            with self.subTest(holder=holder):
                parent = self.parent(1)
                before = state(parent)
                with self.assertRaises(AUTH.ContractError) as raised:
                    parent.attenuate({'use_count': 1}, child_generation=2, child_holder=holder)
                self.assertEqual(raised.exception.code, 'INVALID_HOLDER')
                self.assertEqual(state(parent), before)
                child = parent.attenuate({'use_count': 1}, child_generation=2, child_holder='holder:child')
                self.assertTrue(child.subset_witness(parent)['contained'])

    def test_valid_child_spends_once_and_cannot_duplicate(self):
        parent = self.parent(1)
        child = parent.attenuate({'use_count': 1}, child_generation=2)
        self.assertTrue(child.is_subset(parent))
        self.assertEqual(parent._delegated_count, 1)
        with self.assertRaises(AUTH.ContractError):
            parent.attenuate({'use_count': 1}, child_generation=2)

    def test_rejected_generation_and_scope_preserve_parent(self):
        parent = self.parent(1)
        before = state(parent)
        for scope, generation in [({}, 1), ({'target': 'path:/other'}, 2), ({'use_count': 2}, 2)]:
            with self.subTest(scope=scope, generation=generation):
                with self.assertRaises(AUTH.ContractError):
                    parent.attenuate(scope, child_generation=generation)
                self.assertEqual(state(parent), before)

    def test_invalid_commit_evidence_does_not_settle(self):
        for value in ['', '   ', None, 7, {}, float('nan'), float('inf')]:
            with self.subTest(value=repr(value)):
                parent = self.reserved()
                before = state(parent)
                with self.assertRaises(AUTH.ContractError) as raised:
                    parent.commit('consume:1', effect_receipt_digest=value)
                self.assertEqual(raised.exception.code, 'INVALID_EFFECT_RECEIPT_DIGEST')
                self.assertEqual(state(parent), before)
                receipt = parent.commit('consume:1', effect_receipt_digest='sha256:effect')
                self.assertEqual(receipt['record']['state'], 'committed')

    def test_invalid_release_reason_does_not_settle(self):
        for operation in ['release', 'mark_indeterminate']:
            for value in ['', '   ', None, float('nan')]:
                with self.subTest(operation=operation, value=repr(value)):
                    parent = self.reserved()
                    before = state(parent)
                    with self.assertRaises(AUTH.ContractError):
                        getattr(parent, operation)('consume:1', reason=value)
                    self.assertEqual(state(parent), before)

    def test_serializer_failure_leaves_settlement_reserved(self):
        for operation in ['commit', 'release', 'mark_indeterminate']:
            with self.subTest(operation=operation):
                parent = self.reserved()
                before = state(parent)
                original = AUTH.digest
                def fail(value):
                    if isinstance(value, dict) and 'record' in value:
                        raise ValueError('injected final receipt serializer failure')
                    return original(value)
                AUTH.digest = fail
                try:
                    with self.assertRaises(ValueError):
                        if operation == 'commit':
                            parent.commit('consume:1', effect_receipt_digest='sha256:effect')
                        else:
                            getattr(parent, operation)('consume:1', reason='fixture')
                finally:
                    AUTH.digest = original
                self.assertEqual(state(parent), before)
                parent.commit('consume:1', effect_receipt_digest='sha256:effect')

    def test_serializer_failure_leaves_reservation_unopened(self):
        parent = self.parent()
        before = state(parent)
        original = AUTH.digest
        def fail(value):
            if isinstance(value, dict) and 'record' in value:
                raise ValueError('injected final receipt serializer failure')
            return original(value)
        AUTH.digest = fail
        try:
            with self.assertRaises(ValueError):
                parent.reserve(consumption_ref='consume:1', args_digest='sha256:args', target_ref='path:/fixture')
        finally:
            AUTH.digest = original
        self.assertEqual(state(parent), before)

    def test_valid_settlements_preserve_accounting_and_digest(self):
        for operation, expected, charged in [('commit', 'committed', 1), ('release', 'released', 0), ('mark_indeterminate', 'indeterminate', 1)]:
            with self.subTest(operation=operation):
                parent = self.reserved()
                if operation == 'commit':
                    receipt = parent.commit('consume:1', effect_receipt_digest='sha256:effect')
                else:
                    receipt = getattr(parent, operation)('consume:1', reason='fixture')
                self.assertEqual(receipt['record']['state'], expected)
                self.assertEqual(parent.settlement('consume:1'), receipt['record'])
                self.assertEqual(receipt['open_reservations'], 0)
                self.assertEqual(receipt['charged_total'], charged)
                self.assertEqual(receipt['consumed_total'], charged)
                self.assertEqual(receipt['receipt_digest'], AUTH.digest({k:v for k,v in receipt.items() if k != 'receipt_digest'}))
                before = state(parent)
                with self.assertRaises(AUTH.ContractError):
                    parent.commit('consume:1', effect_receipt_digest='sha256:other')
                self.assertEqual(state(parent), before)

    def test_duplicate_and_unknown_refs_are_refused(self):
        parent = self.reserved()
        before = state(parent)
        with self.assertRaises(AUTH.ContractError):
            parent.reserve(consumption_ref='consume:1', args_digest='sha256:args', target_ref='path:/fixture')
        with self.assertRaises(AUTH.ContractError):
            parent.commit('consume:unknown', effect_receipt_digest='sha256:effect')
        self.assertEqual(state(parent), before)

    def test_pr125_temporal_subset_controls(self):
        whole = '2026-10-04T00:00:00Z'
        fraction = '2026-10-04T00:00:00.100000Z'
        for bound, parent, child, contained in [
            ('not_after', whole, fraction, False), ('not_after', fraction, whole, True),
            ('not_before', fraction, whole, False), ('not_before', whole, fraction, True),
            ('not_after', whole, '2026-10-04T01:00:00.000000+01:00', True),
            ('not_before', fraction, '2026-10-04T01:00:00.100000+01:00', True),
        ]:
            with self.subTest(bound=bound, parent=parent, child=child):
                self.assertEqual(AUTH.is_subset_scope({'time': {bound: child}}, {'time': {bound: parent}}), contained)

    def test_pr125_wire_fingerprint_control(self):
        scope = {'time': {'not_after': '2026-10-04T00:00:00Z'}}
        equivalent = {'time': {'not_after': '2026-10-04T01:00:00.000000+01:00'}}
        self.assertEqual(AUTH.normalize_scope(scope), scope)
        self.assertEqual(AUTH.scope_fingerprint(scope), AUTH.scope_fingerprint(equivalent))
