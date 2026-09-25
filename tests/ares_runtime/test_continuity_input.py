"""Input acceptance survives lease contention and fences stale execution."""
from dataclasses import asdict
import json
import subprocess
import sys
from unittest.mock import patch

import pytest

from hermes_state import SessionDB
from hermes_state_continuity import ContextContinuationError
from tests.ares_runtime.test_continuity_dispatch import durable_agent  # noqa: F401
from tests.run_agent.test_run_agent import agent, _mock_response  # noqa: F401


@pytest.fixture
def db(tmp_path):
    value = SessionDB(tmp_path / "state.db")
    value.create_session("s", source="cli")
    value.append_message("s", "user", "Original input", timestamp=1.0)
    assert value.try_acquire_session_turn_lease("s", "holder", ttl_seconds=300)
    yield value
    value.close()


def accept(db, event="e1", content="Correction", **kwargs):
    return db.accept_context_input("s", source="cli", event_id=event, content=content, **kwargs)


def row(receipt):
    return {"role": "user", "content": receipt.content, "timestamp": receipt.timestamp,
            "display_metadata": receipt.display_metadata, "_context_input": {
                "conversation_root": receipt.conversation_root, "sequence": receipt.sequence,
                "payload_digest": receipt.payload_digest}}


def project(db, receipt):
    return db.append_messages_batch("s", [row(receipt)], turn_lease_holder="holder")


