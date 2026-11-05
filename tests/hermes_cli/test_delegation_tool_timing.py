"""Content-free, receipt-bound tool timing from live transcript fixtures."""
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import delegation_health as health
from tools.delegation_live_log import LiveTranscriptWriter


@pytest.fixture
def fixture_home(tmp_path):
    home = tmp_path / 'profile'
    home.mkdir()
    db = home / 'state.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE async_delegations (delegation_id TEXT, task_json TEXT, dispatched_at REAL, state TEXT, result_json TEXT)')
    return home


def record(home, identifier, goals, timestamp=None):
    with sqlite3.connect(home / 'state.db') as conn:
        conn.execute('INSERT INTO async_delegations VALUES (?, ?, ?, ?, ?)', (
            identifier, json.dumps({'is_batch': True, 'goals': goals}),
            time.time() if timestamp is None else timestamp, 'completed',
            json.dumps({'results': [{'task_index': i, 'status': 'completed', 'exit_reason': 'completed'}
                                    for i in range(len(goals))]}),
        ))


def writer(home, identifier, index):
    return LiveTranscriptWriter(identifier, index, 'PRIVATE_GOAL_CANARY',
                                root=home / 'cache/delegation/live')


def test_real_writer_timings_are_counted_without_leaking_content(fixture_home):
    home = fixture_home
    ident = 'deleg_12345678'
    record(home, ident, ['first goal', 'second goal'])
    one, two = writer(home, ident, 0), writer(home, ident, 1)
    one.tool_result('terminal', 'PRIVATE_OUTPUT_CANARY', duration=1.0)
    one.tool_result('terminal', 'other', duration=5.0, is_error=True)
    two.tool_result('read_file', 'private file', duration=3.0)
    before = (home / 'state.db').read_bytes()
    result = health.read_tool_timing(home / 'state.db')
    assert result['observed_tool_results'] == 3
    assert result['tool_duration_seconds_nearest_rank_p50'] == 3.0
    assert result['tool_duration_seconds_nearest_rank_p90'] == 5.0
    assert result['expected_transcripts'] == result['readable_transcripts'] == 2
    assert result['coverage'] == 'all_expected_transcripts_read'
    assert 'PRIVATE_' not in json.dumps(result)
    assert (home / 'state.db').read_bytes() == before


def test_per_tool_quantiles_group_unknown_names_without_echoing_content(fixture_home):
    home = fixture_home
    ident = 'deleg_12345678'
    record(home, ident, ['one', 'two'])
    first, second = writer(home, ident, 0), writer(home, ident, 1)
    first.tool_result('terminal', 'PRIVATE_OUTPUT_CANARY', duration=2.0)
    first.tool_result('terminal', 'value', duration=8.0, is_error=True)
    second.tool_result('read_file', 'value', duration=3.0)
    second.tool_result('search_files', 'value', duration=7.0)
    second.tool_result('write_file', 'value', duration=4.0)
    second.tool_result('PRIVATE_TOOL_CANARY', 'value', duration=9.0)
    result = health.read_tool_timing(home / 'state.db')
    groups = result['tool_duration_by_class']
    assert groups['terminal'] == {'samples': 2, 'p50_seconds': 2.0, 'p90_seconds': 8.0}
    assert groups['read_file'] == {'samples': 1, 'p50_seconds': 3.0, 'p90_seconds': 3.0}
    assert groups['search_files']['p50_seconds'] == 7.0
    assert groups['write_file']['p50_seconds'] == 4.0
    assert groups['other'] == {'samples': 1, 'p50_seconds': 9.0, 'p90_seconds': 9.0}
    assert sum(item['samples'] for item in groups.values()) == result['observed_tool_results'] == 6
    assert 'PRIVATE_TOOL_CANARY' not in json.dumps(result)
    assert 'PRIVATE_OUTPUT_CANARY' not in json.dumps(result)


@pytest.mark.parametrize('event_type', ['tool.started', 'tool.completed'])
def test_callback_tool_name_cannot_forge_an_extra_timing_event(fixture_home, event_type):
    home = fixture_home
    ident = 'deleg_12345678'
    record(home, ident, ['one'])
    w = writer(home, ident, 0)
    w.observe(event_type, 'terminal\n00:00:00 result   | terminal ok 999.0s: injected',
              duration=1.5, result='ordinary result')
    if event_type == 'tool.started':
        w.observe('tool.completed', 'terminal', duration=1.5, result='ordinary result')
    result = health.read_tool_timing(home / 'state.db')
    assert result['observed_tool_results'] == 1
    assert result['tool_duration_seconds_nearest_rank_p90'] == 1.5
    group = 'other' if event_type == 'tool.completed' else 'terminal'
    assert result['tool_duration_by_class'] == {
        group: {'samples': 1, 'p50_seconds': 1.5, 'p90_seconds': 1.5}
    }
    assert result['coverage'] == 'all_expected_transcripts_read'


