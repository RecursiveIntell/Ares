"""Requested-path admission must fail before any test worker starts."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def requested(tmp_path):
    marker = tmp_path / 'worker-ran'
    valid = tmp_path / 'test_valid.py'
    valid.write_text(f"from pathlib import Path\ndef test_neutral():\n    Path({str(marker)!r}).write_text('ran')\n")
    return valid, tmp_path / 'test_absent.py', marker


def invoke(*arguments):
    root = Path(__file__).resolve().parents[1]
    return subprocess.run([sys.executable, str(root / 'scripts/run_tests_parallel.py'),
        '-j', '1', '--file-retries', '0', '--file-timeout', '30', *arguments],
        cwd=root, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize('mode', ['positional', 'paths', 'files', 'generate-slices'])
def test_mixed_requested_paths_refuse_before_worker_admission(requested, mode):
    valid, missing, marker = requested
    if mode == 'paths':
        arguments = ['--paths', os.pathsep.join((str(valid), str(missing)))]
    elif mode == 'files':
        arguments = ['--files', os.pathsep.join((str(valid), str(missing)))]
    else:
        arguments = [str(valid), str(missing)]
        if mode == 'generate-slices':
            arguments += ['--generate-slices', '2']
    result = invoke(*arguments)
    assert result.returncode == 2, result.stdout + result.stderr
    assert 'TEST_REQUESTED_PATH_MISSING' in result.stderr
    assert str(missing) in result.stderr
    assert not marker.exists(), 'A partial requested suite was executed'


def test_files_selector_refuses_directory_before_collection(tmp_path):
    result = invoke('--files', str(tmp_path))
    assert result.returncode == 2, result.stdout + result.stderr
    assert 'TEST_REQUESTED_FILE_INVALID' in result.stderr


def test_exact_existing_file_still_runs(requested):
    valid, _, marker = requested
    result = invoke(str(valid))
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text() == 'ran'
    assert '1 tests passed' in result.stdout


def test_empty_explicit_file_list_never_discovers_the_default_suite(monkeypatch):
    from scripts import run_tests_parallel as runner

    monkeypatch.setattr(sys, 'argv', ['run_tests_parallel.py', '--files', ''])
    def forbidden_discovery(_roots):
        raise AssertionError('Empty explicit CI shard fell back to the whole suite')
    monkeypatch.setattr(runner, '_discover_files', forbidden_discovery)
    assert runner.main() == 1
