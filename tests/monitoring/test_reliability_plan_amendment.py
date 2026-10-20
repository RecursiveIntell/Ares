"""Approved amendments change criteria, never manufacture execution results."""
import copy
import hashlib
import json

import pytest

from scripts.ci.reliability_completion_projection import (
    ProjectionError, apply_plan_amendment, unresolved_subchecks,
)


@pytest.fixture
def amendment(tmp_path):
    old = 'absence=legacy total,600 unchanged'
    new = 'absence=legacy total with 0/no cap unchanged; explicit total/600 preserved'
    plan = tmp_path / 'plan.md'
    plan.write_text('Acceptance: ' + old)
    parent = hashlib.sha256(plan.read_bytes()).hexdigest()
    projection = {
        'source_inputs': {'COMPLETION_PLAN.md': {'path': str(plan), 'sha256': parent}},
        'completion_cards': [{'id': 'C04', 'fields': {'complete_card_markdown': plan.read_text()}}],
        'cases': {'B10': {'subchecks': ['legacy_total600'],
            'full_card_clauses': {'C04': {'complete_card_markdown': plan.read_text()}},
            'evidence': {'legacy_total600': {'status': 'not_run', 'bindings': []}},
            'status': 'not_qualified'}},
        'implementation_complete': False,
        'effect_authorization': False,
    }
    value = {'schema': 'AresCompletionPlanAmendmentV1', 'parent_plan_sha256': parent,
        'operator_instruction': 'neutral fixture approval',
        'changes': [{'card': 'C04', 'field': 'complete_card_markdown', 'old': old, 'new': new}],
        'case_interpretations': {'B10': 'explicit 600 remains a supported finite setting'},
        'implementation_complete': False, 'effect_authorization': False}
    path = tmp_path / 'amendment.json'
    return projection, value, path


def apply_fixture(fixture, value=None, digest=None):
    projection, original, path = fixture
    path.write_text(json.dumps(original if value is None else value))
    approved = hashlib.sha256(path.read_bytes()).hexdigest() if digest is None else digest
    return apply_plan_amendment(projection, path, approved_amendment_sha256=approved)


def test_approved_change_updates_all_card_views_without_changing_evidence(amendment):
    before = copy.deepcopy(amendment[0])
    after = apply_fixture(amendment)
    criterion = after['completion_cards'][0]['fields']['complete_card_markdown']
    assert '0/no cap unchanged' in criterion
    assert after['cases']['B10']['full_card_clauses']['C04']['complete_card_markdown'] == criterion
    assert after['cases']['B10']['evidence'] == before['cases']['B10']['evidence']
    assert unresolved_subchecks(after) == ['B10:legacy_total600']
    assert not after['implementation_complete'] and not after['effect_authorization']
    assert amendment[0] == before


def test_unapproved_digest_is_refused(amendment):
    with pytest.raises(ProjectionError, match='APPROVED_AMENDMENT_IDENTITY_MISMATCH'):
        apply_fixture(amendment, digest='0' * 64)


@pytest.mark.parametrize('change, error', [
    (lambda a: a.update(parent_plan_sha256='f' * 64), 'AMENDMENT_PARENT_IDENTITY_MISMATCH'),
    (lambda a: a.update(effect_authorization=True), 'AMENDMENT_SCHEMA_INVALID'),
    (lambda a: a.update(implementation_complete=True), 'AMENDMENT_SCHEMA_INVALID'),
    (lambda a: a['changes'][0].update(old='does not exist'), 'AMENDMENT_PREIMAGE_MISMATCH'),
    (lambda a: a['changes'][0].update(card='C99'), 'AMENDMENT_CHANGE_INVALID'),
    (lambda a: a['changes'][0].update(field='cases'), 'AMENDMENT_CHANGE_INVALID'),
    (lambda a: a['changes'].append(copy.deepcopy(a['changes'][0])), 'AMENDMENT_CHANGE_DUPLICATE'),
    (lambda a: a.update(case_interpretations={'B99': 'invented'}), 'AMENDMENT_CASE_INTERPRETATION_INVALID'),
])
def test_rebinding_or_manufactured_completion_is_refused(amendment, change, error):
    value = copy.deepcopy(amendment[1])
    change(value)
    with pytest.raises(ProjectionError, match=error):
        apply_fixture(amendment, value)


def test_missing_parent_cannot_equal_missing_amendment_parent(amendment):
    amendment[0]['source_inputs'].clear()
    amendment[1].pop('parent_plan_sha256')
    with pytest.raises(ProjectionError, match='AMENDMENT_PARENT_IDENTITY_MISMATCH'):
        apply_fixture(amendment)
