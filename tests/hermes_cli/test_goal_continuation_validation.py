"""Revalidate canonical checkpoints when a queued goal turn actually starts."""

import pytest

from hermes_cli import goals


@pytest.fixture
def pending(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    cache = {}
    monkeypatch.setattr(goals, "_DB_CACHE", cache)
    manager = goals.GoalManager("consumer-validation", default_max_turns=4)
    manager.set("finish bounded work")
    manager.evaluate_after_turn("", turn_outcome=goals.EXECUTION_FAILED)
    assert manager.state is not None
    assert manager.claim_continuation("scheduler") is True
    assert manager.release_continuation(queued=True) is True
    db = goals._get_session_db()
    assert db is not None
    yield manager, db
    for database in cache.values():
        database.close()


def _identity(state):
    return {
        "expected_goal_id": state.goal_id,
        "expected_checkpoint_revision": state.checkpoint_revision,
        "expected_continuation_token": state.continuation_token,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("goal_id", "another-goal"),
        ("checkpoint_revision", 0),
        ("outcome", goals.GOAL_COMPLETED),
        ("remaining_goal_turns", 99),
        ("next_admissible_action", None),
        ("checkpoint_revision", "not-an-integer"),
        ("remaining_goal_turns", {"invalid": True}),
    ],
)
def test_consumer_revalidates_checkpoint_after_enqueue(pending, field, value):
    manager, db = pending
    identity = _identity(manager.state)
    manager.state.checkpoint[field] = value
    assert goals.save_goal(manager.session_id, manager.state) is True
    before = db.get_meta(goals._meta_key(manager.session_id))

    restarted = goals.GoalManager(manager.session_id)
    assert restarted.start_continuation(**identity) is False
    assert restarted.state is not None
    assert db.get_meta(goals._meta_key(manager.session_id)) == before
    assert restarted.state.continuation_pending is True
    assert restarted.state.outcome == goals.CONTINUATION_REQUIRED


@pytest.mark.parametrize("damage", ["missing-checkpoint", "wrong-token-reason", "invalid-uuid"])
def test_consumer_rejects_other_invalid_checkpoint_material(pending, damage):
    manager, db = pending
    if damage == "missing-checkpoint":
        manager.state.checkpoint = None
    elif damage == "wrong-token-reason":
        manager.state.last_stop_reason = "changed-after-enqueue"
    else:
        manager.state.goal_id = "not-a-uuid"
        manager.state.checkpoint["goal_id"] = "not-a-uuid"
    assert goals.save_goal(manager.session_id, manager.state) is True
    before = db.get_meta(goals._meta_key(manager.session_id))

    restarted = goals.GoalManager(manager.session_id)
    assert restarted.start_continuation(**_identity(restarted.state)) is False
    assert db.get_meta(goals._meta_key(manager.session_id)) == before


def test_valid_checkpoint_starts_once_without_resetting_budget(pending):
    manager, db = pending
    identity = _identity(manager.state)
    before = goals.load_goal(manager.session_id)
    assert before is not None
    restarted = goals.GoalManager(manager.session_id)
    assert restarted.start_continuation(**identity) is True
    persisted = goals.load_goal(manager.session_id)
    assert persisted is not None
    assert restarted.state is not None
    assert persisted.to_json() == restarted.state.to_json()
    assert persisted.continuation_pending is False
    assert persisted.outcome == goals.GOAL_ACTIVE
    assert persisted.checkpoint == before.checkpoint
    assert persisted.turns_used == before.turns_used
    assert persisted.max_turns == before.max_turns
    consumed = db.get_meta(goals._meta_key(manager.session_id))
    assert goals.GoalManager(manager.session_id).start_continuation(**identity) is False
    assert db.get_meta(goals._meta_key(manager.session_id)) == consumed


