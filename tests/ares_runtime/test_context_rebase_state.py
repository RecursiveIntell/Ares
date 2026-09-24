import hashlib
import json

import pytest

from hermes_state import SessionDB
from hermes_state_continuity import ContextContinuationError
from hermes_state_runs import RunCheckpoint, RunCustodyError
from plugins.context_engine._context_governor import ContextGovernorEngine


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _messages(label="next"):
    return [
        {"role": "assistant", "content": f"continuation brief {label}", "display_kind": "hidden"},
        {"role": "user", "content": "continue the exact task"},
    ]


@pytest.fixture
def db(tmp_path):
    value = SessionDB(db_path=tmp_path / "state.db")
    value.create_session("s0", source="cli", profile_name="p1", model="test")
    value.append_message("s0", "user", "original task")
    assert value.try_acquire_session_turn_lease("s0", "holder", ttl_seconds=300)
    yield value
    value.close()


def _publish(db, *, transition="tx1", parent="s0", child="s1", digest=None, watermark=None):
    if watermark is None:
        watermark = db.get_active_message_watermark(parent)
    return db.publish_context_rebase_child(
        transition_id=transition,
        parent_session_id=parent,
        child_session_id=child,
        continuation_digest=digest or ("sha256:" + "a" * 64),
        control_revision=7,
        input_watermark=watermark,
        turn_lease_holder="holder",
        source="cli",
        messages=_messages(child),
        model="test",
        model_config={"temperature": 0},
        system_prompt="trusted base prompt",
        profile_name="p1",
    )


def test_atomic_publication_creates_complete_pending_child_and_closes_parent(db):
    transition = _publish(db)
    assert transition.state == "committed_pending_activation"
    assert transition.context_epoch == 1
    parent = db.get_session("s0")
    child = db.get_session("s1")
    assert parent["end_reason"] == "context_rebase"
    assert child["ended_at"] is None
    assert child["parent_session_id"] == "s0"
    config = json.loads(child["model_config"])
    assert config["_context_rebase_from"] == "s0"
    assert config["_context_rebase_transition"] == "tx1"
    assert config["_context_epoch"] == 1
    assert [row["content"] for row in db.get_messages_as_conversation("s1")] == [
        "continuation brief s1", "continue the exact task"
    ]
    assert db.read_context_rebase_transition("tx1") == transition


def test_stale_watermark_fails_without_closing_parent_or_creating_child(db):
    watermark = db.get_active_message_watermark("s0")
    db.append_message("s0", "assistant", "new durable state")
    with pytest.raises(ContextContinuationError, match="CONTEXT_REBASE_STALE_INPUT"):
        _publish(db, watermark=watermark)
    assert db.get_session("s0")["ended_at"] is None
    assert db.get_session("s1") is None
    assert db.read_context_rebase_transition("tx1") is None


def test_turn_lease_holder_is_required_and_must_match(db):
    watermark = db.get_active_message_watermark("s0")
    with pytest.raises(ContextContinuationError, match="TURN_LEASE_MISMATCH"):
        db.publish_context_rebase_child(
            transition_id="tx1", parent_session_id="s0", child_session_id="s1",
            continuation_digest="sha256:" + "a" * 64, control_revision=7,
            input_watermark=watermark, turn_lease_holder="other", source="cli",
            messages=_messages(), model="test", profile_name="p1",
        )
    assert db.get_session("s0")["ended_at"] is None


def test_retry_same_transition_is_idempotent_but_changed_binding_refuses(db):
    first = _publish(db)
    second = _publish(db)
    assert second == first
    assert db.message_count("s1") == 2
    with pytest.raises(ContextContinuationError, match="CONTEXT_REBASE_IDEMPOTENCY_MISMATCH"):
        _publish(db, digest="sha256:" + "b" * 64)
    assert db.message_count("s1") == 2


def test_ready_requires_exact_child_and_continuation(db):
    _publish(db)
    with pytest.raises(ContextContinuationError, match="CONTEXT_REBASE_READY_BINDING_MISMATCH"):
        db.mark_context_rebase_ready("tx1", expected_continuation_digest="sha256:" + "b" * 64,
                                     expected_child_session_id="s1")
    ready = db.mark_context_rebase_ready("tx1", expected_continuation_digest="sha256:" + "a" * 64,
                                         expected_child_session_id="s1")
    assert ready.state == "ready" and ready.ready_at is not None
    assert db.mark_context_rebase_ready("tx1", expected_continuation_digest="sha256:" + "a" * 64,
                                        expected_child_session_id="s1") == ready


