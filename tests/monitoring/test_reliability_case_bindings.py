"""Literal case/subcheck bindings against real neutral runner exports.

Fixtures are parser/consistency witnesses, not program acceptance or authority.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from scripts.ci.reliability_qualification import validate_dag, validate_qualification

PLAN = 'a' * 64
CANDIDATE = 'b' * 64
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def executed_fixture(tmp_path_factory):
    base = tmp_path_factory.mktemp('literal-case-export')
    path = base / 'test_neutral_example.py'
    path.write_text("import pytest\n"
                    "def test_source_binding():\n    assert True\n"
                    "def test_neutral_inputs():\n    assert True\n"
                    "@pytest.mark.parametrize('label', ['one','two'])\n"
                    "def test_literal_route(label):\n    assert label in {'one','two'}\n")
    output = base / 'export'
    result = subprocess.run([sys.executable, str(ROOT/'scripts/run_tests_parallel.py'),
        '-j1', '--file-retries', '0', '--file-timeout', '30', '--evidence-dir', str(output),
        str(path), '-q'], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt_path, = output.glob('*/attempt-*/receipt.json')
    attempt = json.loads(receipt_path.read_text())
    assert attempt['pytest_return_code'] == 0
    nodes = {node.get('name'): node.get('classname')
             for node in ET.parse(output/attempt['junit']['path']).iter('testcase')}
    assert set(nodes) == {'test_source_binding', 'test_neutral_inputs',
                          'test_literal_route[one]', 'test_literal_route[two]'}
    return base, receipt_path.relative_to(base).as_posix(), attempt['file'], nodes


@pytest.fixture
def example(executed_fixture, tmp_path):
    base, relative, file, nodes = executed_fixture
    root = tmp_path/'artifacts'
    shutil.copytree(base, root)
    reference = {'path': relative, 'sha256': hashlib.sha256((root/relative).read_bytes()).hexdigest()}
    def binding(name):
        return {'candidate_sha256': CANDIDATE, 'export_root': 'export',
                'attempt': copy.deepcopy(reference),
                'file': file, 'classname': nodes[name], 'name': name}
    subchecks = {'A01': ['source', 'neutral'], 'A02': ['literal_one', 'literal_two']}
    dag = {'plan_sha256': PLAN, 'task_count': 2, 'case_count': 2, 'gate_count': 2,
           'gate_aggregates': {'G0': ['0.1'], 'G1': ['1.1']},
           'case_subchecks': subchecks,
           'tasks': [{'id': '0.1', 'depends_on': [], 'cases': ['A01'], 'issues': ['R01']},
                     {'id': '1.1', 'depends_on': ['0.1'], 'cases': ['A02'], 'issues': ['R02']}]}
    case_evidence = {'A01': {'source': [binding('test_source_binding')],
                            'neutral': [binding('test_neutral_inputs')]},
                     'A02': {'literal_one': [binding('test_literal_route[one]')],
                            'literal_two': [binding('test_literal_route[two]')]}}
    receipt = {'schema': 'AresReliabilityQualificationV2', 'plan_sha256': PLAN,
        'candidate_sha256': CANDIDATE,
        'tasks': {t['id']: {'status': 'passed', 'candidate_sha256': CANDIDATE,
            'cases': {c: 'passed' for c in t['cases']},
            'case_evidence': {c: case_evidence[c] for c in t['cases']},
            'evidence': [copy.deepcopy(reference)]} for t in dag['tasks']},
        'gates': {g: {'status': 'passed', 'candidate_sha256': CANDIDATE, 'tasks': list(members)}
                  for g, members in dag['gate_aggregates'].items()},
        'issues': {'R01': {'status': 'repaired'}, 'R02': {'status': 'repaired'}}, 'all_fixed': True}
    return dag, receipt, root


def check(example):
    dag, receipt, root = example
    return validate_qualification(dag, receipt, expected_plan_sha256=PLAN,
        expected_candidate_sha256=CANDIDATE, artifact_root=root)


def first_binding(example):
    return example[1]['tasks']['0.1']['case_evidence']['A01']['source'][0]


def rewrite_attempt(example, mutation=None, xml_mutation=None):
    _, receipt, root = example
    reference = first_binding(example)['attempt']
    path = root/reference['path']
    attempt = json.loads(path.read_text())
    if xml_mutation is not None:
        xml_path = root/'export'/attempt['junit']['path']
        tree = ET.parse(xml_path)
        xml_mutation(tree)
        tree.write(xml_path, encoding='utf-8')
        attempt['junit']['sha256'] = hashlib.sha256(xml_path.read_bytes()).hexdigest()
    if mutation is not None:
        mutation(attempt)
    path.write_text(json.dumps(attempt))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    for task in receipt['tasks'].values():
        task['evidence'][0]['sha256'] = digest
        for subchecks in task['case_evidence'].values():
            for bindings in subchecks.values():
                for binding in bindings:
                    binding['attempt']['sha256'] = digest


def test_exact_executed_case_binding_is_consistent_not_an_effect_grant(example):
    assert check(example) == []


@pytest.mark.parametrize('mode', ['missing', 'legacy', 'unknown'])
def test_legacy_or_unknown_unbound_receipt_cannot_qualify(example, mode):
    _, receipt, _ = example
    if mode == 'missing':
        receipt.pop('schema')
    else:
        receipt['schema'] = 'AresReliabilityQualificationV1' if mode == 'legacy' else 'future'
    assert 'QUALIFICATION_SCHEMA_INVALID' in check(example)


@pytest.mark.parametrize('mutation, error', [
    (lambda d: d.pop('case_subchecks'), 'DAG_SUBCHECK_COVERAGE_INVALID'),
    (lambda d: d['case_subchecks'].pop('A01'), 'DAG_SUBCHECK_COVERAGE_INVALID'),
    (lambda d: d['case_subchecks'].update(extra=['invented']), 'DAG_SUBCHECK_COVERAGE_INVALID'),
    (lambda d: d['case_subchecks'].update(A01=[]), 'DAG_SUBCHECKS_INVALID'),
    (lambda d: d['case_subchecks'].update(A01=['source','source']), 'DAG_SUBCHECKS_INVALID'),
])
def test_subchecks_are_owned_by_approved_dag_not_receipt(example, mutation, error):
    mutation(example[0])
    assert error in validate_dag(example[0])


@pytest.mark.parametrize('mutation, error', [
    (lambda t: t.pop('case_evidence'), 'CASE_EVIDENCE_COVERAGE_MISMATCH'),
    (lambda t: t['case_evidence'].update(A99={}), 'CASE_EVIDENCE_COVERAGE_MISMATCH'),
    (lambda t: t['case_evidence']['A01'].pop('neutral'), 'SUBCHECK_EVIDENCE_COVERAGE_MISMATCH'),
    (lambda t: t['case_evidence']['A01'].update(extra=[]), 'SUBCHECK_EVIDENCE_COVERAGE_MISMATCH'),
    (lambda t: t['case_evidence']['A01'].update(source=[]), 'SUBCHECK_EVIDENCE_MISSING'),
])
def test_declared_pass_does_not_replace_subcheck_evidence(example, mutation, error):
    mutation(example[1]['tasks']['0.1'])
    assert error in check(example)


@pytest.mark.parametrize('field, value, error', [
    ('file', 'tests/different.py', 'CASE_TEST_FILE_MISMATCH'),
    ('classname', 'OtherClass', 'CASE_TEST_NODE_MISSING'),
    ('name', 'test_literal_route[uncollected]', 'CASE_TEST_NODE_MISSING'),
    ('candidate_sha256', 'c'*64, 'CASE_CANDIDATE_MISMATCH'),
    ('unexpected_field', True, 'CASE_TEST_BINDING_INVALID'),
])
def test_file_class_parameter_and_candidate_are_literal(example, field, value, error):
    first_binding(example)[field] = value
    assert error in check(example)


@pytest.mark.parametrize('mutation, error', [
    (lambda a: a.update(pytest_return_code=1, runner_return_code=0), 'CASE_ATTEMPT_NOT_PASSED'),
    (lambda a: a.update(pytest_return_code=5, runner_return_code=0), 'CASE_ATTEMPT_NOT_PASSED'),
    (lambda a: a.update(pytest_return_code=True), 'CASE_ATTEMPT_NOT_PASSED'),
    (lambda a: a.update(timed_out=True), 'CASE_ATTEMPT_NOT_PASSED'),
    (lambda a: a.update(attempt_completed=False), 'CASE_ATTEMPT_NOT_PASSED'),
    (lambda a: a.update(attempt=2), 'CASE_RETRY_EVIDENCE_NOT_QUALIFIED'),
    (lambda a: a.update(test_sha256_after='d'*64), 'CASE_TEST_SOURCE_DRIFT'),
    (lambda a: a.update(schema='future'), 'CASE_ATTEMPT_SCHEMA_INVALID'),
    (lambda a: a.update(unexpected=True), 'CASE_ATTEMPT_SCHEMA_INVALID'),
    (lambda a: a.pop('junit'), 'CASE_ATTEMPT_SCHEMA_INVALID'),
    (lambda a: a.update(junit=None), 'EVIDENCE_IDENTITY_INVALID'),
])
def test_failed_incomplete_or_retry_attempt_is_not_case_pass(example, mutation, error):
    rewrite_attempt(example, mutation)
    assert error in check(example)


@pytest.mark.parametrize('outcome', ['failure','error','skipped'])
def test_re_signed_failed_junit_node_cannot_hide_under_pass_labels(example, outcome):
    rewrite_attempt(example, xml_mutation=lambda tree: ET.SubElement(
        tree.find('.//testcase'), outcome, {'message': 'neutral negative witness'}))
    assert 'CASE_TEST_NODE_NOT_PASSED' in check(example)


def test_duplicate_junit_identity_cannot_be_arbitrarily_selected(example):
    def duplicate(tree):
        suite = tree.find('.//testsuite')
        suite.append(copy.deepcopy(suite.find('testcase')))
    rewrite_attempt(example, xml_mutation=duplicate)
    assert 'CASE_TEST_NODE_AMBIGUOUS' in check(example)


def test_duplicate_subcheck_binding_is_not_additional_coverage(example):
    values = example[1]['tasks']['0.1']['case_evidence']['A01']['source']
    values.append(copy.deepcopy(values[0]))
    assert 'CASE_TEST_BINDING_DUPLICATE' in check(example)


def test_junit_digest_mismatch_is_not_qualified(example):
    rewrite_attempt(example, lambda a: a['junit'].update(sha256='d'*64))
    assert 'EVIDENCE_DIGEST_MISMATCH' in check(example)


def test_unreadable_junit_is_not_qualified(example):
    rewrite_attempt(example, lambda a: a['junit'].update(path='missing.xml'))
    assert 'EVIDENCE_UNREADABLE' in check(example)


def test_attempt_artifact_paths_cannot_escape_export_root(example):
    rewrite_attempt(example, lambda a: a['stdout'].update(path='../foreign.log'))
    assert 'EVIDENCE_PATH_OUTSIDE_ROOT' in check(example)


@pytest.mark.parametrize('value', [True, -1, float('nan'), float('inf'), 10**400])
def test_invalid_or_unrepresentable_attempt_wall_is_typed_refusal(example, value):
    rewrite_attempt(example, lambda a: a.update(wall_seconds=value))
    assert 'CASE_ATTEMPT_SCHEMA_INVALID' in check(example)


def test_malformed_attempt_json_is_not_case_evidence(example):
    _, receipt, root = example
    reference = first_binding(example)['attempt']
    path = root/reference['path']
    path.write_bytes(b'{invalid json')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    for task in receipt['tasks'].values():
        task['evidence'][0]['sha256'] = digest
        for subchecks in task['case_evidence'].values():
            for bindings in subchecks.values():
                for binding in bindings:
                    binding['attempt']['sha256'] = digest
    assert 'CASE_ATTEMPT_SCHEMA_INVALID' in check(example)


def test_export_scope_is_checked_before_attempt_read(example, monkeypatch):
    from scripts.ci import reliability_qualification as checker
    first_binding(example)['export_root'] = 'other-export'
    opened = []
    original = checker.os.open
    def observe(path, flags, *args, **kwargs):
        opened.append(Path(path))
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(checker.os, 'open', observe)
    errors = []
    checker._validate_test_binding(example[2], first_binding(example), CANDIDATE, errors)
    assert 'EVIDENCE_PATH_OUTSIDE_ROOT' in errors
    assert not opened


def test_junit_symlink_cannot_borrow_outside_artifact_scope(example, tmp_path):
    _, _, root = example
    outside = tmp_path/'outside.xml'
    outside.write_text('<testsuites/>')
    (root/'export'/'outside.xml').symlink_to(outside)
    rewrite_attempt(example, lambda a: a['junit'].update(
        path='outside.xml', sha256=hashlib.sha256(outside.read_bytes()).hexdigest()))
    assert 'EVIDENCE_PATH_OUTSIDE_ROOT' in check(example)


def test_junit_doctype_is_not_a_runner_receipt(example):
    def replace(attempt):
        path = example[2]/'export'/attempt['junit']['path']
        path.write_bytes(b'<!DOCTYPE testsuites [<!ENTITY x "neutral">]><testsuites/>')
        attempt['junit']['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    rewrite_attempt(example, replace)
    assert 'CASE_JUNIT_SCHEMA_INVALID' in check(example)


@pytest.mark.linux_only
def test_named_pipe_artifact_is_refused_without_reading(tmp_path):
    from scripts.ci.reliability_qualification import _read_evidence
    path = tmp_path/'artifact.fifo'
    os.mkfifo(path)
    errors = []
    assert _read_evidence(tmp_path, {'path': path.name, 'sha256': 'a'*64}, errors) is None
    assert errors == ['EVIDENCE_UNREADABLE']


def test_oversized_sparse_artifact_refuses_before_content_hash(tmp_path):
    from scripts.ci.reliability_qualification import _MAX_ARTIFACT_BYTES, _read_evidence
    path = tmp_path/'large.log'
    with path.open('wb') as stream:
        stream.truncate(_MAX_ARTIFACT_BYTES+1)
    errors = []
    assert _read_evidence(tmp_path, {'path': path.name, 'sha256': 'a'*64}, errors) is None
    assert errors == ['EVIDENCE_TOO_LARGE']


@pytest.mark.parametrize('approved', [True, False])
def test_real_cli_preserves_proof_and_authority_boundaries(example, approved):
    dag, receipt, root = example
    dag_path = root/'dag.json'
    receipt_path = root/'qualification.json'
    dag_path.write_text(json.dumps(dag))
    receipt_path.write_text(json.dumps(receipt))
    digest = hashlib.sha256(dag_path.read_bytes()).hexdigest() if approved else 'c'*64
    result = subprocess.run([sys.executable, str(ROOT/'scripts/ci/reliability_qualification.py'),
        '--dag', str(dag_path), '--approved-dag-sha256', digest, '--receipt', str(receipt_path),
        '--plan-sha256', PLAN, '--candidate-sha256', CANDIDATE, '--artifact-root', str(root)],
        cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == (0 if approved else 1), result.stdout + result.stderr
    output = json.loads(result.stdout)
    assert output['effect_authorization'] is False
    assert output['execution_provenance_verified'] is False
    assert output['consistency'] == ('passed' if approved else 'failed')
    if not approved:
        assert 'APPROVED_DAG_IDENTITY_MISMATCH' in output['errors']