def test_native_acceptance_deduplicates_across_reopen_without_lease(db):
    original = accept(db)
    script = """
import json,sys
from dataclasses import asdict
from pathlib import Path
from hermes_state import SessionDB
db=SessionDB(Path(sys.argv[1]))
r=db.accept_context_input('s',source='cli',event_id='e1',content='Correction')
print(json.dumps(asdict(r)))
db.close()
"""
    result = subprocess.run([sys.executable, "-c", script, str(db.db_path)],
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == asdict(original)
    assert db.read_context_input("s", source="cli", event_id="e1") == original
    with pytest.raises(ContextContinuationError, match="ID_COLLISION"):
        accept(db, content="Changed same event")
    assert accept(db, event="e2").sequence == 2


def test_late_input_invalidates_dispatch_and_tools_before_projection(db):
    initial = db.read_context_rebase_snapshot("s")
    db.admit_context_dispatch("s", turn_lease_holder="holder", attempt_id="attempt",
        expected_snapshot_digest=initial.digest, payload_digest="sha256:" + "a" * 64, route_ref="test")
    accept(db)
    current = db.read_context_rebase_snapshot("s")
    assert current.input_watermark == initial.input_watermark
    assert current.action_control_digest != initial.action_control_digest
    assert current.has_pending_inputs
    with pytest.raises(ContextContinuationError, match="RESPONSE_SUPERSEDED"):
        db.settle_context_dispatch_response("attempt", turn_lease_holder="holder")
    with pytest.raises(ContextContinuationError, match="INPUT_PENDING"):
        db.admit_context_dispatch("s", turn_lease_holder="holder", attempt_id="new",
            expected_snapshot_digest=current.digest, payload_digest="sha256:" + "a" * 64, route_ref="test")


def test_projection_order_atomicity_and_clean_api_sidecar(db):
    first, second = accept(db), accept(db, "e2", "Next requirement")
    with pytest.raises(ContextContinuationError, match="ORDER_MISMATCH"):
        project(db, second)
    with pytest.raises(ContextContinuationError):
        db.append_messages_batch("s", [row(first)])
    bad = row(second)
    bad["content"] = "Mutated"
    with pytest.raises(ContextContinuationError, match="PAYLOAD_MISMATCH"):
        db.append_messages_batch("s", [row(first), bad], turn_lease_holder="holder")
    assert db.read_context_rebase_snapshot("s").has_pending_inputs
    assert len(db.get_messages("s")) == 1
    result = db.project_context_inputs_before("s", receipt=second, turn_lease_holder="holder")
    assert result == {"inserted": 1, "projection": None}
    message = row(second)
    message["api_content"] = "API-only note\nNext requirement"
    assert db.append_messages_batch("s", [message], turn_lease_holder="holder") == 1
    assert project(db, second) == 0
    assert not db.read_context_rebase_snapshot("s").has_pending_inputs
    rows = db.get_messages("s")
    assert [r["content"] for r in rows] == ["Original input", "Correction", "Next requirement"]
    assert rows[-1]["api_content"] == message["api_content"]
    assert db.get_session("s")["message_count"] == 3


def test_receipt_read_rejects_corrupt_pointer(db):
    receipt = accept(db)
    from hermes_state_inbox import _hash, _prefix
    db.set_meta(_prefix("s") + ":dedup:" + _hash(["cli", "e1"]),
                json.dumps({"sequence": receipt.sequence, "payload_digest": "b" * 64}))
    with pytest.raises(ContextContinuationError, match="RECORD_INVALID"):
        db.read_context_input("s", source="cli", event_id="e1")


def test_edited_projection_cannot_authorize_duplicate_delivery(db):
    receipt = accept(db)
    project(db, receipt)
    db._execute_write(lambda conn: conn.execute(
        "UPDATE messages SET content='Changed' WHERE content='Correction'"))
    with pytest.raises(ContextContinuationError, match="PROJECTION_CHANGED"):
        project(db, receipt)


def test_aliases_share_order_but_forks_do_not(db):
    first = accept(db)
    db.create_session("child", source="cli", parent_session_id="s")
    db.end_session("s", end_reason="compression")
    child = db.accept_context_input("child", source="cli", event_id="e2", content="Next")
    assert (child.conversation_root, child.sequence) == (first.conversation_root, 2)
    db.create_session("fork", source="tool", parent_session_id="child")
    fork = db.accept_context_input("fork", source="cli", event_id="e1", content="Independent")
    assert fork.conversation_root == "fork" and fork.sequence == 1
    db.create_session("wrong", source="cli", parent_session_id="child", profile_name="other")
    db.end_session("child", end_reason="compression")
    with pytest.raises(ContextContinuationError, match="PROFILE_MISMATCH"):
        db.accept_context_input("wrong", source="cli", event_id="e3", content="Cross-profile")


def test_real_turn_enrolls_once_and_preserves_api_sidecar(durable_agent):
    agent, db = durable_agent
    agent.client.chat.completions.create.return_value = _mock_response(content="Done", finish_reason="stop")
    with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        result = agent.run_conversation("API-only note\nClean request", persist_user_message="Clean request",
                                        persist_user_event_id="delivery1")
        assert result.get("error") is None, result
        with pytest.raises(ContextContinuationError, match="ALREADY_PROJECTED"):
            agent.run_conversation("API-only note\nClean request", persist_user_message="Clean request",
                                   persist_user_event_id="delivery1")
    assert agent.client.chat.completions.create.call_count == 1
    receipt = db.read_context_input(agent.session_id, source=agent.platform or "agent", event_id="delivery1")
    assert receipt.sequence == 1
    users = [r for r in db.get_messages(agent.session_id) if r["role"] == "user"]
    assert len(users) == 1 and users[0]["content"] == "Clean request"
    assert "API-only note" in users[0]["api_content"]


def test_real_turn_accepts_before_failed_lease_and_next_turn_drains(durable_agent):
    agent, db = durable_agent
    with patch.object(db, "acquire_session_turn_lease", return_value=False):
        result = agent.run_conversation("First queued", persist_user_event_id="delivery1")
    assert result["api_calls"] == 0
    assert db.read_context_input(agent.session_id, source=agent.platform or "agent", event_id="delivery1")
    assert db.get_messages(agent.session_id) == []
    agent.client.chat.completions.create.return_value = _mock_response(content="Both handled", finish_reason="stop")
    with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        result = agent.run_conversation("Second queued", persist_user_event_id="delivery2")
    assert result.get("error") is None, result
    assert [r["content"] for r in db.get_messages(agent.session_id) if r["role"] == "user"] == ["First queued", "Second queued"]
    assert not db.read_context_rebase_snapshot(agent.session_id).has_pending_inputs


def test_native_accept_lost_ack_reconciles_same_receipt(db, monkeypatch):
    original = db._execute_write
    def lose_ack(fn, *args, **kwargs):
        original(fn, *args, **kwargs)
        raise RuntimeError("lost acknowledgement")
    with monkeypatch.context() as m:
        m.setattr(db, "_execute_write", lose_ack)
        with pytest.raises(RuntimeError, match="lost acknowledgement"):
            accept(db)
    assert accept(db).sequence == 1


def test_tui_busy_ack_is_durable_and_preserves_distinct_occurrences(db, monkeypatch):
    from types import SimpleNamespace
    from tui_gateway import server
    from tests.tui_gateway.test_prompt_recovery_contract import _session
    session = _session(agent=SimpleNamespace(_session_db=db, session_id="s", platform="cli",
        context_rebase_enabled=True), session_key="s", running=True)
    session["inflight_turn"] = {"user": "Identical words"}
    monkeypatch.setattr(server, "_sess_nowait", lambda *a: (session, None))
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a: None)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: False)
    monkeypatch.setattr(server, "_voice_mode_enabled", lambda: False)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *a: None)
    monkeypatch.setattr(server, "current_transport", lambda: None)
    handler = server._methods["prompt.submit"]
    for event_id in ("one", "one", "two"):
        result = handler("rpc", {"session_id": "s", "text": "Identical words", "input_event_id": event_id})
        assert result.get("error") is None, result
        assert result["result"] == {"status": "queued", "input_event_id": event_id}
        assert db.read_context_input("s", source="cli", event_id=event_id) is not None
    entries = [session["queued_prompt"], *session["queued_prompts"]]
    assert [entry["context_input_event_id"] for entry in entries] == ["one", "two"]
    assert [entry["text"] for entry in entries] == ["Identical words"] * 2
    assert db.read_context_rebase_snapshot("s").has_pending_inputs


