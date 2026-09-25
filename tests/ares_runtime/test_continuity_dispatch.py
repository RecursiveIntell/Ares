"""Exercise real AIAgent admission paths with a recording provider double."""
from unittest.mock import patch

import pytest

from ares_runtime.continuity.runtime import (
    AutomaticRebaseError, AutomaticRebaseResult, AutomaticRebaseStatus,
)
from tests.run_agent.test_run_agent import agent, _mock_response  # noqa: F401


@pytest.mark.parametrize("boundary", ["turn_start", "pre_api"])
@pytest.mark.parametrize("reason", [
    "CONTEXT_REBASE_STALE_SNAPSHOT",
    "CONTEXT_REBASE_UNRESOLVED_EFFECTS",
    "TOO_MANY_UNRESOLVED_EFFECTS",
    "SUCCESSOR_INSUFFICIENT_RUNWAY",
])
def test_blocked_rebase_stops_before_provider_and_tool_admission(agent, boundary, reason):
    from agent import conversation_loop

    agent._cached_system_prompt = "Trusted instructions"
    agent._use_prompt_caching = False
    agent.save_trajectories = False
    agent.context_rebase_enabled = True
    agent.compression_enabled = boundary == "turn_start"
    agent.max_iterations = 1
    agent.context_compressor.threshold_tokens = 100
    agent.client.chat.completions.create.return_value = _mock_response(
        content="This provider call must never happen", finish_reason="stop",
    )
    result = AutomaticRebaseResult(AutomaticRebaseStatus.BLOCKED, reason, agent.session_id)
    history = [{"role": "user", "content": "old request"},
               {"role": "assistant", "content": "old evidence " * 100}]
    original_build = conversation_loop.build_turn_context

    def build_then_enable(*args, **kwargs):
        context = original_build(*args, **kwargs)
        agent.compression_enabled = True
        return context

    with (
        patch.object(agent.context_compressor, "should_compress_info",
                     return_value=(False, "insufficient_progress")),
        patch.object(agent.context_compressor, "should_defer_preflight_to_real_usage",
                     return_value=False, create=True),
        patch("ares_runtime.continuity.runtime.attempt_turn_start_context_rebase", return_value=result) as rebase,
        patch("agent.conversation_loop.build_turn_context", side_effect=build_then_enable),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("run_agent.handle_function_call") as tool,
    ):
        if boundary == "turn_start":
            with pytest.raises(AutomaticRebaseError, match=reason):
                agent.run_conversation("continue", conversation_history=history)
        else:
            stopped = agent.run_conversation("continue", conversation_history=history)
            assert stopped["failed"] is True
            assert stopped["completed"] is False
            assert stopped["error"] == reason
            assert stopped["api_calls"] == 0
            assert agent.iteration_budget.used == 0
        rebase.assert_called_once()
        agent.client.chat.completions.create.assert_not_called()
        tool.assert_not_called()


@pytest.mark.parametrize("status_code", [400, 413])
def test_provider_overflow_refusal_preserves_charge_and_stops_retry(agent, status_code):
    agent._cached_system_prompt = "Trusted instructions"
    agent._use_prompt_caching = False
    agent.save_trajectories = False
    agent.compression_enabled = True
    agent.context_rebase_enabled = True
    agent.max_compression_attempts = 0
    agent.max_iterations = 3
    agent.context_compressor.threshold_tokens = 1_000_000
    error = Exception("maximum context length exceeded" if status_code == 400 else "request entity too large")
    error.status_code = status_code
    agent.client.chat.completions.create.side_effect = error
    refusal = AutomaticRebaseResult(
        AutomaticRebaseStatus.BLOCKED, "CONTEXT_REBASE_UNRESOLVED_EFFECTS", agent.session_id,
    )
    with (
        patch("ares_runtime.continuity.runtime.attempt_turn_start_context_rebase", return_value=refusal) as rebase,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("run_agent.handle_function_call") as tool,
    ):
        stopped = agent.run_conversation("continue")
    assert stopped["error"] == refusal.reason
    assert stopped["failed"] is True
    assert stopped["completed"] is False
    assert stopped["api_calls"] == 1
    assert agent.iteration_budget.used == 1
    assert agent.client.chat.completions.create.call_count == 1
    rebase.assert_called_once()
    tool.assert_not_called()


@pytest.mark.parametrize("reason", ["NO_PROGRESS_REBASE_LIMIT", "PROVIDER_CONTEXT_RESET_UNQUALIFIED"])
def test_early_turn_start_refusal_preserves_authentic_input_in_real_store(agent, tmp_path, reason):
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "blocked-input.db")
    agent._session_db = db
    agent.session_id = "blocked-input"
    db.create_session(agent.session_id, source="cli", profile_name="default")
    agent._session_db_created = True
    agent._cached_system_prompt = "Trusted instructions"
    agent._use_prompt_caching = False
    agent.save_trajectories = False
    agent.compression_enabled = True
    agent.context_rebase_enabled = True
    agent.context_compressor.threshold_tokens = 100
    refused = AutomaticRebaseResult(AutomaticRebaseStatus.BLOCKED, reason, agent.session_id)
    actual_input = ("Keep my new restriction: do not publish. " * 30).rstrip()
    try:
        with (
            patch.object(agent.context_compressor, "should_compress_info", return_value=(False, "insufficient_progress")),
            patch("ares_runtime.continuity.runtime.attempt_turn_start_context_rebase", return_value=refused),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            with pytest.raises(AutomaticRebaseError, match=reason):
                agent.run_conversation(actual_input)
        stored = db.get_messages_as_conversation(agent.session_id)
        assert [row["content"] for row in stored if row["role"] == "user"] == [actual_input]
        agent.client.chat.completions.create.assert_not_called()
    finally:
        db.close()
