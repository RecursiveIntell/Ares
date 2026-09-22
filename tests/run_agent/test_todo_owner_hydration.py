"""Real owner-read hydration; producer/dispatcher wiring is a separate gate.

Fixtures commit through the internal SessionDB API under a real turn lease.
They are not proof of producer authentication or of a running provider.
"""
import json

import pytest

from hermes_state import SessionDB, TodoSnapshotError
from run_agent import AIAgent
from tools.todo_tool import TodoStore


ITEMS = [{"id": "work", "content": "Retain exact state", "status": "in_progress"}]
STALE = [{"id": "stale", "content": "Do not resurrect", "status": "pending"}]


def agent_for(db, session="s"):
    agent = AIAgent.__new__(AIAgent)
    agent._session_db = db
    agent._session_db_created = True
    agent.session_id = session
    agent._todo_store = TodoStore()
    agent.quiet_mode = True
    return agent


def history(items=STALE):
    return [
        {"role": "assistant", "tool_calls": [{"id": "model-id", "type": "function",
          "function": {"name": "todo", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "model-id", "content": json.dumps({"todos": items})},
    ]


@pytest.fixture
def owner(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s", source="test")
    yield db
    db.close()


def commit(db, items=ITEMS, *, execution="execution-1"):
    current = db.get_current_todo_snapshot("s")
    rows = [{"role": "user", "content": execution},
            {"role": "assistant", "content": "tool execution boundary"}]
    db.append_messages_batch("s", rows)
    assert db.try_acquire_session_turn_lease("s", "holder", ttl_seconds=60)
    try:
        return db.commit_todo_snapshot(
            session_id="s", owner_execution_id=execution,
            anchor_assistant_row_id=rows[-1]["_row_id"], expected_head_id=rows[-1]["_row_id"],
            turn_lease_holder="holder",
            expected_prior_snapshot_id=current["snapshot_id"] if current else None,
            todos_json=json.dumps(items, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        )
    finally:
        db.release_session_turn_lease("s", "holder")


@pytest.mark.parametrize("items", [ITEMS, []])
@pytest.mark.parametrize("initial", [[], STALE])
@pytest.mark.parametrize("supplied_history", [[], history()])
def test_owner_selected_state_overrides_local_and_history_after_reopen(owner, items, initial, supplied_history):
    commit(owner, items)
    with SessionDB(owner.db_path, read_only=True) as reopened:
        agent = agent_for(reopened)
        agent._todo_store.write(initial)
        agent._hydrate_todo_store(supplied_history)
        assert agent._todo_store.read() == items


@pytest.mark.parametrize("transition", ["in_place", "child"])
def test_owner_copied_state_hydrates_only_selected_scope(owner, transition):
    commit(owner)
    handoff = [{"role": "user", "content": "Summary without todo text"}]
    target = "s"
    if transition == "in_place":
        owner.archive_and_compact("s", handoff)
    else:
        assert owner.try_acquire_compression_lock("s", "compress", ttl_seconds=60)
        owner.publish_compression_child(
            parent_session_id="s", child_session_id="c", source="test",
            messages=handoff, compression_lock_holder="compress",
        )
        target = "c"
    agent = agent_for(owner, target)
    agent._hydrate_todo_store([])
    assert agent._todo_store.read() == ITEMS


def test_rewind_to_before_first_snapshot_does_not_resurrect_caller_history(owner):
    commit(owner)
    first_user = owner.get_messages("s")[0]["id"]
    owner.rewind_to_message("s", first_user)
    assert owner.get_current_todo_snapshot("s") is None
    agent = agent_for(owner)
    agent._todo_store.write(ITEMS)
    agent._hydrate_todo_store(history())
    assert agent._todo_store.read() == []


@pytest.mark.parametrize("transition", ["in_place", "child", "repeated_child"])
@pytest.mark.parametrize("initial", [[], STALE])
@pytest.mark.parametrize("supplied_history", [[], history()])
def test_cleared_selection_survives_owner_lifecycle_and_reopen(
    owner, transition, initial, supplied_history,
):
    commit(owner)
    first_user = owner.get_messages("s")[0]["id"]
    owner.rewind_to_message("s", first_user)
    target = "s"
    for child in (["c", "grandchild"] if transition == "repeated_child" else ["c"]):
        handoff = [{"role": "user", "content": "Summary without todo authority"}]
        if transition == "in_place":
            owner.archive_and_compact(target, handoff)
        else:
            assert owner.try_acquire_compression_lock(target, "compress", ttl_seconds=60)
            owner.publish_compression_child(
                parent_session_id=target, child_session_id=child, source="test",
                messages=handoff, compression_lock_holder="compress",
            )
            target = child
    with SessionDB(owner.db_path, read_only=True) as reopened:
        # Absent selected snapshot is not a never-used todo owner.
        assert reopened.get_current_todo_snapshot(target) is None
        assert reopened.get_todo_recovery_state(target).state == "cleared_by_rewind"
        agent = agent_for(reopened, target)
        agent._todo_store.write(initial)
        agent._hydrate_todo_store(supplied_history)
        assert agent._todo_store.read() == []


def test_rewind_and_restore_select_exact_prior_state(owner):
    commit(owner)
    commit(owner, [], execution="execution-2")
    second_user = next(r for r in owner.get_messages("s") if r["content"] == "execution-2")
    owner.rewind_to_message("s", second_user["id"])
    agent = agent_for(owner)
    agent._hydrate_todo_store(history())
    assert agent._todo_store.read() == ITEMS


@pytest.mark.parametrize("mutation", [
    "UPDATE todo_snapshots SET schema_version=999",
    "UPDATE todo_snapshots SET todos_json='{}'",
    "UPDATE sessions SET todo_current_snapshot_id=999 WHERE id='s'",
])
def test_owner_validation_failure_never_falls_through_to_history(owner, mutation):
    commit(owner)
    # Fault injection in this test-owned scratch database only.
    owner._conn.execute(mutation)
    owner._conn.commit()
    agent = agent_for(owner)
    with pytest.raises(TodoSnapshotError):
        agent._hydrate_todo_store(history())
    assert agent._todo_store.read() == []


def test_closed_owner_read_failure_never_falls_through(owner):
    commit(owner)
    assert owner.try_acquire_compression_lock("s", "compress", ttl_seconds=60)
    owner.publish_compression_child(parent_session_id="s", child_session_id="c", source="test",
        messages=[{"role": "user", "content": "Summary"}], compression_lock_holder="compress")
    with pytest.raises(TodoSnapshotError, match="closed_session"):
        agent_for(owner)._hydrate_todo_store(history())


def test_missing_owner_retains_explicit_legacy_compatibility(owner):
    agent = agent_for(owner)
    agent._hydrate_todo_store(history())
    assert agent._todo_store.read() == STALE


def test_missing_owner_does_not_overwrite_existing_local_state(owner):
    agent = agent_for(owner)
    agent._todo_store.write(ITEMS)
    agent._hydrate_todo_store(history())
    assert agent._todo_store.read() == ITEMS


def test_persistence_disabled_fork_cannot_read_parent_snapshot(owner):
    commit(owner)
    agent = agent_for(owner)
    agent._persist_disabled = True
    agent._hydrate_todo_store([])
    assert agent._todo_store.read() == []


def test_uncreated_session_keeps_legacy_without_inventing_owner_state(owner):
    agent = agent_for(owner, "new-session")
    agent._session_db_created = False
    agent._hydrate_todo_store(history())
    assert agent._todo_store.read() == STALE
    assert owner.get_session("new-session") is None


def test_existing_owner_read_does_not_depend_on_agent_created_flag(owner):
    commit(owner)
    agent = agent_for(owner)
    agent._session_db_created = False
    agent._hydrate_todo_store([])
    assert agent._todo_store.read() == ITEMS


def test_generic_parent_links_and_imported_pairs_do_not_supply_owner_state(owner):
    commit(owner)
    owner.create_session("branch", source="test", parent_session_id="s")
    owner.append_messages_batch("branch", history())
    agent = agent_for(owner, "branch")
    agent._hydrate_todo_store([])
    assert agent._todo_store.read() == []


def test_known_session_disappearance_is_not_legacy_missing(owner):
    agent = agent_for(owner, "absent")
    with pytest.raises(TodoSnapshotError, match="session"):
        agent._hydrate_todo_store(history())
    assert owner.get_session("absent") is None


def test_owner_selection_projection_distinguishes_missing_empty_and_rewind(owner):
    assert owner.get_todo_recovery_state("absent").state == "missing_session"
    assert owner.get_todo_recovery_state("s").state == "never_committed"
    snapshot = commit(owner, [])
    selected = owner.get_todo_recovery_state("s")
    assert selected.state == "selected"
    assert selected.snapshot == snapshot
    assert selected.snapshot["todos"] == []
    first_user = owner.get_messages("s")[0]["id"]
    owner.rewind_to_message("s", first_user)
    cleared = owner.get_todo_recovery_state("s")
    assert cleared.state == "cleared_by_rewind"
    assert cleared.snapshot is None
    owner.restore_rewound("s", first_user)
    restored = owner.get_todo_recovery_state("s")
    assert restored.state == "selected"
    assert restored.snapshot["todos"] == []


def test_owner_projection_rejects_broken_rewind_selection(owner):
    commit(owner)
    first_user = owner.get_messages("s")[0]["id"]
    owner.rewind_to_message("s", first_user)
    owner._conn.execute("UPDATE session_rewinds SET schema_version=999")
    owner._conn.commit()
    with pytest.raises(TodoSnapshotError, match="causal_scope"):
        owner.get_todo_recovery_state("s")
