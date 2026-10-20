"""Compile approved subchecks into a source-bound obligation projection.

The original task partition is authoritative for A cases. Completion cards are
many-to-many scheduling/qualification links, not additional task owners. B cases
remain supplemental, card-linked obligations. Nothing here creates test PASS,
changes the canonical plans, or grants execution/publication/activation.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path


class ProjectionError(ValueError):
    pass


def compile_obligation_projection(packet: Path, draft_path: Path, *, approved_draft_sha256: str) -> dict:
    raw = draft_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != approved_draft_sha256:
        raise ProjectionError('APPROVED_SUBCHECK_IDENTITY_MISMATCH')
    draft = json.loads(raw)
    source_hashes = draft.get('source_hashes') if isinstance(draft, dict) else None
    required_sources = {'COMPLETION_PLAN.md', 'inherited/ORIGINAL_DAG.json',
                        'COMPLETION_DAG.json', 'COVERAGE.json'}
    if not isinstance(source_hashes, dict):
        raise ProjectionError('CANONICAL_SOURCE_BINDINGS_INVALID')
    missing = required_sources - source_hashes.keys()
    if missing:
        raise ProjectionError('CANONICAL_SOURCE_BINDING_MISSING: ' + ', '.join(sorted(missing)))
    inputs = {}
    admitted_bytes = {}
    packet_root = packet.resolve()
    for name, expected in source_hashes.items():
        if (not isinstance(name, str) or not name or Path(name).is_absolute()
                or '..' in Path(name).parts):
            raise ProjectionError('CANONICAL_SOURCE_PATH_INVALID')
        try:
            source = (packet / name).resolve(strict=True)
        except (OSError, ValueError) as exc:
            raise ProjectionError('CANONICAL_SOURCE_UNAVAILABLE: ' + name) from exc
        if not source.is_relative_to(packet_root):
            raise ProjectionError('CANONICAL_SOURCE_PATH_INVALID: ' + name)
        value = source.read_bytes()
        if hashlib.sha256(value).hexdigest() != expected:
            raise ProjectionError('CANONICAL_SOURCE_IDENTITY_MISMATCH: ' + name)
        inputs[name] = {'sha256': expected, 'path': str(packet / name)}
        admitted_bytes[name] = value
    # Parse the bytes whose identities were checked, not a second mutable read.
    original = json.loads(admitted_bytes['inherited/ORIGINAL_DAG.json'])
    current = json.loads(admitted_bytes['COMPLETION_DAG.json'])
    coverage = json.loads(admitted_bytes['COVERAGE.json'])
    if current.get('plan_sha256') != source_hashes['COMPLETION_PLAN.md']:
        raise ProjectionError('CANONICAL_PLAN_PROJECTION_MISMATCH')
    cases = {row['id']: row for row in coverage['cases']}
    if len(cases) != len(coverage['cases']):
        raise ProjectionError('CANONICAL_CASE_DUPLICATE')
    labels = draft['case_subchecks']
    if set(labels) != set(cases):
        raise ProjectionError('SUBCHECK_CASE_COVERAGE_MISMATCH')
    if any(not isinstance(v, list) or not v or len(v) != len(set(v))
           or any(not isinstance(x, str) or not x for x in v) for v in labels.values()):
        raise ProjectionError('SUBCHECK_LABELS_INVALID')
    owners = {}
    for task in original['tasks']:
        for case in task['cases']:
            if case in owners:
                raise ProjectionError('INHERITED_CASE_MULTIPLE_OWNERS')
            owners[case] = task['id']
    supplemental_ids = set(current['added_case_ids'])
    if set(owners) & supplemental_ids or set(owners) | supplemental_ids != set(cases):
        raise ProjectionError('INHERITED_SUPPLEMENTAL_PARTITION_INVALID')
    cards = {card['id']: card for card in current['cards']}
    if len(cards) != len(current['cards']):
        raise ProjectionError('CANONICAL_CARD_DUPLICATE')
    for case, row in cases.items():
        if row.get('inherited_task') != owners.get(case):
            raise ProjectionError('INHERITED_CASE_OWNER_MISMATCH: ' + case)
        if not row['completion_cards'] or any(card not in cards for card in row['completion_cards']):
            raise ProjectionError('CASE_CARD_BINDING_INVALID: ' + case)
    projected_cases = {}
    for case, row in cases.items():
        projected_cases[case] = {
            'definition': row['definition'],
            'controlling_inherited_task': owners.get(case),
            'supplemental': case in supplemental_ids,
            'completion_cards': list(row['completion_cards']),
            'subchecks': copy.deepcopy(labels[case]),
            'full_task_clauses': copy.deepcopy(next((t['fields'] for t in original['tasks']
                                                   if t['id'] == owners.get(case)), None)),
            'full_card_clauses': {name: copy.deepcopy(cards[name]['fields']) for name in row['completion_cards']},
            'evidence': {label: {'status': 'not_run', 'bindings': []} for label in labels[case]},
            'status': 'not_qualified',
        }
    return {
        'schema': 'AresCompletionObligationProjectionV1',
        'source_inputs': inputs,
        'approved_subchecks_sha256': approved_draft_sha256,
        'execution_boundary': 'through C09; C10-C13 retained, not authorized',
        'original_tasks': copy.deepcopy(original['tasks']),
        'original_gate_aggregates': copy.deepcopy(original['gate_aggregates']),
        'original_gate_dependencies': copy.deepcopy(original['gate_dependencies']),
        'completion_cards': copy.deepcopy(current['cards']),
        'completion_gate_links': copy.deepcopy(current['gates']),
        'cases': projected_cases,
        'issues': copy.deepcopy(coverage['issues']),
        'counts': {'inherited_tasks': len(original['tasks']), 'inherited_cases': len(owners),
                   'supplemental_cases': len(supplemental_ids), 'cases': len(cases),
                   'subchecks': sum(len(v) for v in labels.values()),
                   'inherited_gates': len(original['gate_aggregates']), 'completion_cards': len(cards)},
        'scope': 'Rebuildable obligation projection, not an implementation/result/authority ledger.',
        'implementation_complete': False,
        'effect_authorization': False,
    }


def apply_plan_amendment(projection: dict, amendment_path: Path, *, approved_amendment_sha256: str) -> dict:
    """Apply a separately approved, parent-bound criterion correction, never results."""
    raw = amendment_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != approved_amendment_sha256:
        raise ProjectionError('APPROVED_AMENDMENT_IDENTITY_MISMATCH')
    amendment = json.loads(raw)
    if (not isinstance(amendment, dict)
            or amendment.get('schema') != 'AresCompletionPlanAmendmentV1'
            or not isinstance(amendment.get('operator_instruction'), str)
            or not amendment['operator_instruction']
            or amendment.get('implementation_complete') is not False
            or amendment.get('effect_authorization') is not False):
        raise ProjectionError('AMENDMENT_SCHEMA_INVALID')
    parent = projection.get('source_inputs', {}).get('COMPLETION_PLAN.md', {}).get('sha256')
    if (not isinstance(parent, str) or len(parent) != 64
            or any(char not in '0123456789abcdef' for char in parent)
            or parent != amendment.get('parent_plan_sha256')):
        raise ProjectionError('AMENDMENT_PARENT_IDENTITY_MISMATCH')
    result = copy.deepcopy(projection)
    cards = {row['id']: row for row in result['completion_cards']}
    changes = amendment.get('changes')
    if not isinstance(changes, list) or not changes:
        raise ProjectionError('AMENDMENT_CHANGES_INVALID')
    seen = set()
    for change in changes:
        if (not isinstance(change, dict) or set(change) != {'card', 'field', 'old', 'new'}
                or change['card'] not in cards or change['field'] != 'complete_card_markdown'
                or any(not isinstance(change[k], str) or not change[k] for k in ('old', 'new'))):
            raise ProjectionError('AMENDMENT_CHANGE_INVALID')
        key = (change['card'], change['field'])
        if key in seen:
            raise ProjectionError('AMENDMENT_CHANGE_DUPLICATE')
        seen.add(key)
        fields = cards[change['card']]['fields']
        value = fields.get(change['field'])
        if not isinstance(value, str) or value.count(change['old']) != 1:
            raise ProjectionError('AMENDMENT_PREIMAGE_MISMATCH')
        fields[change['field']] = value.replace(change['old'], change['new'])
        for case in result['cases'].values():
            if change['card'] in case['full_card_clauses']:
                case['full_card_clauses'][change['card']] = copy.deepcopy(fields)
    interpretations = amendment.get('case_interpretations', {})
    if (not isinstance(interpretations, dict)
            or any(k not in result['cases'] or not isinstance(v, str) or not v
                   for k, v in interpretations.items())):
        raise ProjectionError('AMENDMENT_CASE_INTERPRETATION_INVALID')
    for case, interpretation in interpretations.items():
        result['cases'][case]['approved_interpretation'] = interpretation
    result.setdefault('plan_amendments', []).append({
        'path': str(amendment_path), 'sha256': approved_amendment_sha256,
        'parent_plan_sha256': parent, 'operator_instruction': amendment['operator_instruction'],
    })
    return result


def unresolved_subchecks(projection: dict) -> list[str]:
    """Index non-passing obligations without synthesizing evidence from labels."""
    return [case + ':' + label for case, row in projection['cases'].items()
            for label in row['subchecks']
            if row['evidence'].get(label, {}).get('status') != 'passed'
            or not row['evidence'].get(label, {}).get('bindings')]
