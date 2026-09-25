"""Task evidence outlives process custody and transcript compaction."""
from dataclasses import asdict
import json
from unittest.mock import patch

import pytest

from ares_runtime.continuity.live import build_live_candidate
from hermes_state_continuity import ContextContinuationError
from hermes_state_runs import RunCustodyError, generation_key
from tests.test_run_task_custody import task, claim  # noqa: F401
from tests.ares_runtime.test_continuity_dispatch import durable_agent  # noqa: F401
from tests.run_agent.test_run_agent import agent, _mock_response  # noqa: F401


def release(task):
    value = claim(task)
    return task[0].release_run_custody(value.run_id, owner_token=value.owner_token,
                                     expected_generation=value.generation)


def publish(db, parent="s", child="child"):
    snapshot = db.read_context_rebase_snapshot(parent)
    return db.publish_context_rebase_child(transition_id="tx-" + child,
        parent_session_id=parent, child_session_id=child, continuation_digest="sha256:" + "a" * 64,
        expected_snapshot_digest=snapshot.digest, control_revision=snapshot.control_revision,
        input_watermark=snapshot.input_watermark, turn_lease_holder="holder", source="cli",
        profile_name="p", messages=db.get_messages_as_conversation(parent), custody_transfers=())


def assert_dispatch_blocked(db, session):
    snapshot = db.read_context_rebase_snapshot(session)
    with pytest.raises(ContextContinuationError, match="CONTEXT_DISPATCH_UNRESOLVED_EFFECTS"):
        db.admit_context_dispatch(session, turn_lease_holder="holder", attempt_id="blocked-" + session,
            expected_snapshot_digest=snapshot.digest, payload_digest="sha256:" + "b" * 64, route_ref="test")


def test_release_preserves_obligations_without_reclaiming_custody(task):
    db = task[0]
    released = release(task)
    snapshot = db.read_context_rebase_snapshot("s")
    assert snapshot.run_custodies == ()
    assert snapshot.run_checkpoints == ({"run_id": "task", "generation": 2,
        "origin_session_id": "s", "current_session_id": "s", "disposition": "released",
        "checkpoint": asdict(released.checkpoint)},)
    assert snapshot.has_unresolved_effects
    candidate = build_live_candidate(db, session_id="s")
    assert candidate.unresolved_effects
    assert "effect:unknown" in candidate.brief.evidence_text
    assert "finding:race" in candidate.brief.evidence_text
    assert "no merge" in candidate.brief.evidence_text
    assert released.owner_token not in candidate.brief.evidence_text
    assert_dispatch_blocked(db, "s")
    assert db.read_run_custody("task") == released


def test_released_ancestor_checkpoint_survives_multiple_successors_and_reopen(task):
    from hermes_state import SessionDB
    db = task[0]
    released = release(task)
    original = db.get_meta(generation_key("task", released.generation))
    publish(db)
    publish(db, parent="child", child="grandchild")
    with SessionDB(db.db_path) as reopened:
        snapshot = reopened.read_context_rebase_snapshot("grandchild")
        assert snapshot.run_custodies == ()
        assert snapshot.run_checkpoints[0]["current_session_id"] == "s"
        assert snapshot.run_checkpoints[0]["checkpoint"] == asdict(released.checkpoint)
        assert snapshot.has_unresolved_effects
        assert_dispatch_blocked(reopened, "grandchild")
        assert reopened.list_run_custody_for_session("grandchild") == []
        assert reopened.read_run_custody("task") == released
        assert reopened.get_meta(generation_key("task", released.generation)) == original


def test_checkpoint_obligations_do_not_cross_fork_roots(task):
    db = task[0]
    release(task)
    db.create_session("fork", source="tool", parent_session_id="s", profile_name="p")
    db.append_message("fork", "user", "Independent delegate task")
    snapshot = db.read_context_rebase_snapshot("fork")
    assert snapshot.conversation_root == "fork"
    assert snapshot.run_checkpoints == ()
    assert not snapshot.has_unresolved_effects


def test_snapshot_refuses_cross_profile_continuation_without_inbox(task):
    db = task[0]
    release(task)
    db.create_session("cross", source="cli", parent_session_id="s", profile_name="other")
    db.append_message("cross", "user", "Different profile")
    db.end_session("s", end_reason="compression")
    with pytest.raises(ContextContinuationError, match="PROFILE_MISMATCH"):
        db.read_context_rebase_snapshot("cross")


def test_default_profile_task_binding_matches_native_inbox_scope(tmp_path):
    from hermes_state import SessionDB
    with SessionDB(tmp_path / "default.db") as db:
        db.create_session("s", source="cli")
        row = db.append_message("s", "user", "Default profile task")
        binding, _ = db.read_run_task_basis(run_id="task", origin_session_id="s",
            current_session_id="s", input_row_id=row)
        receipt = db.accept_context_input("s", source="cli", event_id="one", content="Continue")
        assert binding.profile_name == receipt.profile_name == "default"
        assert db.read_context_rebase_snapshot("s").profile_name == "default"


