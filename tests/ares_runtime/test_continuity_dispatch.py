"""Exercise real AIAgent admission paths with a recording provider double."""
from unittest.mock import patch

import pytest

from ares_runtime.continuity.runtime import (
    AutomaticRebaseError, AutomaticRebaseResult, AutomaticRebaseStatus,
)
from tests.run_agent.test_run_agent import agent, _mock_response  # noqa: F401


@pytest.fixture
def durable_agent(agent, tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "admission.db")
    db.create_session(agent.session_id, source="cli", profile_name="p1", model="test")
    agent._session_db = db
    agent._session_db_created = True
    agent._cached_system_prompt = "Trusted instructions"
    agent.context_rebase_enabled = True
    agent.compression_enabled = False
    agent.max_tokens = 1000
    agent.context_compressor.context_length = 256_000
    agent.context_compressor.threshold_tokens = 128_000
    agent.max_iterations = 1
    try:
        yield agent, db
    finally:
        db.close()


def test_dispatch_repairs_before_durable_flush_and_preserves_user_occurrences(durable_agent):
    from agent import conversation_loop

    agent, db = durable_agent
    original_build = conversation_loop.build_turn_context

    def malformed_tail(*args, **kwargs):
        context = original_build(*args, **kwargs)
        context.messages[:0] = [
            {"role": "tool", "tool_call_id": "missing", "content": "orphan"},
            {"role": "assistant", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "Earlier correction", "timestamp": 1.0},
        ]
        context.current_turn_user_idx += 4
        agent._persist_user_message_idx = context.current_turn_user_idx
        return context

    agent.client.chat.completions.create.return_value = _mock_response(content="Done", finish_reason="stop")
    with (patch("agent.conversation_loop.build_turn_context", side_effect=malformed_tail),
          patch.object(agent, "_persist_session"), patch.object(agent, "_save_trajectory"),
          patch.object(agent, "_cleanup_task_resources")):
        result = agent.run_conversation("Current correction")
    assert result.get("error") is None, result
    stored = db.get_messages_as_conversation(agent.session_id)
    assert not any(row["role"] == "tool" for row in stored)
    assistants = [row["content"] for row in stored if row["role"] == "assistant"]
    assert assistants == ["first\nsecond", "Done"]
    assert [row["content"] for row in stored if row["role"] == "user"] == [
        "Earlier correction", "Current correction"]
    sent = agent.client.chat.completions.create.call_args.kwargs["messages"]
    assert any(row.get("content") == assistants[0] for row in sent)


@pytest.mark.parametrize("api_text, admitted", [("token", False), ("prefix\n\nok\n\nsuffix", True)])
def test_dispatch_requires_standalone_authentic_input(durable_agent, api_text, admitted):
    agent, db = durable_agent
    agent.client.chat.completions.create.return_value = _mock_response(content="Done", finish_reason="stop")
    with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        result = agent.run_conversation(api_text, persist_user_message="ok")
    assert agent.client.chat.completions.create.call_count == int(admitted)
    if admitted:
        assert result.get("error") is None, result
    else:
        assert result["error"] == "CONTEXT_DISPATCH_STALE_MATERIALIZATION"
    assert [row["content"] for row in db.get_messages(agent.session_id) if row["role"] == "user"] == ["ok"]


