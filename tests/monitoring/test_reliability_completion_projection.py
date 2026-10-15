"""Projection compiler witnesses are not execution/case-qualification results."""
import hashlib
import json

import pytest
from scripts.ci.reliability_completion_projection import (
    ProjectionError, compile_obligation_projection, unresolved_subchecks,
)


@pytest.fixture
def packet(tmp_path):
    original = {'tasks': [{'id': '0.1', 'cases': ['A01'], 'fields': {'Gate': 'full original gate clause'}}],
                'gate_aggregates': {'G0': ['0.1']}, 'gate_dependencies': {'G0': []}}
    current = {'cards': [{'id': 'C00', 'fields': {'acceptance': 'full C00 clause'}},
                         {'id': 'C02', 'fields': {'acceptance': 'full C02 clause'}},
                         {'id': 'C10', 'fields': {'acceptance': 'later authorization still required'}}],
               'added_case_ids': ['B01'], 'gates': {'G0': {'completion_cards': ['C00']}}}
    coverage = {'cases': [{'id': 'A01', 'definition': 'source binding', 'inherited_task': '0.1', 'completion_cards': ['C00']},
                         {'id': 'B01', 'definition': 'media bytes', 'inherited_task': None, 'completion_cards': ['C02']}],
                'issues': [{'id': 'R01', 'status': 'unknown'}]}
    values = {'inherited/ORIGINAL_DAG.json': original, 'COMPLETION_DAG.json': current, 'COVERAGE.json': coverage}
    hashes = {}
    for name, value in values.items():
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(value))
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    draft = tmp_path / 'draft.json'
    draft.write_text(json.dumps({'source_hashes': hashes, 'case_subchecks': {'A01': ['source'], 'B01': ['bytes']}}))
    return tmp_path, draft


def compile_packet(packet):
    root, draft = packet
    return compile_obligation_projection(root, draft,
        approved_draft_sha256=hashlib.sha256(draft.read_bytes()).hexdigest())


def test_full_clauses_and_later_cards_are_preserved_but_no_case_is_passed(packet):
    value = compile_packet(packet)
    assert value['cases']['A01']['full_task_clauses'] == {'Gate': 'full original gate clause'}
    assert value['cases']['B01']['full_card_clauses']['C02'] == {'acceptance': 'full C02 clause'}
    assert value['cases']['B01']['controlling_inherited_task'] is None
    assert value['cases']['B01']['supplemental'] is True
    assert any(card['id'] == 'C10' for card in value['completion_cards'])
    assert value['effect_authorization'] is False
    assert value['implementation_complete'] is False
    assert unresolved_subchecks(value) == ['A01:source', 'B01:bytes']


def test_approved_digest_is_not_inferred_from_draft(packet):
    root, draft = packet
    with pytest.raises(ProjectionError, match='APPROVED_SUBCHECK_IDENTITY_MISMATCH'):
        compile_obligation_projection(root, draft, approved_draft_sha256='0' * 64)


def test_canonical_source_change_refuses_even_with_unchanged_approved_draft(packet):
    root, _ = packet
    (root / 'COVERAGE.json').write_text('{}')
    with pytest.raises(ProjectionError, match='CANONICAL_SOURCE_IDENTITY_MISMATCH'):
        compile_packet(packet)


def test_labels_and_status_without_evidence_do_not_close_obligations(packet):
    value = compile_packet(packet)
    value['cases']['A01']['status'] = 'passed'
    value['cases']['A01']['evidence']['source']['status'] = 'passed'
    assert 'A01:source' in unresolved_subchecks(value)
