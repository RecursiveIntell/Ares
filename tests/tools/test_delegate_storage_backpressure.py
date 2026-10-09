"""SD03B inert delegate caller gates for durable dispatch backpressure.

Only supplied recording children and callbacks are used. Dispatch never invokes
its runner; the typed pool case alone exercises one inert synchronous callback.
"""
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest


class _Sd03bChild:
    def __init__(self, identifier, pending_steer):
        self._subagent_id = identifier
        self._delegate_role = "leaf"
        self.session_id = identifier + "-session"
        self.close = Mock()
        self._drain_pending_steer = Mock(return_value=pending_steer)
        self.interrupt = Mock()
        self.tool_progress_callback = None


@pytest.fixture
def sd03b_delegate_state(monkeypatch):
    import tools.delegate_tool as caller
    import tools.async_delegation as async_owner
    import tools.approval as approval

    live = ModuleType("tools.delegation_live_log")
    writer = SimpleNamespace(path="/inert/sd03b/task-0.log", finalize=Mock())
    live.create_live_transcripts = Mock(return_value=("sd03b-delegation", [writer], [writer.path]))
    live.update_manifest_statuses = Mock()
    live.wrap_progress_callback = lambda callback, _writer: callback
    monkeypatch.setitem(sys.modules, "tools.delegation_live_log", live)
    schemas = ModuleType("tools.delegation_output_schema")
    schemas.coerce_output_schema = lambda _raw: (None, None)
    monkeypatch.setitem(sys.modules, "tools.delegation_output_schema", schemas)
    session_context = ModuleType("gateway.session_context")
    session_context.async_delivery_supported = Mock(return_value=True)
    session_context.get_session_env = Mock(return_value="")
    monkeypatch.setitem(sys.modules, "gateway.session_context", session_context)
    monkeypatch.setattr(approval, "get_current_session_key", lambda **_kw: "sd03b-session")
    monkeypatch.setattr(async_owner, "_current_origin_session_id", lambda: "sd03b-parent")
    monkeypatch.setattr(caller, "_capture_gateway_steer_authority", lambda _sid: (None, None))
    monkeypatch.setattr(caller, "is_spawn_paused", lambda: False)
    monkeypatch.setattr(caller, "_get_max_spawn_depth", lambda: 3)
    monkeypatch.setattr(caller, "_get_max_concurrent_children", lambda: 1)
    monkeypatch.setattr(caller, "_get_max_async_children", lambda: 1)
    monkeypatch.setattr(caller, "_load_config", lambda: {"max_iterations": 1})
    credentials = {
        "model": "sd03b-inert", "provider": None, "base_url": None,
        "api_key": None, "api_mode": None, "command": None, "args": None,
    }
    monkeypatch.setattr(caller, "_resolve_delegation_credentials", lambda *_a, **_kw: credentials)
    monkeypatch.setattr(caller, "_finalize_child_results", Mock())
    monkeypatch.setattr(caller, "_emit_parent_console", Mock())

    missed_steer = "Keep the exact accepted steer.\nSecond line."
    child = _Sd03bChild("sd03b-child", missed_steer)
    unrelated = _Sd03bChild("sd03b-already-running", "unrelated accepted steer")
    parent = SimpleNamespace(
        _delegate_depth=0, session_id="sd03b-parent", _interrupt_requested=False,
        _active_children=[unrelated], _active_children_lock=None,
    )
    unrelated_record = {
        "subagent_id": unrelated._subagent_id, "agent": unrelated,
        "accepting_steer": True, "status": "running", "goal": "already running",
    }
    registry = {unrelated._subagent_id: unrelated_record}
    monkeypatch.setattr(caller, "_active_subagents", registry)
    monkeypatch.setattr(caller, "_recent_subagents", {})

    def build_child(**_kwargs):
        parent._active_children.append(child)
        caller._register_subagent({
            "subagent_id": child._subagent_id, "agent": child,
            "accepting_steer": True, "status": "running", "goal": "inert child",
            "owner_agent_session_id": parent.session_id,
        })
        return child

    build = Mock(side_effect=build_child)
    monkeypatch.setattr(caller, "_build_child_preserving_parent_tools", build)
    close_steering = Mock(wraps=caller._close_subagent_steering)
    unregister = Mock(wraps=caller._unregister_subagent)
    monkeypatch.setattr(caller, "_close_subagent_steering", close_steering)
    monkeypatch.setattr(caller, "_unregister_subagent", unregister)
    run = Mock(return_value={
        "task_index": 0, "status": "completed", "summary": "inert once",
        "api_calls": 0, "duration_seconds": 0,
    })
    monkeypatch.setattr(caller, "_run_single_child", run)
    dispatch = Mock()
    monkeypatch.setattr(async_owner, "dispatch_async_delegation_batch", dispatch)
    return SimpleNamespace(
        caller=caller, parent=parent, child=child, unrelated=unrelated,
        unrelated_record=unrelated_record, registry=registry,
        missed_steer=missed_steer, build=build, run=run, dispatch=dispatch,
        close_steering=close_steering, unregister=unregister, writer=writer, live=live,
    )


