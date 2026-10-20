"""Actual per-attempt exports must not imply blanket case qualification."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[1]


def invoke(*arguments, timeout=60):
    return subprocess.run([sys.executable, str(ROOT / 'scripts/run_tests_parallel.py'),
        '-j', '2', '--file-retries', '0', '--file-timeout', '20', *arguments],
        cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def receipts(directory):
    return [json.loads(p.read_text()) for p in sorted(directory.glob('*/attempt-*/receipt.json'))]


def assert_bound_artifacts(directory, records):
    for record in records:
        for field in ['stdout', 'junit']:
            artifact = record[field]
            if artifact is None:
                continue
            path = directory / artifact['path']
            assert path.is_relative_to(directory)
            assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact['sha256']
        assert record['effect_authorization'] is False


def test_parallel_export_preserves_parameter_nodes_skips_and_duplicate_basenames(tmp_path):
    files = []
    for name in ['left', 'right']:
        folder = tmp_path / name
        folder.mkdir()
        path = folder / 'test_same.py'
        path.write_text("import pytest\n"
                        "@pytest.mark.parametrize('value', [1,2], ids=['one','two'])\n"
                        "def test_parameter(value):\n    assert value > 0\n"
                        "@pytest.mark.skip(reason='deliberate fixture skip')\n"
                        "def test_skip():\n    pass\n")
        files.append(str(path))
    output = tmp_path / 'evidence'
    result = invoke('--evidence-dir', str(output), *files, '-q')
    assert result.returncode == 0, result.stdout + result.stderr
    records = receipts(output)
    assert len(records) == 2
    assert {r['file'] for r in records} == set(files)
    for record in records:
        assert record['pytest_return_code'] == record['runner_return_code'] == 0
        assert record['timed_out'] is False
        assert record['test_sha256_before'] == record['test_sha256_after']
        junit = ET.parse(output / record['junit']['path'])
        assert {node.attrib['name'] for node in junit.iter('testcase')} == {
            'test_parameter[one]', 'test_parameter[two]', 'test_skip'}
        assert any(node.find('skipped') is not None for node in junit.iter('testcase'))
    assert_bound_artifacts(output, records)
    assert json.loads((output / 'summary.json').read_text())['runner_return_code'] == 0


@pytest.mark.parametrize('destination_type', ['directory', 'file', 'symlink'])
def test_existing_evidence_destination_refuses_before_worker(tmp_path, destination_type):
    marker = tmp_path / 'worker-ran'
    path = tmp_path / 'test_neutral.py'
    path.write_text(f"from pathlib import Path\ndef test_neutral():\n    Path({str(marker)!r}).touch()\n")
    output = tmp_path / 'evidence'
    if destination_type == 'directory':
        output.mkdir()
    elif destination_type == 'file':
        output.write_text('preserve')
    else:
        target = tmp_path / 'target'
        target.mkdir()
        output.symlink_to(target, target_is_directory=True)
    result = invoke('--evidence-dir', str(output), str(path))
    assert result.returncode == 2, result.stdout + result.stderr
    assert 'TEST_EVIDENCE_DIRECTORY_NOT_FRESH' in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize('flag', ['--junitxml', '--junit-xml'])
def test_caller_junit_destination_refused_before_worker(tmp_path, flag):
    marker = tmp_path / 'worker-ran'
    path = tmp_path / 'test_neutral.py'
    path.write_text(f"from pathlib import Path\ndef test_neutral():\n    Path({str(marker)!r}).touch()\n")
    output = tmp_path / 'evidence'
    result = invoke('--evidence-dir', str(output), str(path), '--', flag + '=' + str(tmp_path/'foreign.xml'))
    assert result.returncode == 2, result.stdout + result.stderr
    assert 'TEST_EVIDENCE_JUNIT_CONFLICT' in result.stderr
    assert not marker.exists()
    assert not output.exists()


def test_failed_retry_retains_both_xml_and_raw_codes(tmp_path):
    path = tmp_path / 'test_flaky.py'
    marker = tmp_path / 'retry-count'
    path.write_text(f"from pathlib import Path\ndef test_flaky():\n"
                    f"    marker = Path({str(marker)!r})\n"
                    "    count = int(marker.read_text()) if marker.exists() else 0\n"
                    "    marker.write_text(str(count+1))\n"
                    "    assert count == 1\n")
    output = tmp_path / 'evidence'
    result = invoke('--file-retries', '1', '--evidence-dir', str(output), str(path), '-q')
    assert result.returncode == 0, result.stdout + result.stderr
    records = receipts(output)
    assert [r['pytest_return_code'] for r in records] == [1, 0]
    assert [r['attempt'] for r in records] == [1, 2]
    assert ET.parse(output / records[0]['junit']['path']).find('.//failure') is not None
    assert ET.parse(output / records[1]['junit']['path']).find('.//failure') is None
    assert_bound_artifacts(output, records)


def test_timeout_is_not_a_passing_case_even_without_junit(tmp_path):
    path = tmp_path / 'test_slow.py'
    path.write_text("import time\ndef test_slow():\n    time.sleep(60)\n")
    output = tmp_path / 'evidence'
    result = invoke('--file-timeout', '0.1', '--evidence-dir', str(output), str(path))
    assert result.returncode == 1, result.stdout + result.stderr
    record, = receipts(output)
    assert record['timed_out'] is True
    assert record['pytest_return_code'] != 0
    assert record['runner_return_code'] == 124
    assert record['junit'] is None
    assert not (output/'summary.json').exists(), 'Timed-out attempts cannot finalize evidence'


def test_teardown_error_retained_beside_successful_test(tmp_path):
    path = tmp_path / 'test_teardown.py'
    path.write_text("import pytest\n@pytest.fixture\ndef broken():\n"
                    "    yield\n    raise RuntimeError('neutral teardown witness')\n"
                    "def test_neutral(broken):\n    assert True\n")
    output = tmp_path / 'evidence'
    result = invoke('--evidence-dir', str(output), str(path), '-q')
    assert result.returncode == 1, result.stdout + result.stderr
    record, = receipts(output)
    assert record['pytest_return_code'] == 1
    assert ET.parse(output / record['junit']['path']).find('.//error') is not None
    assert record['parsed_summary']['passed'] == 1
    assert record['parsed_summary']['errors'] == 1


def test_zero_collection_raw_code_not_normalized_in_export(tmp_path):
    empty = tmp_path / 'test_empty.py'
    empty.write_text('# intentionally no tests\n')
    valid = tmp_path / 'test_valid.py'
    valid.write_text('def test_neutral():\n    assert True\n')
    output = tmp_path / 'evidence'
    result = invoke('--evidence-dir', str(output), str(empty), str(valid))
    assert result.returncode == 0, result.stdout + result.stderr
    record = next(r for r in receipts(output) if r['file'] == str(empty))
    assert record['pytest_return_code'] == 5
    assert record['runner_return_code'] == 0
    assert not list(ET.parse(output / record['junit']['path']).iter('testcase'))


def test_duplicate_requested_file_refuses_before_evidence_workers(tmp_path):
    marker = tmp_path / 'worker-ran'
    path = tmp_path / 'test_neutral.py'
    path.write_text(f"from pathlib import Path\ndef test_neutral():\n    Path({str(marker)!r}).touch()\n")
    output = tmp_path / 'evidence'
    result = invoke('--evidence-dir', str(output), '--files', f'{path}:{path}')
    assert result.returncode == 2, result.stdout + result.stderr
    assert 'TEST_EVIDENCE_DUPLICATE_FILE' in result.stderr
    assert not marker.exists()
    assert not output.exists()


def test_changed_test_source_retains_evidence_but_cannot_finalize(tmp_path):
    path = tmp_path / 'test_change.py'
    path.write_text("from pathlib import Path\ndef test_change():\n"
                    "    path = Path(__file__)\n    path.write_text(path.read_text()+'# changed\\n')\n")
    output = tmp_path / 'evidence'
    result = invoke('--evidence-dir', str(output), str(path))
    assert result.returncode == 1, result.stdout + result.stderr
    record, = receipts(output)
    assert record['pytest_return_code'] == 0
    assert record['test_sha256_before'] != record['test_sha256_after']
    assert record['attempt_completed'] is False
    assert not (output/'summary.json').exists()


def test_evidence_write_failure_is_not_hidden_by_test_pass(tmp_path, monkeypatch):
    from scripts import run_tests_parallel as runner

    path = tmp_path / 'test_neutral.py'
    path.write_text('def test_neutral():\n    assert True\n')
    output = tmp_path / 'evidence'
    write = runner._write_evidence_json
    def fail_receipt(path, value):
        if path.name == 'receipt.json':
            raise OSError('neutral evidence write failure')
        return write(path, value)
    monkeypatch.setattr(runner, '_write_evidence_json', fail_receipt)
    monkeypatch.setattr(sys, 'argv', ['run_tests_parallel.py', '-j1', '--file-retries', '0',
                        '--evidence-dir', str(output), str(path)])
    assert runner.main() == 1
    assert list(output.glob('*/attempt-*/stdout.log'))
    assert not (output/'summary.json').exists()


def test_concurrent_evidence_claim_admits_only_one_runner(tmp_path):
    path = tmp_path / 'test_neutral.py'
    path.write_text('def test_neutral():\n    assert True\n')
    output = tmp_path / 'evidence'
    cmd = [sys.executable, str(ROOT/'scripts/run_tests_parallel.py'), '-j1',
           '--file-retries', '0', '--evidence-dir', str(output), str(path)]
    processes = [subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for _ in range(2)]
    results = [p.communicate(timeout=60) for p in processes]
    assert sorted(p.returncode for p in processes) == [0, 2], results
    assert len(receipts(output)) == 1
    assert any('TEST_EVIDENCE_DIRECTORY_NOT_FRESH' in err for _, err in results)


def test_pytest_addopts_junit_conflict_refuses_before_worker(tmp_path, monkeypatch):
    marker = tmp_path/'worker-ran'
    path = tmp_path/'test_neutral.py'
    path.write_text(f"from pathlib import Path\ndef test_neutral():\n    Path({str(marker)!r}).touch()\n")
    output = tmp_path/'evidence'
    monkeypatch.setenv('PYTEST_ADDOPTS', '--junitxml=' + str(tmp_path/'foreign.xml'))
    result = invoke('--evidence-dir', str(output), str(path))
    assert result.returncode == 2, result.stdout + result.stderr
    assert 'TEST_EVIDENCE_JUNIT_CONFLICT' in result.stderr
    assert not marker.exists()
    assert not output.exists()


@pytest.mark.parametrize('xml', [None, '<malformed'])
def test_absent_or_malformed_junit_does_not_finalize(tmp_path, monkeypatch, xml):
    from scripts import run_tests_parallel as runner
    from types import SimpleNamespace

    path = tmp_path/'test_neutral.py'
    path.write_text('def test_neutral():\n    assert True\n')
    output = tmp_path/'evidence'
    output.mkdir()
    def popen(cmd, **kwargs):
        junit = Path(next(value.split('=', 1)[1] for value in cmd if value.startswith('--junitxml=')))
        if xml is not None:
            junit.write_text(xml)
        return SimpleNamespace(pid=os.getpid(), returncode=0,
                               communicate=lambda **kw: ('1 passed in 0.1s\n', None))
    monkeypatch.setattr(runner.subprocess, 'Popen', popen)
    monkeypatch.setattr(runner, '_kill_tree', lambda *a, **kw: None)
    # Unit-test malformed output handling, not mocked process containment.
    runner._run_one_file(path, [], ROOT, 20, evidence_dir=output)
    record, = receipts(output)
    assert record['junit_error'] == ('FileNotFoundError' if xml is None else 'ParseError')
    assert runner._finish_evidence(output, [path], 0) is False
    assert not (output/'summary.json').exists()


def test_pytest_interruption_retains_raw_outcome_without_summary(tmp_path):
    path = tmp_path/'test_interrupt.py'
    path.write_text('def test_interrupt():\n    raise KeyboardInterrupt\n')
    output = tmp_path/'evidence'
    result = invoke('--evidence-dir', str(output), str(path))
    assert result.returncode == 1, result.stdout + result.stderr
    record, = receipts(output)
    assert record['pytest_return_code'] == 2
    assert record['attempt_completed'] is False
    assert not (output/'summary.json').exists()


def test_manifest_publication_failure_preserves_staging_not_final(tmp_path, monkeypatch):
    from scripts import run_tests_parallel as runner

    def failed_link(*args, **kwargs):
        raise OSError('neutral link failure')
    monkeypatch.setattr(runner.os, 'link', failed_link)
    path = tmp_path/'summary.json'
    with pytest.raises(OSError, match='neutral link failure'):
        runner._write_evidence_json(path, {'scope': 'neutral'})
    assert not path.exists()
    staged, = tmp_path.glob('summary.json.pending-*')
    assert json.loads(staged.read_text()) == {'scope': 'neutral'}


def test_manifest_publication_cannot_replace_previous_bytes(tmp_path):
    from scripts import run_tests_parallel as runner

    path = tmp_path/'summary.json'
    path.write_bytes(b'original observation')
    with pytest.raises(FileExistsError):
        runner._write_evidence_json(path, {'scope': 'neutral'})
    assert path.read_bytes() == b'original observation'
