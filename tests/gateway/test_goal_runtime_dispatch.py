"""Real gateway runtime with controlled constructor/conversation, temp SQLite.

Construction pauses/clears the goal in the consume-to-dispatch interval. The
native runtime owns the final fence; no live provider calls are made.
"""
import asyncio
import threading

import pytest
import gateway.run as runtime
import run_agent
from hermes_cli import goals
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource, build_session_key
from gateway.config import Platform
from tests.gateway.test_run_progress_interrupt import _make_runner, ProgressCaptureAdapter, PreInterruptAgent


@pytest.mark.asyncio
@pytest.mark.parametrize('cut', ['none', 'pause', 'clear', 'stop'])
async def test_actual_runtime_checks_goal_after_construction(tmp_path, monkeypatch, cut):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    goals._DB_CACHE.clear()
    db = goals._get_session_db()
    sid = 'gateway-native-' + cut
    db.create_session(sid, source='telegram')
    manager = goals.GoalManager(sid)
    manager.set('authorized task')
    manager.checkpoint_recovery('RETRYABLE_FAILURE')
    binding = {'goal_id': manager.state.goal_id, 'checkpoint_revision': manager.state.checkpoint_revision,
               'continuation_token': manager.state.continuation_token}
    adapter = ProgressCaptureAdapter()
    runner = _make_runner(adapter)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='isolated', chat_type='private')
    event = MessageEvent(text='cached prompt', source=source, internal=True,
                         metadata={'goal_continuation': binding})
    from types import SimpleNamespace
    assert await runner._consume_goal_continuation_at_turn_start(event, SimpleNamespace(session_id=sid))
    calls = []
    class ControlledAgent(PreInterruptAgent):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.session_id = sid
            if cut in {'pause', 'clear'}: getattr(goals.GoalManager(sid), cut)()
            if cut == 'stop': self._interrupt_requested = True
        def run_conversation(self, message, **kwargs):
            calls.append(message)
            return {'final_response': 'controlled reply', 'messages': [], 'api_calls': 0, 'completed': True}
    monkeypatch.setattr(run_agent, 'AIAgent', ControlledAgent)
    monkeypatch.setattr(runtime, '_hermes_home', tmp_path)
    monkeypatch.setattr(runtime, '_load_gateway_config', lambda: {
        'model': {'default': 'controlled-model', 'context_length': 128000},
        'display': {'tool_progress': 'off', 'thinking': False},
    })
    monkeypatch.setattr(runner, '_resolve_session_agent_runtime', lambda **k: ('controlled-model',
        {'api_key': 'controlled-fixture', 'provider': 'custom', 'base_url': 'http://controlled.invalid/v1'}))
    try:
        result = await asyncio.wait_for(runner._run_agent(
            message=event.text, context_prompt='', history=[], source=source,
            session_id=sid, session_key=build_session_key(source), goal_continuation=binding), timeout=10)
        if cut == 'none':
            assert calls == [event.text]
            assert result['final_response'] == 'controlled reply'
        else:
            assert calls == []
            assert result.get('interrupted') is True
    finally:
        runner._shutdown_executor()
        await adapter.cancel_background_tasks()
        goals._DB_CACHE.clear()
