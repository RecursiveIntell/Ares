"""Exercise native runtime outcomes and qualification barriers offline."""
from types import SimpleNamespace

import pytest

from agent import codex_runtime
from agent.transports import codex_app_server_session as sessions
from ares_runtime.continuity.runtime import ContextDispatchError


@pytest.fixture
def runtime(monkeypatch):
    memory, review, construction = [], [], []
    turn = SimpleNamespace(final_text="draft", partial_text="draft", completed=False,
                           projected_messages=[], tool_iterations=0, interrupted=False,
                           error=None, should_retire=False, thread_id="thread", turn_id="turn")
    class DummySession:
        def __init__(self, **kwargs):
            construction.append(kwargs)
        def run_turn(self, **kwargs):
            return turn
        def close(self):
            pass
        def matches_route(self, **kwargs):
            return True
    monkeypatch.setattr(sessions, "CodexAppServerSession", DummySession)
    monkeypatch.setattr(codex_runtime, "_record_codex_app_server_usage", lambda *args: {})
    monkeypatch.setattr(codex_runtime, "_record_codex_app_server_compaction", lambda *args: None)
    agent = SimpleNamespace(model="openai-codex/gpt-test", provider="openai-codex",
                            api_mode="codex_app_server", session_cwd="/tmp", context_rebase_enabled=False,
                            _skill_nudge_interval=1, _iters_since_skill=0, valid_tool_names={"skill_manage"},
                            _sync_external_memory_for_turn=lambda **kw: memory.append(kw),
                            _spawn_background_review=lambda **kw: review.append(kw))
    def run():
        return codex_runtime.run_codex_app_server_turn(
            agent, user_message="dummy", original_user_message="dummy", messages=[],
            effective_task_id="offline", should_review_memory=True)
    return SimpleNamespace(agent=agent, turn=turn, run=run, memory=memory, review=review,
                           construction=construction)


def test_runtime_never_infers_success_from_partial_text(runtime):
    result = runtime.run()
    assert result["completed"] is False
    assert result["partial"] is True
    assert result["final_response"] == ""
    assert result["partial_response"] == "draft"
    assert runtime.memory == []
    assert runtime.review == []


def test_runtime_passes_selected_model_and_provider(runtime):
    runtime.run()
    assert runtime.construction[0]["model"] == "openai-codex/gpt-test"
    assert runtime.construction[0]["provider"] == "openai-codex"
    assert runtime.construction[0]["subscription_only_trial"] is False


def test_native_trial_is_unqualified_before_child_dispatch(runtime):
    runtime.agent.context_rebase_enabled = True
    with pytest.raises(ContextDispatchError, match="PROVIDER_CONTEXT_RESET_UNQUALIFIED"):
        runtime.run()
    assert runtime.construction == []
    assert runtime.memory == []
    assert runtime.review == []


def test_runtime_accepts_only_explicit_success(runtime):
    runtime.turn.completed = True
    runtime.turn.final_text = "complete"
    runtime.turn.partial_text = ""
    result = runtime.run()
    assert result["completed"] is True
    assert result["final_response"] == "complete"
    assert len(runtime.memory) == 1
    assert len(runtime.review) == 1


@pytest.mark.parametrize("current_mode", ["codex_app_server", "chat_completions"])
def test_trial_prologue_blocks_native_primary_before_restore_or_auxiliary(monkeypatch, current_mode):
    from agent import turn_context

    restored = []
    def restore():
        restored.append(True)
        raise AssertionError("unqualified native primary reached runtime restoration")
    agent = SimpleNamespace(api_mode=current_mode, context_rebase_enabled=True,
                            _primary_runtime={"api_mode": "codex_app_server"},
                            session_id="offline", _memory_write_origin="assistant_tool",
                            _restore_primary_runtime=restore)
    monkeypatch.setattr(turn_context, "recover_rotated_compression_session", lambda *a: None)
    noop = lambda *a, **kw: None
    with pytest.raises(ContextDispatchError, match="PROVIDER_CONTEXT_RESET_UNQUALIFIED"):
        turn_context.build_turn_context(
            agent, "dummy", None, None, "offline", None, None,
            restore_or_build_system_prompt=noop, install_safe_stdio=noop,
            sanitize_surrogates=noop, summarize_user_message_for_log=noop,
            set_session_context=noop, set_current_write_origin=noop, ra=SimpleNamespace())
    assert restored == []