def test_resume_and_turn_lease_follow_rebase_tip(db):
    _publish(db)
    assert db.resolve_resume_session_id("s0") == "s1"
    assert db._session_turn_lease_key("s1") == db._session_turn_lease_key("s0")
    assert db.refresh_session_turn_lease("s1", "holder", ttl_seconds=300)
    assert not db.try_acquire_session_turn_lease("s1", "competing", ttl_seconds=300)


def test_explicit_branch_child_does_not_replace_rebase_tip(db):
    _publish(db)
    db.create_session("branch", source="cli", parent_session_id="s0",
                      model_config={"_branched_from": "s0"}, profile_name="p1")
    assert db.get_context_continuation_tip("s0") == "s1"


def test_ambiguous_marker_bound_rebase_children_fail_closed(db):
    _publish(db)
    db.create_session("evil", source="cli", parent_session_id="s0",
                      model_config={"_context_rebase_from": "s0", "_context_rebase_transition": "evil", "_context_epoch": 1},
                      profile_name="p1")
    with pytest.raises(ContextContinuationError, match="AMBIGUOUS_CONTEXT_REBASE"):
        db.get_context_continuation_tip("s0")
    with pytest.raises(ContextContinuationError, match="AMBIGUOUS_CONTEXT_REBASE"):
        db.resolve_resume_session_id("s0")


def test_depth_limit_returns_typed_error_not_stale_tip(db):
    _publish(db, transition="tx1", parent="s0", child="s1")
    # The same conversation lease spans the rebase edge.
    db.append_message("s1", "assistant", "more work")
    _publish(db, transition="tx2", parent="s1", child="s2")
    with pytest.raises(ContextContinuationError, match="CONTINUATION_DEPTH_LIMIT"):
        db.get_context_continuation_tip("s0", max_depth=1)
    assert db.get_context_continuation_tip("s0", max_depth=3) == "s2"
    assert json.loads(db.get_session("s2")["model_config"])["_context_epoch"] == 2


def test_custody_transfer_preserves_owner_checkpoint_and_historical_binding(db):
    goal = '{"goal":"same task","status":"active"}'
    db.set_meta("goal:history", goal)
    checkpoint = RunCheckpoint(
        _sha("plan"), _sha("contract"), _sha("source"), "continue",
        (("historical-goal-key", "goal:history"),), ("effect:unknown",), (), ("no publish",),
    )
    owner = db.claim_run_custody_checked(
        "run1", lease_holder="holder", expected_generation=0, checkpoint=checkpoint,
        origin_session_id="s0", current_session_id="s0", historical_goal_digest=_sha(goal),
    )
    _publish(db)
    moved = db.transfer_run_session(
        "run1", owner_token=owner.owner_token, expected_generation=owner.generation,
        expected_session_id="s0", new_session_id="s1", ttl_seconds=300,
    )
    assert moved.current_session_id == "s1"
    assert moved.origin_session_id == "s0"
    assert moved.owner_token == owner.owner_token
    assert moved.checkpoint == checkpoint
    assert moved.historical_goal_digest == owner.historical_goal_digest
    assert moved.generation == owner.generation + 1


def test_custody_transfer_refuses_unrelated_child(db):
    goal = '{"goal":"same task","status":"active"}'
    db.set_meta("goal:history", goal)
    checkpoint = RunCheckpoint(_sha("p"), _sha("c"), _sha("s"), "continue",
        (("historical-goal-key", "goal:history"),), (), (), ())
    owner = db.claim_run_custody_checked("run1", lease_holder="holder", expected_generation=0,
        checkpoint=checkpoint, origin_session_id="s0", current_session_id="s0",
        historical_goal_digest=_sha(goal))
    db.create_session("branch", source="cli", parent_session_id="s0",
                      model_config={"_branched_from":"s0"}, profile_name="p1")
    with pytest.raises(RunCustodyError, match="SESSION_TRANSITION_MISMATCH"):
        db.transfer_run_session("run1", owner_token=owner.owner_token,
            expected_generation=owner.generation, expected_session_id="s0",
            new_session_id="branch", ttl_seconds=300)
    assert db.read_run_custody("run1") == owner


