"""Projection compiler witnesses are not execution/case-qualification results."""
import hashlib
import json

import pytest
from scripts.ci.reliability_completion_projection import (
    ProjectionError, compile_obligation_projection, unresolved_subchecks,
)


@pytest.fixture
def packet(tmp_path):
    master = 'Neutral master plan; complete clauses remain authoritative.\n'
    original = {'tasks': [{'id': '0.1', 'cases': ['A01'], 'fields': {'Gate': 'full original gate clause'}}],
                'gate_aggregates': {'G0': ['0.1']}, 'gate_dependencies': {'G0': []}}
    current = {'plan_sha256': hashlib.sha256(master.encode()).hexdigest(),
               'cards': [{'id': 'C00', 'fields': {'acceptance': 'full C00 clause'}},
                         {'id': 'C02', 'fields': {'acceptance': 'full C02 clause'}},
                         {'id': 'C10', 'fields': {'acceptance': 'later authorization still required'}}],
               'added_case_ids': ['B01'], 'gates': {'G0': {'completion_cards': ['C00']}}}
    coverage = {'cases': [{'id': 'A01', 'definition': 'source binding', 'inherited_task': '0.1', 'completion_cards': ['C00']},
                         {'id': 'B01', 'definition': 'media bytes', 'inherited_task': None, 'completion_cards': ['C02']}],
                'issues': [{'id': 'R01', 'status': 'unknown'}]}
    values = {'COMPLETION_PLAN.md': master, 'inherited/ORIGINAL_DAG.json': original,
              'COMPLETION_DAG.json': current, 'COVERAGE.json': coverage}
    hashes = {}
    for name, value in values.items():
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))
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


def reapprove_draft(packet, mutate):
    root, draft = packet
    value = json.loads(draft.read_text())
    mutate(value)
    draft.write_text(json.dumps(value))
    return root, draft


@pytest.mark.parametrize('name', [
    'COMPLETION_PLAN.md', 'inherited/ORIGINAL_DAG.json',
    'COMPLETION_DAG.json', 'COVERAGE.json',
])
def test_every_consumed_source_requires_an_approved_binding(packet, name):
    reapprove_draft(packet, lambda value: value['source_hashes'].pop(name))
    with pytest.raises(ProjectionError, match='CANONICAL_SOURCE_BINDING_MISSING'):
        compile_packet(packet)


def test_duplicate_card_identity_cannot_silently_replace_clauses(packet):
    root, draft = packet
    path = root / 'COMPLETION_DAG.json'
    value = json.loads(path.read_text())
    value['cards'].append({'id': 'C00', 'fields': {'acceptance': 'replacement'}})
    path.write_text(json.dumps(value))
    reapprove_draft(packet, lambda value: value['source_hashes'].update({
        'COMPLETION_DAG.json': hashlib.sha256(path.read_bytes()).hexdigest()}))
    with pytest.raises(ProjectionError, match='CANONICAL_CARD_DUPLICATE'):
        compile_packet(packet)


def test_stale_master_projection_cannot_pass_by_reapproving_its_file_hash(packet):
    root, draft = packet
    path = root / 'COMPLETION_DAG.json'
    value = json.loads(path.read_text())
    value['plan_sha256'] = '0' * 64
    path.write_text(json.dumps(value))
    reapprove_draft(packet, lambda value: value['source_hashes'].update({
        'COMPLETION_DAG.json': hashlib.sha256(path.read_bytes()).hexdigest()}))
    with pytest.raises(ProjectionError, match='CANONICAL_PLAN_PROJECTION_MISMATCH'):
        compile_packet(packet)


@pytest.mark.parametrize('name', ['/absolute-source.md', '../foreign-source.md'])
def test_source_paths_cannot_escape_the_admitted_packet(packet, name):
    reapprove_draft(packet, lambda value: value['source_hashes'].update({name: '0' * 64}))
    with pytest.raises(ProjectionError, match='CANONICAL_SOURCE_PATH_INVALID'):
        compile_packet(packet)


@pytest.mark.linux_only
def test_hashed_source_symlink_cannot_escape_packet(packet):
    root, _ = packet
    source = root / 'COMPLETION_PLAN.md'
    outside = root.parent / 'foreign-master.md'
    outside.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(outside)
    with pytest.raises(ProjectionError, match='CANONICAL_SOURCE_PATH_INVALID'):
        compile_packet(packet)


def test_consumers_parse_the_exact_bytes_admitted_by_the_hash_check(packet, monkeypatch):
    root, _ = packet
    path_type = type(root)
    original = path_type.read_bytes
    reads = {}
    consumed = {'inherited/ORIGINAL_DAG.json', 'COMPLETION_DAG.json', 'COVERAGE.json'}

    def once(path):
        try:
            name = str(path.relative_to(root))
        except ValueError:
            name = ''
        if name in consumed:
            reads[name] = reads.get(name, 0) + 1
            assert reads[name] == 1, 'consumer reread mutable source after identity admission'
        return original(path)

    monkeypatch.setattr(path_type, 'read_bytes', once)
    value = compile_packet(packet)
    assert reads == {name: 1 for name in consumed}
    assert value['cases']['A01']['definition'] == 'source binding'
    assert value['implementation_complete'] is False
    assert value['effect_authorization'] is False
