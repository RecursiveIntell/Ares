"""Control races through real agent and registry boundaries on disposable DBs."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ares_runtime.continuity.runtime import (
    ContextDispatchError, context_tool_control_scope, settle_final_context_dispatch,
)
from tests.ares_runtime.test_continuity_dispatch import durable_agent  # noqa: F401
from tests.run_agent.test_run_agent import agent, _mock_response, _mock_tool_call  # noqa: F401


def admit(agent, db):
    db.append_message(agent.session_id, "user", "Run the permitted action")
    assert db.try_acquire_session_turn_lease(agent.session_id, "holder", ttl_seconds=300)
    agent._active_session_turn_lease_holder = "holder"
    snapshot = db.read_context_rebase_snapshot(agent.session_id)
    record = db.admit_context_dispatch(
        agent.session_id, turn_lease_holder="holder", attempt_id="attempt-1",
        expected_snapshot_digest=snapshot.digest, payload_digest="sha256:" + "a" * 64,
        route_ref="test",
    )
    settle_final_context_dispatch(agent, record)
    return record


def mutate(agent, db, mutation):
    if mutation == "stop":
        db.record_context_stop(agent.session_id)
    elif mutation == "correct":
        db.append_message(agent.session_id, "user", "Do not run that action")
    elif mutation == "edit_same_id":
        db._execute_write(lambda conn: conn.execute(
            "UPDATE messages SET content=? WHERE session_id=? AND role='user'",
            ("Do not run that action", agent.session_id),
        ))
    elif mutation == "lease":
        db.release_session_turn_lease(agent.session_id, "holder")
        assert db.try_acquire_session_turn_lease(agent.session_id, "new-holder", ttl_seconds=300)


@pytest.mark.parametrize("mutation", ["stop", "correct", "edit_same_id", "lease"])
def test_registry_rechecks_after_edit_approval(durable_agent, mutation):
    import model_tools

    agent, db = durable_agent
    admit(agent, db)
    db.append_message(agent.session_id, "assistant", "Observed model response")

    def approve(*_):
        mutate(agent, db, mutation)
        return None

    with (context_tool_control_scope(agent),
          patch("acp_adapter.edit_approval.maybe_require_edit_approval", side_effect=approve),
          patch.object(model_tools.registry, "dispatch") as effect):
        result = model_tools.handle_function_call("web_search", {"query": "allowed"}, session_id=agent.session_id)
    assert "CONTEXT_TOOL_CONTROL_SUPERSEDED" in result or "TURN_LEASE_MISMATCH" in result
    effect.assert_not_called()


def test_registry_allows_assistant_and_settled_tool_observations(durable_agent):
    import model_tools

    agent, db = durable_agent
    admit(agent, db)
    db.append_message(agent.session_id, "assistant", "Observed model response")
    db.append_message(agent.session_id, "tool", "Completed earlier observation", tool_call_id="earlier")
    with (context_tool_control_scope(agent),
          patch.object(model_tools.registry, "dispatch", return_value='{"ok":true}') as effect):
        result = model_tools.handle_function_call("web_search", {"query": "allowed"}, session_id=agent.session_id)
    assert json.loads(result)["ok"] is True
    effect.assert_called_once()


def test_registry_rechecks_after_execution_middleware(durable_agent):
    import model_tools

    agent, db = durable_agent
    admit(agent, db)

    def middleware(_name, args, execute, **_):
        db.record_context_stop(agent.session_id)
        return execute(args)

    with (context_tool_control_scope(agent),
          patch("hermes_cli.middleware.run_tool_execution_middleware", side_effect=middleware),
          patch.object(model_tools.registry, "dispatch") as effect):
        result = model_tools.handle_function_call("web_search", {"query": "allowed"}, session_id=agent.session_id)
    assert "CONTEXT_TOOL_CONTROL_SUPERSEDED" in result or "TURN_LEASE_MISMATCH" in result
    effect.assert_not_called()


def test_old_worker_keeps_its_response_binding_across_thread_propagation(durable_agent):
    from ares_runtime.continuity.runtime import assert_context_tool_control_current
    from tools.thread_context import propagate_context_to_thread

    agent, db = durable_agent
    admit(agent, db)
    with context_tool_control_scope(agent):
        worker = propagate_context_to_thread(assert_context_tool_control_current)
    # A later agent response cannot replace the immutable worker prerequisite.
    agent._context_response_admission = {"attempt_id": "new-attempt"}
    db.record_context_stop(agent.session_id)
    with pytest.raises(ContextDispatchError, match="CONTEXT_TOOL_CONTROL_SUPERSEDED"):
        worker()


@pytest.mark.parametrize("mutation", [None, "stop", "correct"])
def test_actual_agent_tool_boundary_preserves_or_refuses_response(durable_agent, mutation):
    agent, db = durable_agent
    agent.client.chat.completions.create.return_value = _mock_response(
        content="", finish_reason="tool_calls", tool_calls=[_mock_tool_call("web_search")],
    )
    original = agent._execute_tool_calls

    def execute(*args, **kwargs):
        if mutation:
            mutate(agent, db, mutation)
        return original(*args, **kwargs)

    with (patch.object(agent, "_execute_tool_calls", side_effect=execute),
          patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"),
          patch("run_agent.handle_function_call", return_value='{"ok":true}') as effect):
        result = agent.run_conversation("Perform the permitted action")
    if mutation:
        effect.assert_not_called()
        assert result["error"] == "CONTEXT_TOOL_CONTROL_SUPERSEDED"
        assert result["completed"] is False
    else:
        effect.assert_called_once()


def test_registry_native_permit_binds_post_middleware_arguments(durable_agent):
    import model_tools

    agent, db = durable_agent
    admit(agent, db)
    consumed = []

    def boundary(name, args, **kwargs):
        if kwargs.get("consume_permit"):
            consumed.append(dict(args))
        return True, "allowed", SimpleNamespace()

    def middleware(_name, args, execute, **_):
        return execute({**args, "query": "final"})

    with (context_tool_control_scope(agent),
          patch("ares_runtime.collaboration.production_permit_canary_context", return_value=object()),
          patch("ares_runtime.collaboration.dispatcher_boundary", side_effect=boundary),
          patch("hermes_cli.middleware.run_tool_execution_middleware", side_effect=middleware),
          patch.object(model_tools, "_record_ares_consumed_outcome") as settled,
          patch.object(model_tools.registry, "dispatch", return_value='{"ok":true}') as effect):
        model_tools.handle_function_call("web_search", {"query": "initial"}, session_id=agent.session_id)
    assert consumed == [{"query": "final"}]
    assert effect.call_args.args[1] == consumed[0]
    assert settled.call_args.kwargs["function_args"] == consumed[0]
