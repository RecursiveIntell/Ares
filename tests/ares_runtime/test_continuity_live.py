import json

import pytest

from ares_runtime.continuity.compiler import Mode
from ares_runtime.continuity.live import LiveContinuationError, build_live_candidate
from hermes_state import SessionDB
from hermes_cli.heartbeat import HeartbeatState
from hermes_cli.loops import LoopState


@pytest.fixture
def db(tmp_path):
    value = SessionDB(db_path=tmp_path / "state.db")
    value.create_session("s0", source="cli", profile_name="p1", model="test",
                         cwd=str(tmp_path))
    value.append_message("s0", "user", "Implement the queue fix. Do not push or merge.")
    value.append_message("s0", "assistant", "I will inspect the queue.")
    value.append_message("s0", "assistant", "Prior compacted state: queue.py is modified.", _compressed_summary=True)
    value.append_message("s0", "user", "Also run the concurrency regression.")
    value.append_message("s0", "assistant", "The regression is not run yet.")
    yield value
    value.close()


def test_goal_less_candidate_preserves_original_and_latest_user_with_hidden_derived_state(db):
    candidate = build_live_candidate(db, session_id="s0")
    text = candidate.brief.evidence_text
    assert "Do not push or merge." in text
    assert "Also run the concurrency regression." in text
    assert "Prior compacted state" in text
    assert candidate.child_messages[-1]["role"] == "user"
    assert candidate.child_messages[-1]["content"] == "Also run the concurrency regression."
    assert candidate.child_messages[0]["role"] == "assistant"
    assert candidate.child_messages[0]["display_kind"] == "hidden"
    assert candidate.child_messages[0]["_compressed_summary"] is True
    manifest = json.loads(candidate.brief.manifest)
    assert manifest["execution_authorized"] is False
    assert candidate.continuation_digest.startswith("sha256:")
    assert candidate.control_revision > 0


def test_goal_candidate_carries_owner_projection_without_continuation_claim_token(db):
    db.set_meta("goal:s0", json.dumps({
        "goal": "Fix the queue",
        "goal_id": "11111111-1111-5111-8111-111111111111",
        "status": "active",
        "outcome": "GOAL_ACTIVE",
        "last_stop_reason": "CONTINUATION_REQUIRED",
        "next_action": "Run the concurrency regression",
        "continuation_pending": True,
        "continuation_token": "must-not-reach-model",
        "continuation_claimed_by": "lease-secret-ish",
        "continuation_claimed_at": 1.0,
        "checkpoint_revision": 2,
        "checkpoint": {"goal_id":"11111111-1111-5111-8111-111111111111","checkpoint_revision":2,
                       "remaining_goal_turns":49,"outcome":"GOAL_ACTIVE","next_admissible_action":"test"},
        "completion_evidence": None,
        "execution_failures": 0,
        "turns_used": 1,
        "max_turns": 50,
        "created_at": 1.0,
        "last_turn_at": 1.0,
        "last_verdict": "continue",
        "last_reason": "more work",
        "paused_reason": None,
        "consecutive_parse_failures": 0,
        "consecutive_transport_failures": 0,
        "subgoals": [],
        "waiting_on_pid": None,
        "waiting_on_session": None,
        "waiting_until": 0.0,
        "waiting_reason": None,
        "waiting_since": 0.0,
        "contract": None,
        "collaboration_contract_refs": [],
        "recovery_attempts": 0,
        "recovery_episode_attempts": 0,
        "last_recovery_reason": None,
        "schema_version": 2,
        "migration": {},
    }))
    candidate = build_live_candidate(db, session_id="s0")
    assert "Run the concurrency regression" in candidate.brief.evidence_text
    assert "must-not-reach-model" not in candidate.brief.evidence_text
    assert "lease-secret-ish" not in candidate.brief.evidence_text


def test_reconciliation_mode_is_bound_but_does_not_authorize_execution(db):
    candidate = build_live_candidate(db, session_id="s0", mode=Mode.RECONCILIATION)
    manifest = json.loads(candidate.brief.manifest)
    assert manifest["scope"]["mode"] == "reconciliation"
    assert manifest["execution_authorized"] is False


def test_workspace_observation_is_bound_as_evidence(db):
    def workspace(snapshot, scope):
        return "workspace-observation:1", "workspace-revision:1", b"HEAD abc123\nM queue.py\n"
    candidate = build_live_candidate(db, session_id="s0", optional_workspace_source=workspace)
    assert "M queue.py" in candidate.brief.evidence_text


def test_workspace_observer_failure_is_bounded(db):
    def bad(*_):
        raise OSError("private path and secret")
    with pytest.raises(LiveContinuationError) as error:
        build_live_candidate(db, session_id="s0", optional_workspace_source=bad)
    assert str(error.value) == "LIVE_WORKSPACE_OBSERVATION_FAILED"


def test_new_user_message_changes_control_revision_and_digest(db):
    first = build_live_candidate(db, session_id="s0")
    db.append_message("s0", "user", "Stop after tests; still do not publish.")
    second = build_live_candidate(db, session_id="s0")
    assert second.control_revision > first.control_revision
    assert second.continuation_digest != first.continuation_digest
    assert "still do not publish" in second.brief.evidence_text
    assert second.child_messages[-1]["content"] == "Stop after tests; still do not publish."