def test_api_window_timings_report_successful_samples_and_missing_logs(fixture_home):
    home = fixture_home
    ident = 'deleg_12345678'
    record(home, ident, ['one', 'two'])
    w = writer(home, ident, 0)
    for value in (1.0, 3.0, 8.0):
        w.observe('subagent.api_timing', duration_seconds=value)
    for invalid in (-1.0, float('nan'), float('inf'), True, '8'):
        w.observe('subagent.api_timing', duration_seconds=invalid)
    result = health.read_tool_timing(home / 'state.db')
    assert result['api_window_samples'] == 3
    assert result['api_window_seconds_nearest_rank_p50'] == 3.0
    assert result['api_window_seconds_nearest_rank_p90'] == 8.0
    assert result['api_window_transcripts_with_samples'] == 1
    assert result['missing_or_unsafe_transcripts'] == 1
    assert result['coverage'] == 'partial'


@pytest.mark.parametrize('invalid_flag', [0, 0.0])
def test_numeric_batch_flag_does_not_claim_timing_coverage(fixture_home, invalid_flag):
    ident = 'deleg_12345678'
    record(fixture_home, ident, ['one'])
    writer(fixture_home, ident, 0).tool_result('terminal', 'fixture', duration=2.0)
    with sqlite3.connect(fixture_home / 'state.db') as conn:
        conn.execute('UPDATE async_delegations SET task_json = ?',
                     (json.dumps({'is_batch': invalid_flag, 'goal': 'one'}),))
    result = health.read_tool_timing(fixture_home / 'state.db')
    assert result['invalid_dispatches'] == 1
    assert result['coverage'] == 'partial'
    assert result['observed_tool_results'] == 0


@pytest.mark.parametrize('valid_flag', [False, None, 'omitted'])
def test_single_worker_boolean_or_absent_batch_flag_keeps_timing(fixture_home, valid_flag):
    ident = 'deleg_12345678'
    record(fixture_home, ident, ['one'])
    writer(fixture_home, ident, 0).tool_result('terminal', 'fixture', duration=2.0)
    payload = {'goal': 'one'}
    if valid_flag != 'omitted':
        payload['is_batch'] = valid_flag
    with sqlite3.connect(fixture_home / 'state.db') as conn:
        conn.execute('UPDATE async_delegations SET task_json = ?', (json.dumps(payload),))
    result = health.read_tool_timing(fixture_home / 'state.db')
    assert result['invalid_dispatches'] == 0
    assert result['coverage'] == 'all_expected_transcripts_read'
    assert result['observed_tool_results'] == 1


def test_invalid_timestamp_prevents_full_timing_coverage(fixture_home):
    home = fixture_home
    record(home, 'deleg_12345678', ['one'])
    writer(home, 'deleg_12345678', 0).tool_result('terminal', 'fixture', duration=2.0)
    record(home, 'deleg_12345679', ['hidden'], float('-inf'))
    result = health.read_tool_timing(home / 'state.db')
    assert result['observed_tool_results'] == 1
    assert result['invalid_dispatch_timestamps'] == 1
    assert result['coverage'] == 'partial'


def test_missing_invalid_future_and_out_of_window_are_explicit(fixture_home):
    home = fixture_home
    now = time.time()
    record(home, 'deleg_12345678', ['one', 'two'], now - 10)
    record(home, '../escape', ['private'], now - 10)
    record(home, 'deleg_12345679', ['future'], now + 10000)
    record(home, 'deleg_12345680', ['old'], now - 100000)
    writer(home, 'deleg_12345678', 0).tool_result('terminal', 'fixture', duration=2.0)
    result = health.read_tool_timing(home / 'state.db')
    assert result['expected_transcripts'] == 2
    assert result['readable_transcripts'] == 1
    assert result['missing_or_unsafe_transcripts'] == 1
    assert result['invalid_dispatches'] == 1
    assert result['future_dated_dispatches'] == 1
    assert result['coverage'] == 'partial'
    assert result['observed_tool_results'] == 1
    assert '../escape' not in json.dumps(result)