def test_actual_turn_recovers_committed_child_before_provider_dispatch(agent, tmp_path):
    from hermes_state import SessionDB
    from tests.ares_runtime.test_context_rebase_state import _publish

    db = SessionDB(db_path=tmp_path / "restart.db")
    try:
        db.create_session("s0", source="cli", profile_name="p1", model="test")
        db.append_message("s0", "user", "Original requirement")
        assert db.try_acquire_session_turn_lease("s0", "holder", ttl_seconds=300)
        transition = _publish(db)
        db.release_session_turn_lease("s0", "holder")
        agent._session_db = db
        agent.session_id = "s1"
        agent._session_db_created = True
        agent._cached_system_prompt = "trusted base prompt"
        agent.compression_enabled = False
        agent.max_iterations = 1
        agent.max_tokens = 1000
        agent.client.chat.completions.create.return_value = _mock_response(content="continued", finish_reason="stop")
        with (
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("New authentic correction")
        assert db.read_context_rebase_transition(transition.transition_id).state == "ready"
        assert agent.session_id == "s1"
        assert result.get("error") is None, result
        assert agent.client.chat.completions.create.call_count == 1
        assert result["api_calls"] == 1
        assert any(row["content"] == "New authentic correction" for row in db.get_messages_as_conversation("s1"))
    finally:
        db.close()


def test_actual_turn_preserves_input_when_engine_recovery_refuses(agent, tmp_path):
    from hermes_state import SessionDB
    from tests.ares_runtime.test_context_rebase_state import _publish

    db = SessionDB(db_path=tmp_path / "blocked-restart.db")
    try:
        db.create_session("s0", source="cli", profile_name="p1", model="test")
        db.append_message("s0", "user", "Original requirement")
        assert db.try_acquire_session_turn_lease("s0", "holder", ttl_seconds=300)
        transition = _publish(db)
        db.release_session_turn_lease("s0", "holder")
        agent._session_db = db
        agent.session_id = "s1"
        agent._session_db_created = True
        with patch.object(agent.context_compressor, "on_session_start", side_effect=RuntimeError("owner unavailable"), create=True):
            with pytest.raises(AutomaticRebaseError, match="CONTEXT_ENGINE_REBIND_FAILED"):
                agent.run_conversation("Stop and preserve this correction")
        assert db.read_context_rebase_transition(transition.transition_id).state == "reconciliation_required"
        assert any(row["content"] == "Stop and preserve this correction" for row in db.get_messages_as_conversation("s1"))
        agent.client.chat.completions.create.assert_not_called()
    finally:
        db.close()


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
def test_provider_overflow_refusal_preserves_charge_and_stops_retry(durable_agent, status_code):
    agent, _ = durable_agent
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


@pytest.mark.parametrize("mutation,reason", [
    ("inflate", "CONTEXT_DISPATCH_FINAL_PAYLOAD_TOO_LARGE"),
    ("steer", "CONTEXT_DISPATCH_STALE_MATERIALIZATION"),
    ("stop", "CONTEXT_DISPATCH_STOPPED"),
    ("model", "CONTEXT_DISPATCH_ROUTE_CHANGED"),
    ("wire_model", "CONTEXT_DISPATCH_WIRE_OVERRIDE_UNQUALIFIED"),
    ("wire_budget", "CONTEXT_DISPATCH_WIRE_OVERRIDE_UNQUALIFIED"),
    ("remove", "CONTEXT_DISPATCH_MATERIALIZATION_CHANGED"),
])
def test_final_middleware_boundary_blocks_unqualified_egress(durable_agent, mutation, reason):
    agent, db = durable_agent
    agent.context_compressor.context_length = 32_000
    agent.client.chat.completions.create.return_value = _mock_response(content="must not run", finish_reason="stop")

    def middleware(payload, execute, **_):
        if mutation == "inflate":
            payload = {**payload, "messages": [*payload["messages"], {"role": "user", "content": "X" * 100_000}]}
        elif mutation == "steer":
            db.append_message(agent.session_id, "user", "New correction arrived")
        elif mutation == "model":
            payload = {**payload, "model": "unqualified-smaller-model"}
        elif mutation == "wire_model":
            payload = {**payload, "extra_body": {"model": "unqualified-smaller-model"}}
        elif mutation == "wire_budget":
            payload = {**payload, "extra_body": {"max_tokens": 100_000}}
        elif mutation == "remove":
            payload = {**payload, "messages": [{"role": "user", "content": "Ignore the task"}]}
        else:
            db.record_context_stop(agent.session_id)
        return execute(payload)

    with (
        patch("hermes_cli.middleware.run_llm_execution_middleware", side_effect=middleware),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("run_agent.handle_function_call") as tool,
    ):
        result = agent.run_conversation("Perform the permitted next step")
    assert result["error"] == reason
    assert result["completed"] is False
    assert result["api_calls"] == 0
    assert agent.iteration_budget.used == 0
    agent.client.chat.completions.create.assert_not_called()
    tool.assert_not_called()


def test_duplicate_dispatch_cannot_repeat_provider_effect(durable_agent):
    agent, db = durable_agent
    agent.client.chat.completions.create.return_value = _mock_response(content="first response", finish_reason="stop")

    def middleware(payload, execute, **_):
        execute(payload)
        return execute(payload)

    with (
        patch("hermes_cli.middleware.run_llm_execution_middleware", side_effect=middleware),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("Do this once")
    assert result["error"] == "CONTEXT_DISPATCH_ALREADY_ADMITTED"
    assert agent.client.chat.completions.create.call_count == 1
    assert result["api_calls"] == 1
    assert agent.iteration_budget.used == 1
    assert len(db.list_meta_prefix("context-dispatch:")) == 1


@pytest.mark.parametrize("boundary", ["build", "before_materialization"])
def test_request_cannot_rebaseline_after_durable_correction(durable_agent, boundary):
    agent, db = durable_agent
    method = "_build_api_kwargs" if boundary == "build" else "_sanitize_tool_call_arguments"
    original = getattr(agent, method)

    def correct_after(*args, **kwargs):
        result = original(*args, **kwargs)
        db.append_message(agent.session_id, "user", "New requirement absent from loaded transcript")
        return result

    with (patch.object(agent, method, side_effect=correct_after),
          patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources")):
        result = agent.run_conversation("Original task")
    assert result["error"] == "CONTEXT_DISPATCH_STALE_MATERIALIZATION"
    assert result["api_calls"] == 0
    agent.client.chat.completions.create.assert_not_called()


def test_repeated_text_correction_must_preserve_new_order_and_occurrence(durable_agent):
    agent, db = durable_agent
    original = agent._sanitize_tool_call_arguments
    history = [{"role": "user", "content": "Proceed", "timestamp": 1.0},
               {"role": "assistant", "content": "Acknowledged", "timestamp": 2.0}]
    for message in history:
        db.append_message(agent.session_id, message["role"], message["content"], timestamp=message["timestamp"])

    def correct_after(*args, **kwargs):
        result = original(*args, **kwargs)
        db.append_message(agent.session_id, "user", "Proceed")
        return result

    with (patch.object(agent, "_sanitize_tool_call_arguments", side_effect=correct_after),
          patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources")):
        result = agent.run_conversation("Do not proceed", conversation_history=history)
    assert result["error"] == "CONTEXT_DISPATCH_STALE_MATERIALIZATION"
    agent.client.chat.completions.create.assert_not_called()


def test_hard_stop_cancels_before_durable_recording_and_survives_failure(durable_agent):
    from unittest.mock import Mock

    agent, db = durable_agent
    abort, child = Mock(), Mock()
    agent._active_request_abort = abort
    agent._execution_thread_id = 912345
    agent._active_children = [child]

    def unavailable(*_):
        # This assertion is also the delayed-storage witness: all local
        # cancellation has happened before storage can block or fail.
        assert agent._hard_interrupt_requested.is_set()
        abort.assert_called_once_with("interrupt_abort")
        child.hard_interrupt.assert_called_once()
        signal.assert_called_with(True, 912345, reason="explicit stop requested")
        raise OSError("durable store unavailable")

    with patch("run_agent._set_interrupt") as signal, patch.object(db, "record_context_stop", side_effect=unavailable):
        agent.interrupt(hard_cancel=True)
    assert agent._context_stop_unacknowledged is True
    assert agent._interrupt_requested is True


@pytest.mark.parametrize("supersede", [False, True])
def test_real_stream_delivers_only_after_durable_settlement(durable_agent, supersede):
    from unittest.mock import Mock
    from tests.run_agent.test_streaming import _make_stream_chunk

    agent, db = durable_agent
    delivered, tts = [], []
    agent.stream_delta_callback = delivered.append
    agent._stream_callback = tts.append
    client = Mock()

    def chunks():
        yield _make_stream_chunk(content="First ")
        assert delivered == [] and tts == []
        if supersede:
            db.record_context_stop(agent.session_id)
        yield _make_stream_chunk(content="second", finish_reason="stop")
        assert delivered == [] and tts == []

    client.chat.completions.create.return_value = chunks()
    with (patch.object(agent, "_create_request_openai_client", return_value=client),
          patch.object(agent, "_close_request_openai_client"),
          patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"),
          patch("run_agent.handle_function_call") as tool):
        result = agent.run_conversation("Continue safely", stream_callback=tts.append)
    client.chat.completions.create.assert_called_once()
    tool.assert_not_called()
    if supersede:
        assert result["error"] == "CONTEXT_DISPATCH_RESPONSE_SUPERSEDED"
        assert delivered == [] and tts == []
    else:
        assert "First second" in "".join(value for value in delivered if value)
        assert "First second" in "".join(value for value in tts if value)


def test_failed_stream_does_not_retry_past_a_durable_stop(durable_agent):
    import httpx
    from unittest.mock import Mock

    agent, db = durable_agent
    client = Mock()
    agent.stream_delta_callback = Mock()
    agent.max_iterations = 3

    def failed_stream():
        db.record_context_stop(agent.session_id)
        raise httpx.ReadError("stream lost")
        yield  # real iterator, never a complete-response mock

    client.chat.completions.create.side_effect = lambda **_: failed_stream()
    with (patch.object(agent, "_create_request_openai_client", return_value=client),
          patch.object(agent, "_close_request_openai_client"),
          patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources")):
        result = agent.run_conversation("Continue safely")
    assert client.chat.completions.create.call_count == 1
    assert result["completed"] is False
    agent.stream_delta_callback.assert_not_called()


def test_partial_failed_stream_is_discarded_before_qualified_retry(durable_agent):
    import httpx
    from unittest.mock import Mock
    from tests.run_agent.test_streaming import _make_stream_chunk

    agent, db = durable_agent
    client = Mock()
    delivered = []
    agent.stream_delta_callback = delivered.append
    agent.max_iterations = 3

    def failed_stream():
        yield _make_stream_chunk(content="Unaccepted partial text")
        assert delivered == []
        raise httpx.ReadTimeout("stream lost after text")

    client.chat.completions.create.side_effect = [failed_stream(), iter([
        _make_stream_chunk(content="Accepted response", finish_reason="stop"),
    ])]
    with (patch.object(agent, "_create_request_openai_client", return_value=client),
          patch.object(agent, "_close_request_openai_client"),
          patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources")):
        result = agent.run_conversation("Continue safely")
    assert client.chat.completions.create.call_count == 2
    assert result["api_calls"] > 0 and agent.iteration_budget.used > 0
    assert len(db.list_meta_prefix("context-dispatch:")) == 2
    assert "Unaccepted partial text" not in str(delivered)
    assert "".join(value for value in delivered if value).count("Accepted response") == 1
    assert result["final_response"] == "Accepted response"
    assert any(row["content"] == "Accepted response" for row in db.get_messages_as_conversation(agent.session_id))
    assert all("Unaccepted partial text" not in str(row) for row in db.get_messages_as_conversation(agent.session_id))
    import json
    settlements = [json.loads(raw) for _, raw in db.list_meta_prefix("context-dispatch-result:")]
    assert {r["disposition"] for r in settlements} == {"response_discarded", "response_received"}
    assert db.read_context_input_work(agent.session_id)["phase"]["state"] == "answered"


def test_buffered_tool_stream_observer_failure_does_not_retry(durable_agent):
    from unittest.mock import Mock
    from tests.run_agent.test_streaming import _make_stream_chunk, _make_tool_call_delta
    from ares_runtime.continuity.runtime import ContextDispatchStreamBuffer, settle_final_context_dispatch

    agent, db = durable_agent
    db.append_message(agent.session_id, "user", "Inspect")
    assert db.try_acquire_session_turn_lease(agent.session_id, "holder", ttl_seconds=300)
    agent._active_session_turn_lease_holder = "holder"
    snapshot = db.read_context_rebase_snapshot(agent.session_id)
    admission = db.admit_context_dispatch(agent.session_id, turn_lease_holder="holder", attempt_id="observer",
                                        expected_snapshot_digest=snapshot.digest,
                                        payload_digest="sha256:" + "a" * 64, route_ref="test")
    agent.stream_delta_callback = Mock(side_effect=RuntimeError("observer unavailable"))
    client = Mock()
    client.chat.completions.create.return_value = iter([
        _make_stream_chunk(tool_calls=[_make_tool_call_delta(tc_id="call-1", name="inspect", arguments="{}")]),
        _make_stream_chunk(content="Inspection commentary", finish_reason="tool_calls"),
    ])
    with (patch.object(agent, "_create_request_openai_client", return_value=client),
          patch.object(agent, "_close_request_openai_client")):
        with ContextDispatchStreamBuffer(agent, admission) as buffer:
            response = agent._interruptible_streaming_api_call({"model": agent.model, "messages": []})
        settle_final_context_dispatch(agent, admission)
        buffer.deliver()
    assert response.choices[0].message.tool_calls
    client.chat.completions.create.assert_called_once()
    agent.stream_delta_callback.assert_called_once_with("Inspection commentary")


@pytest.mark.parametrize("supersede", [False, True])
def test_codex_completed_commentary_waits_for_settlement(durable_agent, supersede):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from agent.codex_runtime import run_codex_stream
    from ares_runtime.continuity.runtime import ContextDispatchError, ContextDispatchStreamBuffer, settle_final_context_dispatch

    agent, db = durable_agent
    db.append_message(agent.session_id, "user", "Inspect")
    assert db.try_acquire_session_turn_lease(agent.session_id, "holder", ttl_seconds=300)
    agent._active_session_turn_lease_holder = "holder"
    snapshot = db.read_context_rebase_snapshot(agent.session_id)
    admission = db.admit_context_dispatch(agent.session_id, turn_lease_holder="holder", attempt_id="codex-commentary",
                                        expected_snapshot_digest=snapshot.digest,
                                        payload_digest="sha256:" + "a" * 64, route_ref="test-codex")
    delivered = []
    agent.interim_assistant_callback = lambda text, **_: delivered.append(text)
    agent.show_commentary = True
    client = Mock()
    item = SimpleNamespace(type="message", phase="commentary", status="completed",
                           content=[SimpleNamespace(type="output_text", text="Inspecting now")])

    def events():
        yield SimpleNamespace(type="response.output_item.done", item=item)
        assert delivered == []
        if supersede:
            db.record_context_stop(agent.session_id)
        yield SimpleNamespace(type="response.completed", response=SimpleNamespace(
            id="r1", status="completed", output=[item], usage=None))

    client.responses.create.return_value = events()
    with ContextDispatchStreamBuffer(agent, admission) as buffer:
        run_codex_stream(agent, {"model": agent.model, "input": []}, client=client)
    assert delivered == []
    if supersede:
        with pytest.raises(ContextDispatchError, match="CONTEXT_DISPATCH_RESPONSE_SUPERSEDED"):
            settle_final_context_dispatch(agent, admission)
    else:
        settle_final_context_dispatch(agent, admission)
        buffer.deliver()
        assert delivered == ["Inspecting now"]
    client.responses.create.assert_called_once()


def test_response_after_durable_stop_cannot_be_consumed(durable_agent):
    agent, db = durable_agent

    def provider(**_):
        db.record_context_stop(agent.session_id)
        return _mock_response(content="stale success", finish_reason="stop")

    agent.client.chat.completions.create.side_effect = provider
    with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        result = agent.run_conversation("Perform the next step")
    assert result["error"] == "CONTEXT_DISPATCH_RESPONSE_SUPERSEDED"
    assert result["completed"] is False
    assert agent.client.chat.completions.create.call_count == 1
    import json
    settlement = json.loads(db.list_meta_prefix("context-dispatch-result:")[0][1])
    assert settlement["disposition"] == "quarantined"


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