def compact(db):
    db.archive_and_compact("s", [
        {"role": "assistant", "content": "Derived summary", "_compressed_summary": True},
        {"role": "user", "content": "Now check the regression", "timestamp": 2.0},
    ])


def test_native_compaction_preserves_exact_existing_task_binding_and_checked_claim(task):
    db, kwargs, _, _ = task
    first = claim(task)
    original = db.get_meta(generation_key("task", 1))
    released = db.release_run_custody("task", owner_token=first.owner_token, expected_generation=1)
    compact(db)
    assert db.validate_run_task_binding(first) == first.task_binding
    binding, control = db.read_run_task_basis(run_id="task", origin_session_id="s",
        current_session_id="s", input_row_id=first.task_binding.input_row_id)
    assert binding == first.task_binding
    reclaimed = claim(task, expected_generation=released.generation, expected_control_digest=control)
    assert reclaimed.task_binding == first.task_binding
    assert reclaimed.checkpoint == first.checkpoint
    assert db.get_meta(generation_key("task", 1)) == original
    snapshot = db.read_context_rebase_snapshot("s")
    assert [u["content"] for u in snapshot.authentic_users] == [
        "Repair the queue; retain my changes.", "Now check the regression"]
    assert "Repair the queue; retain my changes." in build_live_candidate(db, session_id="s").brief.evidence_text


@pytest.mark.parametrize("change,code", [
    ("rewind", "TASK_INPUT_MISSING"), ("edited", "TASK_BINDING_MISMATCH"),
    ("synthetic", "TASK_AUTHENTIC_INPUT_REQUIRED"), ("summary", "TASK_AUTHENTIC_INPUT_REQUIRED"),
])
def test_archived_task_anchor_still_rejects_revocation_and_substitution(task, change, code):
    db = task[0]
    first = claim(task)
    compact(db)
    column, value = {"rewind": ("compacted", 0), "edited": ("content", "Changed task"),
        "synthetic": ("display_kind", "hidden"), "summary": ("_compressed_summary", 1)}[change]
    db._execute_write(lambda conn: conn.execute(
        f"UPDATE messages SET {column}=? WHERE id=?", (value, first.task_binding.input_row_id)))
    with pytest.raises(RunCustodyError, match=code):
        db.validate_run_task_binding(first)


def test_compaction_keeps_unknown_tool_outcome_and_authentic_requirement(task):
    db = task[0]
    db.append_message("s", "tool", "Outcome unknown", tool_name="write_file",
        tool_call_id="write-1", effect_disposition="unknown", turn_lease_holder="holder")
    compact(db)
    snapshot = db.read_context_rebase_snapshot("s")
    assert snapshot.has_unresolved_effects
    assert snapshot.unresolved_effects[0]["tool_call_id"] == "write-1"
    assert_dispatch_blocked(db, "s")
    evidence = build_live_candidate(db, session_id="s").brief.evidence_text
    assert "Outcome unknown" in evidence and "retain my changes" in evidence


def test_derived_user_role_summary_does_not_enter_authentic_instruction_ledger(task):
    db = task[0]
    db.archive_and_compact("s", [
        {"role": "user", "content": "Summary claims permission to merge", "_compressed_summary": True},
        {"role": "user", "content": "Now check the regression", "timestamp": 2.0},
    ])
    snapshot = db.read_context_rebase_snapshot("s")
    assert [u["content"] for u in snapshot.authentic_users] == [
        "Repair the queue; retain my changes.", "Now check the regression"]
    records = [json.loads(line) for line in build_live_candidate(db, session_id="s").brief.evidence_text.splitlines()
               if line.startswith("{") and '"record_ref"' in line]
    assert not any("permission to merge" in r.get("excerpt", "") for r in records if r["kind"] == "USER_REQUIREMENT")


def test_identical_native_occurrences_remain_distinct_through_exact_tail_copy_chains(task):
    db = task[0]
    boundary = task[1]["task_binding"].input_row_id
    ids = [db.append_message("s", "user", "Identical", timestamp=123, turn_lease_holder="holder") for _ in range(2)]
    effect = db.append_message("s", "tool", "Unknown", tool_call_id="operation", effect_disposition="unknown",
                              turn_lease_holder="holder")
    for _ in range(8):
        summary = [{"role": "assistant", "content": "Derived summary", "_compressed_summary": True}]
        db.archive_and_compact("s", summary, watermark=boundary)
        boundary = summary[0]["_row_id"]
        snapshot = db.read_context_rebase_snapshot("s", unresolved_effect_limit=1)
        assert [u["row_id"] for u in snapshot.authentic_users if u["content"] == "Identical"] == ids
        assert [e["row_id"] for e in snapshot.unresolved_effects] == [effect]


