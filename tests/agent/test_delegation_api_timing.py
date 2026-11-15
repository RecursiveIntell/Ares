"""Delegation timing is emitted by the actual conversation loop, not guessed."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import json
import sqlite3

import pytest

from run_agent import AIAgent
from tools.delegation_live_log import LiveTranscriptWriter


def _response():
    message = SimpleNamespace(content='fixture complete', tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason='stop')],
                           model='fixture-model', usage=None)


@pytest.mark.parametrize('depth,expected', [(0, 0), (1, 1)])
def test_only_delegated_agent_emits_successful_api_window(tmp_path, depth, expected):
    writer = LiveTranscriptWriter('deleg_deadbeef', 0, 'fixture',
                                  root=tmp_path / 'cache/delegation/live')
    db_path = tmp_path / 'state.db'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE async_delegations (delegation_id TEXT, task_json TEXT, dispatched_at REAL)')
        import time
        db.execute('INSERT INTO async_delegations VALUES (?, ?, ?)',
                   ('deleg_deadbeef', json.dumps({'goal': 'fixture'}), time.time()))
    events = []

    def progress(event, *args, **kwargs):
        events.append((event, kwargs))
        writer.observe(event, *args, **kwargs)

    with (patch('run_agent.get_tool_definitions', return_value=[]),
          patch('run_agent.check_toolset_requirements', return_value={}),
          patch('run_agent.OpenAI')):
        agent = AIAgent(api_key='test-key', base_url='https://example.invalid/v1',
                        quiet_mode=True, skip_context_files=True, skip_memory=True,
                        tool_progress_callback=progress)
    agent._delegate_depth = depth
    agent._cached_system_prompt = 'test system'
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.client = MagicMock()
    agent.client.chat.completions.create.return_value = _response()
    try:
        with (patch.object(agent, '_persist_session'),
              patch.object(agent, '_save_trajectory'),
              patch.object(agent, '_cleanup_task_resources')):
            result = agent.run_conversation('fixture request')
    finally:
        agent.close()
    assert result['final_response'] == 'fixture complete'
    timings = [kwargs['duration_seconds'] for event, kwargs in events
               if event == 'subagent.api_timing']
    assert len(timings) == expected
    from hermes_cli.delegation_health import read_tool_timing
    report = read_tool_timing(db_path)
    assert report['api_window_samples'] == expected
    assert report['api_window_transcripts_with_samples'] == expected
    if expected:
        assert isinstance(timings[0], float) and timings[0] >= 0
        assert 'api-window' in writer.path.read_text(encoding='utf-8')
        assert report['api_window_seconds_nearest_rank_p50'] == round(timings[0], 3)
    else:
        assert report['api_window_seconds_nearest_rank_p50'] is None
