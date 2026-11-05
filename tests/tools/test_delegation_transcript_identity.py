"""Partial transcript failure must never reassign one child's identity to another."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.tools.test_delegate_storage_backpressure import sd03b_delegate_state
from tools import delegation_live_log as live_log


@pytest.mark.parametrize('missing', [0, 1, 2])
def test_manifest_preserves_task_identity_when_one_writer_cannot_create(tmp_path, monkeypatch, missing):
    root = tmp_path / 'live'
    target = root / 'deleg_12345678'
    target.mkdir(parents=True)
    existing = target / f'task-{missing}.log'
    existing.write_text('existing synthetic canary')
    monkeypatch.setattr(live_log, 'live_transcript_root', lambda: root)
    tasks = [{'goal': f'Inspect synthetic workload item {i}'} for i in range(3)]
    deleg_id, writers, paths = live_log.create_live_transcripts(tasks, delegation_id='deleg_12345678')
    assert deleg_id == 'deleg_12345678'
    assert writers[missing] is None
    assert len(paths) == 2
    manifest = json.loads((target / 'manifest.json').read_text())
    for i, task in enumerate(manifest['tasks']):
        assert task['index'] == i
        assert task['log'] == (None if i == missing else str(target / f'task-{i}.log'))
    assert existing.read_text() == 'existing synthetic canary'


def test_real_writer_partial_failure_propagates_identity_to_results_and_manifest(
        sd03b_delegate_state, monkeypatch, tmp_path):
    state = sd03b_delegate_state
    root = tmp_path / 'live'
    target = root / 'deleg_12345678'
    target.mkdir(parents=True)
    existing = target / 'task-0.log'
    existing.write_text('existing synthetic canary')
    monkeypatch.setattr(live_log, 'live_transcript_root', lambda: root)
    state.live.create_live_transcripts.side_effect = lambda tasks, context, **kwargs: (
        live_log.create_live_transcripts(tasks, context, delegation_id='deleg_12345678', **kwargs)
    )
    state.live.update_manifest_statuses.side_effect = live_log.update_manifest_statuses
    monkeypatch.setattr(state.caller, '_get_max_concurrent_children', lambda: 3)
    state.build.side_effect = [SimpleNamespace(_subagent_id=f'real-writer-child-{i}',
                                _delegate_role='leaf', session_id=f'real-writer-session-{i}',
                                tool_progress_callback=None, close=Mock()) for i in range(3)]
    state.run.side_effect = lambda task_index, **kw: {
        'task_index': task_index, 'status': 'completed', 'summary': 'synthetic result',
        'api_calls': 1, 'duration_seconds': 0,
    }
    tasks = [{'goal': f'Inspect synthetic workload item number {i}'} for i in range(3)]
    result = json.loads(state.caller.delegate_task(tasks=tasks, parent_agent=state.parent))
    manifest = json.loads((target / 'manifest.json').read_text())
    for i, entry in enumerate(result['results']):
        expected = None if i == 0 else str(target / f'task-{i}.log')
        assert entry.get('live_transcript') == expected
        assert manifest['tasks'][i]['log'] == expected
        assert manifest['tasks'][i]['status'] == 'completed'
    assert existing.read_text() == 'existing synthetic canary'


@pytest.mark.parametrize('mode', ['synchronous', 'refused', 'background_runner'])
@pytest.mark.parametrize('missing', [0, 1, 2])
def test_partial_writer_failure_keeps_result_paths_bound_to_indices(sd03b_delegate_state, monkeypatch, mode, missing):
    state = sd03b_delegate_state
    writers = [None if i == missing else SimpleNamespace(path=f'/inert/task-{i}.log', finalize=Mock())
               for i in range(3)]
    paths = [w.path for w in writers if w is not None]
    state.live.create_live_transcripts.return_value = ('deleg-fixture', writers, paths)
    monkeypatch.setattr(state.caller, '_get_max_concurrent_children', lambda: 3)
    children = [SimpleNamespace(_subagent_id=f'identity-child-{i}', _delegate_role='leaf',
                                session_id=f'identity-session-{i}', tool_progress_callback=None,
                                close=Mock()) for i in range(3)]
    state.build.side_effect = children
    state.run.side_effect = lambda task_index, **kw: {
        'task_index': task_index, 'status': 'completed', 'summary': 'synthetic result',
        'api_calls': 1, 'duration_seconds': 0,
    }
    if mode == 'refused':
        state.dispatch.return_value = {'status': 'rejected', 'error_code': 'durable_backlog_full',
                                       'execution_started': False, 'error': 'synthetic refusal'}
    elif mode == 'background_runner':
        state.dispatch.return_value = {'status': 'dispatched', 'delegation_id': 'deleg-fixture'}
    tasks = [{'goal': f'Inspect synthetic workload item number {i}'} for i in range(3)]
    result = json.loads(state.caller.delegate_task(tasks=tasks, background=(mode != 'synchronous'),
                                                   parent_agent=state.parent))
    if mode == 'background_runner':
        assert result['status'] == 'dispatched'
        assert result['live_transcripts'] == paths
        assert 'may be missing' in result['live_transcripts_hint']
        result = state.dispatch.call_args.kwargs['runner']()
    assert len(result['results']) == 3, result
    for i, entry in enumerate(result['results']):
        assert entry['task_index'] == i
        assert entry.get('live_transcript') == (None if i == missing else f'/inert/task-{i}.log')
    assert result.get('status') == ('rejected' if mode == 'refused' else None)