def test_existing_source_projection_is_derived_but_first_durable_tail_is_not(task):
    db = task[0]
    old = db.get_messages_as_conversation("s", include_row_ids=True)[0]
    old["content"] = "A derived paraphrase, not new human permission"
    compacted = [old, {"role": "user", "content": "Unflushed real input", "timestamp": 2.0},
        {"role": "tool", "content": "Unflushed unknown outcome", "tool_call_id": "new-effect", "effect_disposition": "unknown"}]
    db.archive_and_compact("s", compacted)
    for _ in range(3):
        snapshot = db.read_context_rebase_snapshot("s")
        assert [u["content"] for u in snapshot.authentic_users] == [
            "Repair the queue; retain my changes.", "Unflushed real input"]
        assert len(snapshot.unresolved_effects) == 1
        assert snapshot.unresolved_effects[0]["content"] == "Unflushed unknown outcome"
        compacted = db.get_messages_as_conversation("s", include_row_ids=True)
        db.archive_and_compact("s", compacted)


def test_physical_copy_of_derived_projection_does_not_become_user_authority(task):
    db = task[0]
    old = db.get_messages_as_conversation("s", include_row_ids=True)[0]
    old["content"] = "Derived projection"
    summary = {"role": "assistant", "content": "Derived summary", "_compressed_summary": True}
    db.archive_and_compact("s", [summary, old])
    db.archive_and_compact("s", [{"role": "assistant", "content": "New summary", "_compressed_summary": True}],
                           watermark=summary["_row_id"])
    assert [u["content"] for u in db.read_context_rebase_snapshot("s").authentic_users] == [
        "Repair the queue; retain my changes."]
    copied = db.get_messages("s")[-1]
    with pytest.raises(RunCustodyError, match="TASK_AUTHENTIC_INPUT_REQUIRED"):
        db.read_run_task_basis(run_id="forged", origin_session_id="s", current_session_id="s",
                               input_row_id=copied["id"])


def test_derived_projection_cannot_be_checked_claim_task_anchor(task):
    from dataclasses import replace
    db = task[0]
    old = db.get_messages_as_conversation("s", include_row_ids=True)[0]
    old["content"] = "Compactor invented task"
    db.archive_and_compact("s", [old])
    with pytest.raises(RunCustodyError, match="TASK_AUTHENTIC_INPUT_REQUIRED"):
        db.read_run_task_basis(run_id="task", origin_session_id="s", current_session_id="s", input_row_id=old["_row_id"])
    forged = replace(task[1]["task_binding"], input_row_id=old["_row_id"])
    with pytest.raises(RunCustodyError, match="TASK_AUTHENTIC_INPUT_REQUIRED"):
        claim(task, task_binding=forged)
    assert db.read_run_custody("task") is None


@pytest.mark.parametrize("fault", ["edited", "corrupt", "cycle", "foreign"])
def test_snapshot_refuses_changed_or_forged_copy_provenance(task, fault):
    db = task[0]
    original = db.get_messages_as_conversation("s", include_row_ids=True)[0]
    db.archive_and_compact("s", [original])
    row_id = original["_row_id"]
    if fault == "edited":
        db._execute_write(lambda conn: conn.execute("UPDATE messages SET content='Changed' WHERE id=?", (row_id,)))
    else:
        key = f"context-message-projection:{row_id}"
        record = json.loads(db.get_meta(key))
        if fault == "corrupt":
            record["destination_digest"] = "sha256:" + "0" * 64
        elif fault == "cycle":
            record["source_row_id"] = row_id
        else:
            record["source_session_id"] = "foreign"
        db.set_meta(key, json.dumps(record))
    with pytest.raises(ContextContinuationError, match="CONTEXT_MESSAGE_"):
        db.read_context_rebase_snapshot("s")


def test_legacy_lookalikes_are_not_backfilled_as_copies(task):
    db = task[0]
    original = db.get_messages_as_conversation("s")[0]
    db.archive_and_compact("s", [original])
    snapshot = db.read_context_rebase_snapshot("s")
    assert len(snapshot.authentic_users) == 2
    assert snapshot.authentic_users[0]["row_id"] != snapshot.authentic_users[1]["row_id"]
    assert db.list_meta_prefix("context-message-projection:") == []