@pytest.mark.parametrize("failure", ["rejected", "exception"])
def test_failed_consumer_write_does_not_publish_started_projection(pending, monkeypatch, failure):
    manager, db = pending
    before = db.get_meta(goals._meta_key(manager.session_id))

    def fail(*args):
        if failure == "exception":
            raise OSError("injected scratch persistence failure")
        return False

    monkeypatch.setattr(db, "compare_and_set_meta", fail)
    assert manager.start_continuation(**_identity(manager.state)) is False
    assert db.get_meta(goals._meta_key(manager.session_id)) == before
    persisted = goals.load_goal(manager.session_id)
    assert persisted is not None
    assert manager.state.to_json() == persisted.to_json()
    assert manager.state.continuation_pending is True


def test_racing_pause_wins_consumer_cas_and_cached_projection(pending, monkeypatch):
    manager, db = pending
    original = db.compare_and_set_meta
    paused_bytes = []

    def pause_then_compare(key, expected, updated):
        other = goals.GoalManager(manager.session_id)
        assert other.pause("operator pause during consume") is not None
        paused_bytes.append(db.get_meta(key))
        return original(key, expected, updated)

    monkeypatch.setattr(db, "compare_and_set_meta", pause_then_compare)
    assert manager.start_continuation(**_identity(manager.state)) is False
    assert db.get_meta(goals._meta_key(manager.session_id)) == paused_bytes[0]
    assert manager.state.status == "paused"
    assert manager.state.outcome == goals.GOAL_PAUSED
    persisted = goals.load_goal(manager.session_id)
    assert persisted is not None
    assert manager.state.to_json() == persisted.to_json()


@pytest.mark.parametrize("damage", ["read-failure", "unparseable-row"])
def test_failed_cas_with_unreadable_reconciliation_clears_cache(pending, monkeypatch, damage):
    manager, db = pending
    read = db.get_meta
    expected = read(goals._meta_key(manager.session_id))

    def lose(*args):
        def damaged_read(key):
            if damage == "read-failure":
                raise OSError("injected scratch read failure")
            return "invalid JSON"

        monkeypatch.setattr(db, "get_meta", damaged_read)
        return False

    monkeypatch.setattr(db, "compare_and_set_meta", lose)
    assert manager.start_continuation(**_identity(manager.state)) is False
    assert manager.state is None
    assert read(goals._meta_key(manager.session_id)) == expected


def test_exception_after_committed_cas_never_grants_dispatch_or_retries(pending, monkeypatch):
    manager, db = pending
    compare = db.compare_and_set_meta
    identity = _identity(manager.state)
    calls = []

    def commit_then_fail(*args):
        calls.append(args)
        assert compare(*args) is True
        raise OSError("injected failure after scratch commit")

    monkeypatch.setattr(db, "compare_and_set_meta", commit_then_fail)
    assert manager.start_continuation(**identity) is False
    assert len(calls) == 1
    persisted = goals.load_goal(manager.session_id)
    assert persisted is not None
    assert manager.state is not None
    assert manager.state.to_json() == persisted.to_json()
    assert persisted.continuation_pending is False
    assert goals.GoalManager(manager.session_id).start_continuation(**identity) is False
    assert len(calls) == 1


def test_newer_checkpoint_wins_consumer_cas(pending, monkeypatch):
    manager, db = pending
    compare = db.compare_and_set_meta
    previous_revision = manager.state.checkpoint_revision

    def update_then_compare(*args):
        other = goals.GoalManager(manager.session_id)
        other.checkpoint_recovery("NEW_FAILURE")
        return compare(*args)

    monkeypatch.setattr(db, "compare_and_set_meta", update_then_compare)
    assert manager.start_continuation(**_identity(manager.state)) is False
    persisted = goals.load_goal(manager.session_id)
    assert persisted is not None
    assert manager.state is not None
    assert persisted.checkpoint_revision > previous_revision
    assert persisted.continuation_pending is True
    assert manager.state.to_json() == persisted.to_json()
