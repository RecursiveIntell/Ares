"""CLI goal consume/render/Stop fences against the real temp SQLite owner."""
import pytest
from cli import HermesCLI
from hermes_cli import goals


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    goals._DB_CACHE.clear()
    db = goals._get_session_db()
    db.create_session('cli-runtime', source='cli')
    mgr = goals.GoalManager('cli-runtime')
    mgr.set('authorized task')
    mgr.checkpoint_recovery('RETRYABLE_FAILURE')
    value = HermesCLI.__new__(HermesCLI)
    value.session_id = 'cli-runtime'
    value.agent = None
    binding = {'goal_id': mgr.state.goal_id, 'checkpoint_revision': mgr.state.checkpoint_revision,
               'continuation_token': mgr.state.continuation_token}
    yield value, binding
    goals._DB_CACHE.clear()


def test_cli_bound_admission_uses_consumed_prompt_once(runtime):
    cli, binding = runtime
    prompt = cli._consume_goal_runtime_turn(binding)
    assert 'authorized task' in prompt
    assert cli._goal_runtime_turn_current(binding)
    assert cli._consume_goal_runtime_turn(binding) is None


@pytest.mark.parametrize('cut', ['pause', 'clear', 'stop', 'rehome'])
def test_cli_postconsume_cut_prevents_actor_dispatch(runtime, monkeypatch, cut):
    cli, binding = runtime
    original = goals.GoalManager.next_continuation_prompt
    def render(self):
        if cut in {'pause', 'clear'}: getattr(goals.GoalManager('cli-runtime'), cut)()
        if cut == 'stop': cli._cancel_goal_runtime_turn()
        if cut == 'rehome': cli.session_id = 'new-owner'
        return original(self)
    monkeypatch.setattr(goals.GoalManager, 'next_continuation_prompt', render)
    assert cli._consume_goal_runtime_turn(binding) is None


@pytest.mark.parametrize('cut', ['none', 'pause', 'clear', 'stop'])
def test_actual_cli_chat_fences_goal_after_agent_initialization(runtime, monkeypatch, cut):
    from tests.cli.test_cli_interrupt_ack_race import _make_cli, _StubAgent
    _, binding = runtime
    cli = _make_cli()
    cli.session_id = 'cli-runtime'
    calls = []
    class ControlledAgent(_StubAgent):
        def run_conversation(self, **kwargs):
            calls.append(kwargs['user_message'])
            return {'final_response': 'controlled reply', 'messages': [], 'api_calls': 0,
                    'completed': True, 'partial': True, 'response_previewed': True}
    cli.agent = ControlledAgent(cli.session_id, turn_seconds=0)
    prompt = cli._consume_goal_runtime_turn(binding)
    assert prompt
    monkeypatch.setattr(cli, '_ensure_runtime_credentials', lambda: True)
    monkeypatch.setattr(cli, '_resolve_turn_agent_config', lambda *a: {
        'signature': cli._active_agent_route_signature, 'model': None, 'runtime': None,
        'request_overrides': None})
    def initialize(**kwargs):
        if cut in {'pause', 'clear'}: getattr(goals.GoalManager('cli-runtime'), cut)()
        if cut == 'stop': cli._cancel_goal_runtime_turn()
        return True
    monkeypatch.setattr(cli, '_init_agent', initialize)
    cli.chat(prompt, persist_user_display_kind='internal_notification',
             persist_user_display_metadata={'synthetic_source': 'goal_continuation', 'goal_continuation': binding})
    assert calls == ([prompt] if cut == 'none' else [])