def test_concurrent_acceptance_has_one_root_order_and_bounded_queue(db):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda n: accept(db, event="event" + str(n)), [*range(16), *range(16)]))
    unique = {receipt.event_id: receipt.sequence for receipt in receipts}
    assert sorted(unique.values()) == list(range(1, 17))
    for receipt in receipts:
        assert unique[receipt.event_id] == receipt.sequence
    for number in range(16, 128):
        accept(db, event="event" + str(number))
    with pytest.raises(ContextContinuationError, match="PENDING_LIMIT"):
        accept(db, event="excess")
    assert accept(db, event="event0").event_id == "event0"


@pytest.mark.parametrize("refusal", ["watch", "confirm", "busy_confirm"])
def test_tui_rejected_submit_never_enters_durable_inbox(db, monkeypatch, refusal):
    from types import SimpleNamespace
    from tui_gateway import server
    from tests.tui_gateway.test_prompt_recovery_contract import _session

    session = _session(agent=SimpleNamespace(_session_db=db, session_id="s", platform="cli",
        context_rebase_enabled=True), session_key="s", running=refusal == "busy_confirm")
    session["lazy"] = refusal == "watch"
    monkeypatch.setattr(server, "_sess_nowait", lambda *a: (session, None))
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a: None)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: False)
    monkeypatch.setattr(server, "_voice_mode_enabled", lambda: False)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *a: None)
    monkeypatch.setattr(server, "_child_run_active", lambda *a: True)
    monkeypatch.setattr(server, "current_transport", lambda: None)
    params = {"session_id": "s", "text": "Rejected input", "input_event_id": "rejected"}
    if refusal != "watch":
        params["confirm_truncate"] = True
    result = server._methods["prompt.submit"]("rpc", params)
    assert result["error"]["code"] == (4009 if refusal == "watch" else 4004)
    assert db.read_context_input("s", source="cli", event_id="rejected") is None
    assert db.read_pending_context_inputs("s") == ()
    assert not session.get("queued_prompt")


def test_native_projection_refuses_closed_alias_and_tip_uses_bound_fork_marker(db):
    from hermes_state import CompressionSessionClosedError
    receipt = accept(db)
    db.create_session("child", source="cli", parent_session_id="s", model_config={"_delegate_from": "different-parent"})
    db.end_session("s", end_reason="compression")
    assert db.get_context_continuation_tip("s") == "child"
    with pytest.raises(CompressionSessionClosedError):
        project(db, receipt)
    assert db.append_messages_batch("child", [row(receipt)], turn_lease_holder="holder") == 1


def test_projecting_pre_stop_input_does_not_resume_execution(db):
    receipt = accept(db)
    db.record_context_stop("s")
    project(db, receipt)
    snapshot = db.read_context_rebase_snapshot("s")
    assert snapshot.dispatch_stopped
    with pytest.raises(ContextContinuationError, match="STOPPED"):
        db.admit_context_dispatch("s", turn_lease_holder="holder", attempt_id="stopped",
            expected_snapshot_digest=snapshot.digest, payload_digest="sha256:" + "b" * 64, route_ref="test")
    later = accept(db, event="after-stop", content="Continue with my correction")
    project(db, later)
    snapshot = db.read_context_rebase_snapshot("s")
    assert not snapshot.dispatch_stopped
    assert db.admit_context_dispatch("s", turn_lease_holder="holder", attempt_id="resumed",
        expected_snapshot_digest=snapshot.digest, payload_digest="sha256:" + "b" * 64, route_ref="test")


@pytest.mark.parametrize("collision", [False, True])
def test_deferred_tui_input_uses_target_profile_config_and_database(tmp_path, monkeypatch, collision):
    from tui_gateway import server
    profile = tmp_path / "target-profile"
    profile.mkdir()
    (profile / "config.yaml").write_text("compression:\n  context_rebase_enabled: true\n")
    launch = SessionDB(tmp_path / "launch.db")
    target = SessionDB(profile / "state.db")
    if collision:
        launch.create_session("same-id", source="tui", profile_name="launch")
    target.create_session("same-id", source="tui", profile_name="target-profile")
    session = {"agent": None, "session_key": "same-id", "profile_home": str(profile), "source": "tui"}
    monkeypatch.setattr(server, "_get_db", lambda: launch)
    monkeypatch.setattr(server, "_resolve_model", lambda: "test-model")
    monkeypatch.setattr(server, "_persisted_session_cwd", lambda _: None)
    monkeypatch.setattr(server, "_apply_managed", lambda cfg: cfg)
    try:
        receipt = server._accept_tui_context_input(session, "Profile-owned input", event_id="event")
        assert receipt is not None and receipt.profile_name == "target-profile"
        assert target.read_context_input("same-id", source="tui", event_id="event") == receipt
        assert launch._conn.execute("SELECT COUNT(*) FROM state_meta WHERE key LIKE 'context-inbox:%'").fetchone()[0] == 0
    finally:
        launch.close()
        target.close()