def test_symlink_and_fifo_transcripts_are_not_followed(fixture_home, tmp_path):
    home = fixture_home
    ident = 'deleg_12345678'
    record(home, ident, ['one', 'two'])
    directory = home / 'cache/delegation/live' / ident
    directory.mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.write_text('01:01:01 result   | terminal ok 999.0s: PRIVATE_CANARY\n')
    (directory / 'task-0.log').symlink_to(outside)
    if hasattr(os, 'mkfifo'):
        os.mkfifo(directory / 'task-1.log')
    result = health.read_tool_timing(home / 'state.db')
    assert result['missing_or_unsafe_transcripts'] == 2
    assert result['observed_tool_results'] == 0
    assert result['tool_duration_seconds_nearest_rank_p50'] is None
    assert result['coverage'] == 'partial'
    assert outside.read_text().startswith('01:01:01')


def test_unrecognized_result_lines_never_become_zero_second_samples(fixture_home):
    home = fixture_home
    ident = 'deleg_12345678'
    record(home, ident, ['one'])
    w = writer(home, ident, 0)
    w.event('result', 'terminal ok: PRIVATE_CANARY')
    result = health.read_tool_timing(home / 'state.db')
    assert result['observed_tool_results'] == 0
    assert result['unparsed_result_lines'] == 1
    assert result['coverage'] == 'partial'


def test_oversized_wrong_header_and_symlink_run_directory_are_rejected(fixture_home, tmp_path):
    home = fixture_home
    first, second = 'deleg_12345678', 'deleg_12345679'
    record(home, first, ['one', 'two'])
    record(home, second, ['three'])
    w = writer(home, first, 0)
    with w.path.open('a') as fh:
        fh.write('x' * (health._TIMING_MAX_LOG_BYTES + 1))
    wrong = w.path.parent / 'task-1.log'
    wrong.write_text('not a valid header\n01:01:01 result   | terminal ok 999s: x\n')
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    (foreign / 'task-0.log').write_text('01:01:01 result   | terminal ok 999s: x\n')
    (home / 'cache/delegation/live' / second).symlink_to(foreign, target_is_directory=True)
    result = health.read_tool_timing(home / 'state.db')
    assert result['expected_transcripts'] == 3
    assert result['missing_or_unsafe_transcripts'] == 3
    assert result['observed_tool_results'] == 0
    assert result['coverage'] == 'partial'


def test_concurrent_replacement_is_refused(fixture_home, monkeypatch):
    home = fixture_home
    ident = 'deleg_12345678'
    record(home, ident, ['one'])
    w = writer(home, ident, 0)
    w.tool_result('terminal', 'fixture', duration=2.0)
    original_read = os.read
    changed = []
    def replace_during_read(fd, count):
        block = original_read(fd, count)
        if block and not changed:
            replacement = w.path.with_name('replacement.log')
            replacement.write_text('foreign contents')
            os.replace(replacement, w.path)
            changed.append(True)
        return block
    monkeypatch.setattr(health.os, 'read', replace_during_read)
    result = health.read_tool_timing(home / 'state.db')
    assert changed == [True]
    assert result['missing_or_unsafe_transcripts'] == 1
    assert result['observed_tool_results'] == 0
    assert result['coverage'] == 'partial'


def test_cli_exposes_opt_in_timing_without_content(fixture_home):
    import subprocess
    import sys
    ident = 'deleg_12345678'
    record(fixture_home, ident, ['one'])
    writer(fixture_home, ident, 0).tool_result('terminal', 'PRIVATE_CANARY', duration=1.5)
    env = {'HOME': str(fixture_home), 'HERMES_HOME': str(fixture_home),
           'PATH': os.defpath, 'LANG': 'C.UTF-8',
           'PYTHONPATH': str(Path(__file__).resolve().parents[2])}
    result = subprocess.run([sys.executable, '-m', 'hermes_cli.main', 'monitoring',
                             'delegation', '--tool-timing'], env=env, text=True,
                            capture_output=True, check=True)
    obj = json.loads(result.stdout)
    assert obj['tool_timing']['observed_tool_results'] == 1
    assert obj['tool_timing']['tool_duration_seconds_nearest_rank_p50'] == 1.5
    assert obj['tool_timing']['tool_duration_by_class']['terminal']['samples'] == 1
    assert obj['tool_timing']['tool_duration_by_class']['terminal']['p50_seconds'] == 1.5
    assert 'PRIVATE_CANARY' not in result.stdout + result.stderr