@pytest.mark.parametrize("reservation_released", [False, True])
@pytest.mark.parametrize("error_code", ["durable_backlog_full", "durable_storage_unavailable"])
def test_delegate_storage_refusal_closes_only_unstarted_child_and_preserves_steer(
    sd03b_delegate_state, error_code, reservation_released
):
    state = sd03b_delegate_state
    state.dispatch.return_value = {
        "status": "rejected", "error_code": error_code,
        "execution_started": False, "error": "inert storage refusal",
        "delegation_id": "sd03b-refused-reservation",
        "durable_reservation_released": reservation_released,
    }
    result = json.loads(state.caller.delegate_task(
        goal="inert child", background=True, parent_agent=state.parent
    ))
    state.run.assert_not_called()
    state.close_steering.assert_called_once_with(state.child._subagent_id, state.child)
    state.child._drain_pending_steer.assert_called_once_with()
    state.child.close.assert_called_once_with()
    state.unrelated.close.assert_not_called()
    state.unrelated._drain_pending_steer.assert_not_called()
    assert state.registry[state.unrelated._subagent_id] is state.unrelated_record
    assert state.unrelated_record["accepting_steer"] is True
    assert state.unrelated in state.parent._active_children
    assert result["status"] == "rejected"
    assert result["error_code"] == error_code
    assert result["execution_started"] is False
    assert result["delegation_id"] == "sd03b-refused-reservation"
    assert result["durable_reservation_released"] is reservation_released
    assert result["results"][0]["missed_steer"] == state.missed_steer
    state.writer.finalize.assert_called_once()
    state.live.update_manifest_statuses.assert_called_once()
    manifest_id, entries = state.live.update_manifest_statuses.call_args.args
    assert manifest_id == "sd03b-delegation"
    assert entries[0]["status"] in {"rejected", "error", "cancelled"}
    assert entries[0]["missed_steer"] == state.missed_steer


def test_delegate_dispatch_uncertain_keeps_children_open_without_sync(sd03b_delegate_state):
    state = sd03b_delegate_state
    state.dispatch.return_value = {
        "status": "dispatch_uncertain", "error_code": "scheduling_uncertain",
        "execution_started": None, "delegation_id": "sd03b-delegation",
        "error": "inert uncertain submit",
    }
    result = json.loads(state.caller.delegate_task(
        goal="inert child", background=True, parent_agent=state.parent
    ))
    state.run.assert_not_called()
    state.close_steering.assert_not_called()
    state.unregister.assert_not_called()
    state.child.close.assert_not_called()
    state.child._drain_pending_steer.assert_not_called()
    state.child.interrupt.assert_not_called()
    state.writer.finalize.assert_not_called()
    state.live.update_manifest_statuses.assert_not_called()
    assert state.registry[state.child._subagent_id]["agent"] is state.child
    assert state.registry[state.child._subagent_id]["accepting_steer"] is True
    assert callable(state.dispatch.call_args.kwargs["runner"])
    assert result["status"] == "dispatch_uncertain"
    assert result["error_code"] == "scheduling_uncertain"
    assert result["execution_started"] is None
    assert result["delegation_id"] == "sd03b-delegation"


def test_delegate_typed_pool_capacity_runs_single_inert_child_once(sd03b_delegate_state):
    state = sd03b_delegate_state
    state.dispatch.return_value = {
        "status": "rejected", "error_code": "pool_capacity",
        "execution_started": False, "error": "inert pool capacity",
    }
    result = json.loads(state.caller.delegate_task(
        goal="inert child", background=True, parent_agent=state.parent
    ))
    state.run.assert_called_once()
    assert state.run.call_args.args[:3] == (0, "inert child", state.child)
    assert result["results"][0]["status"] == "completed"
    assert result["results"][0]["summary"] == "inert once"
    assert "SYNCHRONOUSLY" in result["note"]