def test_bounded_snapshot_carries_first_user_current_users_and_owner_state(db):
    db.append_message("s0", "assistant", "older summary", _compressed_summary=True)
    db.append_message("s0", "user", "new correction: still do not publish")
    db.append_message("s0", "assistant", "working on it")
    db.set_meta("goal:s0", '{"goal":"same task","status":"active"}')
    snapshot = db.read_context_rebase_snapshot("s0", recent_limit=4)
    assert snapshot.conversation_root == "s0"
    assert snapshot.first_user["content"] == "original task"
    assert [item["content"] for item in snapshot.current_users] == [
        "new correction: still do not publish"
    ]
    assert snapshot.latest_summary["content"] == "older summary"
    assert snapshot.goal_raw == '{"goal":"same task","status":"active"}'
    assert snapshot.input_watermark == db.get_active_message_watermark("s0")


def test_snapshot_refuses_too_many_user_changes_since_summary(db):
    db.append_message("s0", "assistant", "summary", _compressed_summary=True)
    for index in range(3):
        db.append_message("s0", "user", f"correction {index}")
    with pytest.raises(ContextContinuationError, match="TOO_MANY_UNSUMMARIZED_USER_CHANGES"):
        db.read_context_rebase_snapshot("s0", user_limit=2)


def test_snapshot_after_rebase_preserves_original_first_user(db):
    _publish(db)
    db.append_message("s1", "assistant", "summary child", _compressed_summary=True)
    db.append_message("s1", "user", "latest correction")
    snapshot = db.read_context_rebase_snapshot("s1")
    assert snapshot.first_user["content"] == "original task"
    assert snapshot.current_users[-1]["content"] == "latest correction"
    assert snapshot.conversation_root == "s0"


def test_governor_logical_lineage_survives_context_rebase(db):
    _publish(db)
    assert ContextGovernorEngine._context_lineage_root(db, "s1") == "s0"
    assert ContextGovernorEngine._compression_lineage_root(db, "s1") == "s0"


def test_governor_lineage_does_not_follow_fake_rebase_marker(db):
    db.create_session(
        "fake",
        source="cli",
        parent_session_id="s0",
        model_config={"_context_rebase_from": "wrong", "_context_epoch": 1},
        profile_name="p1",
    )
    assert ContextGovernorEngine._context_lineage_root(db, "fake") == "fake"


def test_post_publish_ambiguity_enters_reconciliation_required(db):
    transition = _publish(db)
    updated = db.mark_context_rebase_reconciliation_required(transition.transition_id)
    assert updated.state == "reconciliation_required"
    assert updated.ready_at is None
    assert db.mark_context_rebase_reconciliation_required(transition.transition_id) == updated
    with pytest.raises(ContextContinuationError, match="CONTEXT_REBASE_NOT_ACTIVATABLE"):
        db.mark_context_rebase_ready(
            transition.transition_id,
            expected_continuation_digest=transition.continuation_digest,
            expected_child_session_id=transition.child_session_id,
        )


def test_ready_rebase_cannot_be_demoted_to_reconciliation(db):
    transition = _publish(db)
    ready = db.mark_context_rebase_ready(
        transition.transition_id,
        expected_continuation_digest=transition.continuation_digest,
        expected_child_session_id=transition.child_session_id,
    )
    assert ready.state == "ready"
    with pytest.raises(ContextContinuationError, match="CONTEXT_REBASE_NOT_RECONCILABLE"):
        db.mark_context_rebase_reconciliation_required(transition.transition_id)


def test_pending_child_refuses_ordinary_turn_admission(db):
    transition = _publish(db)
    assert transition.state == "committed_pending_activation"
    with pytest.raises(ContextContinuationError, match="CONTEXT_REBASE_NOT_READY"):
        db.assert_context_rebase_ready_for_turn("s1")


def test_ready_child_allows_ordinary_turn_admission(db):
    transition = _publish(db)
    ready = db.mark_context_rebase_ready(
        transition.transition_id,
        expected_continuation_digest=transition.continuation_digest,
        expected_child_session_id=transition.child_session_id,
    )
    assert db.assert_context_rebase_ready_for_turn("s1") == ready


def test_non_rebase_session_has_no_continuity_admission_requirement(db):
    assert db.assert_context_rebase_ready_for_turn("s0") is None