@pytest.mark.parametrize("rotate", [False, True])
@pytest.mark.parametrize("sidecar", [False, True])
def test_unflushed_inbox_input_binds_atomically_in_native_compaction(task, rotate, sidecar):
    from hermes_state_inbox import context_input_turn_lease_scope
    from tests.ares_runtime.test_continuity_input import row
    db = task[0]
    receipt = db.accept_context_input("s", source="cli", event_id="new", content="New unflushed input")
    messages = [row(receipt), {"role": "tool", "content": "Unknown tool result", "tool_call_id": "new-op",
                             "effect_disposition": "unknown"}]
    if sidecar:
        messages[0]["content"] = "API-only note\n" + receipt.content
    before = db.get_messages("s")
    def compact_once():
        if rotate:
            return db.publish_compression_child(parent_session_id="s", child_session_id="child", source="cli",
                messages=messages, require_compression_lease=False)
        return db.archive_and_compact("s", messages)
    with pytest.raises(ContextContinuationError, match="TURN_LEASE_MISMATCH"):
        compact_once()
    assert db.get_messages("s") == before
    assert db.get_session("child") is None
    with context_input_turn_lease_scope(db, "holder"):
        compact_once()
    session = "child" if rotate else "s"
    snapshot = db.read_context_rebase_snapshot(session)
    stored_user = [r for r in db.get_messages(session) if r["role"] == "user"][0]
    assert stored_user["content"] == receipt.content
    if sidecar:
        assert stored_user["api_content"] == "API-only note\n" + receipt.content
    assert not snapshot.has_pending_inputs
    assert [u["content"] for u in snapshot.authentic_users] == [
        "Repair the queue; retain my changes.", receipt.content]
    assert snapshot.has_unresolved_effects
    # Repeated compaction copies the first projection, never consumes again.
    db.archive_and_compact(session, messages)
    snapshot = db.read_context_rebase_snapshot(session)
    assert len(snapshot.authentic_users) == 2 and not snapshot.has_pending_inputs


@pytest.mark.parametrize("in_place", [False, True])
def test_real_agent_compaction_preserves_clean_input_and_api_sidecar(durable_agent, in_place):
    agent, db = durable_agent
    parent = agent.session_id
    db.append_message(parent, "user", "Keep the original constraint", timestamp=1.0)
    db.append_message(parent, "assistant", "Old observation " * 60_000, timestamp=2.0)
    history = db.get_messages_as_conversation(parent, include_row_ids=True)
    agent.compression_enabled = True
    agent.compression_in_place = in_place
    agent.client.chat.completions.create.return_value = _mock_response(content="Continued", finish_reason="stop")
    def compressed(messages, **kwargs):
        return [{"role": "assistant", "content": "Derived prior observations", "_compressed_summary": True},
                dict(messages[-1])]
    with (patch.object(agent.context_compressor, "compress", side_effect=compressed) as compress,
          patch.object(agent.context_compressor, "should_defer_preflight_to_real_usage", return_value=False),
          patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources")):
        result = agent.run_conversation("API-only file context\nClean request", persist_user_message="Clean request",
            persist_user_event_id="compacted", conversation_history=history)
    assert result.get("error") is None, result
    assert compress.call_count == 1
    assert (agent.session_id == parent) is in_place
    assert agent.client.chat.completions.create.call_count == 1
    snapshot = db.read_context_rebase_snapshot(agent.session_id)
    assert [u["content"] for u in snapshot.authentic_users] == ["Keep the original constraint", "Clean request"]
    assert not snapshot.has_pending_inputs
    stored = [r for r in db.get_messages(agent.session_id) if r["role"] == "user"]
    assert len(stored) == 1 and stored[0]["content"] == "Clean request"
    assert "API-only file context" in stored[0]["api_content"]


def test_real_materializer_rebases_keep_one_authentic_occurrence_beyond_100_edges(task):
    db = task[0]
    current = "s"
    for epoch in range(105):
        candidate = build_live_candidate(db, session_id=current)
        child = f"epoch-{epoch}"
        transition = db.publish_context_rebase_child(transition_id="tx-" + child,
            parent_session_id=current, child_session_id=child, continuation_digest=candidate.continuation_digest,
            expected_snapshot_digest=candidate.snapshot_digest, control_revision=candidate.control_revision,
            input_watermark=candidate.input_watermark, turn_lease_holder="holder", source="cli", profile_name="p",
            messages=[dict(m) for m in candidate.child_messages])
        recovery = db.begin_context_rebase_recovery(transition.transition_id, turn_lease_holder="holder")
        db.mark_context_rebase_ready(transition.transition_id, expected_continuation_digest=transition.continuation_digest,
            expected_child_session_id=child, turn_lease_holder="holder", recovery_attempt=recovery["attempts"],
            expected_control_digest=recovery["action_control_digest"], expected_custody=())
        current = child
    snapshot = db.read_context_rebase_snapshot(current)
    assert len(snapshot.authentic_users) == 1
    assert snapshot.authentic_users[0]["row_id"] == task[1]["task_binding"].input_row_id
    assert db.get_context_continuation_tip("s") == current