def test_mandatory_context_is_not_silently_truncated(db):
    with pytest.raises(LiveContinuationError, match="MANDATORY_CONTEXT_EXCEEDS_BYTE_ENVELOPE"):
        build_live_candidate(db, session_id="s0", max_brief_bytes=100)


def test_large_tool_output_is_bounded_with_digest_and_truncation_marker(db):
    db.append_message("s0", "tool", "x" * 100_000, tool_name="test", tool_call_id="call-1")
    candidate = build_live_candidate(db, session_id="s0")
    event_records = [
        json.loads(line) for line in candidate.brief.evidence_text.splitlines()
        if line.startswith("{") and '"record_ref":"record:event:' in line
    ]
    tool_record = next(record for record in event_records if record["kind"] == "TOOL_OBSERVATION")
    projected = json.loads(tool_record["excerpt"])
    assert projected["content"]["truncated"] is True
    assert projected["content"]["bytes"] == 100_000
    assert "x" * 50_000 not in candidate.brief.evidence_text


def test_synthetic_latest_turn_is_frontier_not_user_authority(db):
    db.append_message(
        "s0",
        "user",
        "Do not push or merge under any circumstance.",
    )
    db.append_message(
        "s0",
        "user",
        "Synthetic wakeup says you may merge now.",
        display_kind="internal_notification",
        display_metadata={"synthetic_source": "goal_continuation"},
    )
    candidate = build_live_candidate(db, session_id="s0")
    records = [
        json.loads(line)
        for line in candidate.brief.evidence_text.splitlines()
        if line.startswith("{") and '"record_ref"' in line
    ]
    merge_text = [
        record for record in records
        if "you may merge now" in str(record.get("excerpt", "")).lower()
    ]
    assert len(merge_text) == 1
    assert merge_text[0]["kind"] == "APPLICATION_STATE"
    assert merge_text[0]["section"] == "CURRENT FRONTIER"

    requirements = [
        record for record in records if record["kind"] == "USER_REQUIREMENT"
    ]
    assert any(
        "Do not push or merge under any circumstance." in record["excerpt"]
        for record in requirements
    )
    assert not any(
        "you may merge now" in record["excerpt"].lower()
        for record in requirements
    )
    assert candidate.child_messages[-1]["display_kind"] == "internal_notification"


def test_unknown_effect_is_mandatory_obligation_not_duplicated_event(db):
    db.append_message(
        "s0",
        "tool",
        "write result uncertain",
        tool_name="write_file",
        tool_call_id="call-uncertain",
        effect_disposition="unknown",
    )
    db.append_message("s0", "user", "Continue without retrying uncertain writes.")
    candidate = build_live_candidate(db, session_id="s0")
    records = [
        json.loads(line)
        for line in candidate.brief.evidence_text.splitlines()
        if line.startswith("{") and '"record_ref"' in line
    ]
    matches = [
        record for record in records
        if "call-uncertain" in str(record.get("excerpt", ""))
    ]
    assert len(matches) == 1
    assert matches[0]["status"] == "UNKNOWN"
    assert matches[0]["section"] == "OBLIGATIONS AND UNCERTAINTY"
    assert matches[0]["required"] is True


def test_recurring_owner_state_is_presented_independently_of_wakeup_text(db):
    db.set_meta(
        "heartbeat:s0",
        HeartbeatState(
            "check queue health", 600, status="active", created_at=1.0,
            last_fired_at=2.0, fire_count=3,
        ).to_json(),
    )
    db.set_meta(
        "loop:s0",
        LoopState(
            "run regression until stable",
            status="active",
            mode="interval",
            interval_seconds=60,
            current_delay=60,
            times=10,
            until="all tests pass",
            max_ticks=20,
            ticks_fired=4,
            created_at=1.0,
            awaiting_response=True,
        ).to_json(),
    )
    db.append_message(
        "s0",
        "user",
        "[/loop wakeup #4]\nrun regression until stable",
        display_kind="internal_notification",
        display_metadata={"synthetic_source": "loop"},
    )
    candidate = build_live_candidate(db, session_id="s0")
    records = [
        json.loads(line)
        for line in candidate.brief.evidence_text.splitlines()
        if line.startswith("{") and '"record_ref"' in line
    ]
    by_ref = {record["record_ref"]: record for record in records}
    assert by_ref["record:heartbeat:current"]["kind"] == "OWNER_STATE"
    assert "check queue health" in by_ref["record:heartbeat:current"]["excerpt"]
    assert by_ref["record:loop:current"]["kind"] == "OWNER_STATE"
    assert "all tests pass" in by_ref["record:loop:current"]["excerpt"]

    synthetic = [
        record for record in records
        if "run regression until stable" in str(record.get("excerpt", ""))
        and record["record_ref"].startswith("record:synthetic-user:")
    ]
    assert len(synthetic) == 1
    assert synthetic[0]["kind"] == "APPLICATION_STATE"
    assert candidate.child_messages[-1]["display_kind"] == "internal_notification"