def test_parked_successor_input_projects_once_across_retry_and_recovery(agent, tmp_path):
    from ares_runtime.continuity.runtime import AutomaticRebaseError
    from tests.ares_runtime.test_context_rebase_state import _publish
    db = SessionDB(tmp_path / "parked.db")
    try:
        db.create_session("s0", source="cli", profile_name="p1", model="test")
        db.append_message("s0", "user", "Original requirement")
        assert db.try_acquire_session_turn_lease("s0", "holder", ttl_seconds=300)
        transition = _publish(db)
        db.release_session_turn_lease("s0", "holder")
        agent._session_db, agent.session_id, agent._session_db_created = db, "s1", True
        agent._cached_system_prompt = "trusted base prompt"
        agent.compression_enabled = False
        agent.max_iterations, agent.max_tokens = 1, 1000
        with patch.object(agent.context_compressor, "on_session_start", side_effect=RuntimeError("unavailable"), create=True):
            with pytest.raises(AutomaticRebaseError, match="CONTEXT_ENGINE_REBIND_FAILED"):
                agent.run_conversation("Park this correction", persist_user_event_id="parked")
            # A native unfinished phase with no admitted request may retry
            # its owner recovery without creating a second user occurrence.
            with pytest.raises(AutomaticRebaseError, match="CONTEXT_ENGINE_REBIND_FAILED"):
                agent.run_conversation("Park this correction", persist_user_event_id="parked")
        agent.client.chat.completions.create.assert_not_called()
        agent.client.chat.completions.create.return_value = _mock_response(content="Continued", finish_reason="stop")
        with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
            result = agent.run_conversation("Continue with both requirements", persist_user_event_id="later")
        assert result.get("error") is None, result
        assert db.read_context_rebase_transition(transition.transition_id).state == "ready"
        assert [r["content"] for r in db.get_messages("s1")].count("Park this correction") == 1
        assert not db.read_context_rebase_snapshot("s1").has_pending_inputs
    finally:
        db.close()


def test_real_tui_reference_expansion_keeps_original_receipt_and_api_sidecar(durable_agent, tmp_path, monkeypatch):
    from tui_gateway import server
    from tests.tui_gateway.test_prompt_recovery_contract import _session
    agent, db = durable_agent
    agent.platform = "tui"
    (tmp_path / "note.txt").write_text("File evidence in the expanded request")
    text = "Read @file:note.txt"
    receipt = db.accept_context_input(agent.session_id, source="tui", event_id="ref", content=text)
    session = _session(agent=agent, session_key=agent.session_id, running=True)
    events = []
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: events.append((event, payload)))
    for name in ("_wire_callbacks", "_sync_agent_model_with_config", "_sync_agent_compression_with_config",
                 "_register_session_cwd", "_sync_session_key_after_compress", "_tts_stream_begin"):
        monkeypatch.setattr(server, name, lambda *a, **k: None)
    monkeypatch.setattr(server, "_session_cwd", lambda _: str(tmp_path))
    monkeypatch.setattr(server, "_get_usage", lambda _: {})
    monkeypatch.setattr(server, "_load_interim_assistant_messages", lambda: False)
    monkeypatch.setattr(server, "_voice_tts_enabled", lambda: False)
    monkeypatch.setattr(server, "_session_info", lambda *a, **k: {})
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *a, **k: None)
    agent.client.chat.completions.create.return_value = _mock_response(content="Read the evidence", finish_reason="stop")
    with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        server._run_prompt_submit("rpc", "ui", session, text, context_input_event_id="ref")
        session["_run_thread"].join(timeout=20)
        assert not session["_run_thread"].is_alive()
    assert agent.client.chat.completions.create.call_count == 1, events
    sent = agent.client.chat.completions.create.call_args.kwargs["messages"]
    assert "File evidence in the expanded request" in json.dumps(sent)
    users = [r for r in db.get_messages(agent.session_id) if r["role"] == "user"]
    assert len(users) == 1 and users[0]["content"] == receipt.content == text
    assert "File evidence in the expanded request" in users[0]["api_content"]
    assert not db.read_context_rebase_snapshot(agent.session_id).has_pending_inputs
