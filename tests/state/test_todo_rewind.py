"""Scratch canonical todo state across ordinary rewind; no agent activation."""
import json

import pytest

from hermes_state import SessionDB, TodoSnapshotError


@pytest.fixture
def db(tmp_path):
    with SessionDB(tmp_path / "state.db") as store:
        store.create_session("s", source="test")
        yield store


def write(db, name, prior=None, *, items=None, anchor=None):
    if anchor is None:
        db.append_message("s", "user", name)
        anchor = db.append_message("s", "assistant", name)
    assert db.try_acquire_session_turn_lease("s", "owner", ttl_seconds=60)
    try:
        return db.commit_todo_snapshot(
            session_id="s", owner_execution_id=name,
            anchor_assistant_row_id=anchor,
            expected_head_id=db.get_active_message_watermark("s"),
            turn_lease_holder="owner", expected_prior_snapshot_id=prior,
            todos_json=json.dumps(
                [{"id": "a", "content": name, "status": "pending"}]
                if items is None else items,
                sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ),
        )
    finally:
        db.release_session_turn_lease("s", "owner")


def user_for(db, name):
    return next(r["id"] for r in db.get_messages("s")
                if r["role"] == "user" and r["content"] == name)


def test_rewind_selects_prior_version_and_restore_recovers_latest(db):
    first = write(db, "first")
    second = write(db, "second", first["snapshot_id"])
    target = user_for(db, "second")
    db.rewind_to_message("s", target)
    assert db.get_current_todo_snapshot("s") == first
    with SessionDB(db.db_path, read_only=True) as reopened:
        assert reopened.get_current_todo_snapshot("s") == first
    assert db.restore_rewound("s", target) == 2
    assert db.get_current_todo_snapshot("s") == second
    assert db.restore_rewound("s", target) == 0
    assert db.get_current_todo_snapshot("s") == second


def test_rewind_before_first_write_is_missing_not_committed_empty(db):
    write(db, "first", items=[])
    target = user_for(db, "first")
    db.rewind_to_message("s", target)
    assert db.get_current_todo_snapshot("s") is None
    db.restore_rewound("s", target)
    assert db.get_current_todo_snapshot("s")["todos"] == []


def test_committed_empty_survives_rewind_of_later_version(db):
    empty = write(db, "empty", items=[])
    write(db, "later", empty["snapshot_id"])
    db.rewind_to_message("s", user_for(db, "later"))
    assert db.get_current_todo_snapshot("s") == empty


def test_new_write_after_rewind_branches_from_selected_version(db):
    first = write(db, "first")
    discarded = write(db, "discarded", first["snapshot_id"])
    db.rewind_to_message("s", user_for(db, "discarded"))
    branch = write(db, "branch", first["snapshot_id"])
    assert branch["supersedes_snapshot_id"] == first["snapshot_id"]
    assert db.get_current_todo_snapshot("s") == branch
    db.rewind_to_message("s", user_for(db, "branch"))
    assert db.get_current_todo_snapshot("s") == first
    assert db.get_current_todo_snapshot("s") != discarded


def test_repeated_rewind_does_not_resurrect_discarded_version(db):
    first = write(db, "first")
    write(db, "second", first["snapshot_id"])
    target = user_for(db, "second")
    db.rewind_to_message("s", target)
    db.rewind_to_message("s", target)
    assert db.get_current_todo_snapshot("s") == first
    assert db.restore_rewound("s", target) == 0
    assert db.get_current_todo_snapshot("s") == first


def test_invalid_current_snapshot_blocks_rewind_without_mutation(db):
    write(db, "first")
    target = user_for(db, "first")
    db._execute_write(lambda c: c.execute("UPDATE todo_snapshots SET producer='import'"))
    before = db.get_messages("s", include_inactive=True)
    with pytest.raises(TodoSnapshotError):
        db.rewind_to_message("s", target)
    assert db.get_messages("s", include_inactive=True) == before


def test_generic_parent_link_never_inherits_version(db):
    write(db, "first")
    db.create_session("unrelated", source="test", parent_session_id="s")
    assert db.get_current_todo_snapshot("unrelated") is None


def state(db):
    with db._read_ctx() as conn:
        return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
                for table in ("sessions", "messages", "todo_snapshots", "session_rewinds")}


def test_restore_refuses_new_empty_write_even_without_new_messages(db):
    from hermes_state import RewindRestoreError
    first = write(db, "first")
    write(db, "second", first["snapshot_id"])
    target = user_for(db, "second")
    db.rewind_to_message("s", target)
    write(db, "empty-branch", first["snapshot_id"], items=[],
          anchor=first["anchor_message_id"])
    before = state(db)
    with pytest.raises(RewindRestoreError) as exc:
        db.restore_rewound("s", target)
    assert exc.value.reason == "todo_state_advanced"
    assert state(db) == before


def test_removed_branch_cas_and_execution_retry_are_refused(db):
    first = write(db, "first")
    second = write(db, "second", first["snapshot_id"])
    db.rewind_to_message("s", user_for(db, "second"))
    before = state(db)
    for name, prior in [("new", second["snapshot_id"]), ("second", first["snapshot_id"])]:
        with pytest.raises(TodoSnapshotError) as exc:
            write(db, name, prior, anchor=first["anchor_message_id"])
        assert exc.value.reason == "stale_version"
        assert state(db) == before


def test_corrupt_predecessor_is_not_skipped_during_rewind(db):
    first = write(db, "first")
    write(db, "second", first["snapshot_id"])
    db._execute_write(lambda c: c.execute(
        "UPDATE todo_snapshots SET todos_json='[ ]' WHERE snapshot_id=?",
        (first["snapshot_id"],)))
    before = state(db)
    with pytest.raises(TodoSnapshotError):
        db.rewind_to_message("s", user_for(db, "first"))
    assert state(db) == before


@pytest.mark.parametrize("stage", ["pointer", "operation"])
@pytest.mark.parametrize("action", ["rewind", "restore"])
def test_lifecycle_abort_is_atomic(db, stage, action):
    import sqlite3
    first = write(db, "first")
    write(db, "second", first["snapshot_id"])
    target = user_for(db, "second")
    if action == "restore":
        db.rewind_to_message("s", target)
    before = state(db)
    event = ("UPDATE OF todo_current_snapshot_id ON sessions" if stage == "pointer"
             else ("INSERT ON session_rewinds" if action == "rewind"
                   else "UPDATE ON session_rewinds"))
    db._execute_write(lambda c: c.execute(
        f"CREATE TRIGGER abort_lifecycle BEFORE {event} "
        "BEGIN SELECT RAISE(ABORT, 'injected lifecycle failure'); END"))
    with pytest.raises(sqlite3.IntegrityError, match="injected lifecycle failure"):
        if action == "rewind":
            db.rewind_to_message("s", target)
        else:
            db.restore_rewound("s", target)
    assert state(db) == before


def test_inactive_target_removes_only_current_branch(db):
    first = write(db, "first")
    write(db, "discarded", first["snapshot_id"])
    target = user_for(db, "discarded")
    db.rewind_to_message("s", target)
    branch = write(db, "branch", first["snapshot_id"])
    db.rewind_to_message("s", target)
    assert db.get_current_todo_snapshot("s") == first
    db.restore_rewound("s", target)
    assert db.get_current_todo_snapshot("s") == branch
    assert target not in db.get_active_message_ids("s")


def test_null_pointer_with_existing_history_requires_lifecycle_evidence(db):
    write(db, "first")
    db._execute_write(lambda c: c.execute(
        "UPDATE sessions SET todo_current_snapshot_id=NULL WHERE id='s'"))
    with pytest.raises(TodoSnapshotError):
        db.get_current_todo_snapshot("s")


def test_readonly_old_pointer_schema_refuses(db):
    import sqlite3
    path = db.db_path
    db.close()
    with sqlite3.connect(path) as conn:
        # Scratch legacy shape; no live state or owner state is fabricated.
        conn.execute("ALTER TABLE sessions DROP COLUMN todo_current_snapshot_id")
    with SessionDB(path, read_only=True) as old:
        with pytest.raises(TodoSnapshotError) as exc:
            old.get_current_todo_snapshot("s")
        assert exc.value.reason == "unsupported_schema"


def test_pointer_migration_keeps_former_latest_and_does_not_repeat(db):
    import sqlite3
    first = write(db, "first")
    second = write(db, "second", first["snapshot_id"])
    path = db.db_path
    db.close()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE sessions DROP COLUMN todo_current_snapshot_id")
        conn.execute("DELETE FROM state_meta WHERE key='todo_current_pointer_v1'")
        conn.execute("UPDATE schema_version SET version=28")
    with SessionDB(path) as migrated:
        assert migrated.get_current_todo_snapshot("s") == second
        migrated.rewind_to_message("s", user_for(migrated, "second"))
        assert migrated.get_current_todo_snapshot("s") == first
    with SessionDB(path) as reopened:
        assert reopened.get_current_todo_snapshot("s") == first


def test_legacy_unrecorded_lifecycle_does_not_get_invented_evidence(db):
    write(db, "first")
    target = user_for(db, "first")
    db.rewind_to_message("s", target)
    db._execute_write(lambda c: c.execute("UPDATE session_rewinds SET schema_version=1"))
    with pytest.raises(TodoSnapshotError):
        db.get_current_todo_snapshot("s")


def test_current_read_uses_one_statement(db):
    first = write(db, "first")
    write(db, "second", first["snapshot_id"])
    db.rewind_to_message("s", user_for(db, "second"))
    queries = []
    with db._read_ctx() as conn:
        conn.set_trace_callback(queries.append)
        try:
            assert db._current_todo_snapshot_on_conn(conn, "s") == first
        finally:
            conn.set_trace_callback(None)
    assert len(queries) == 1
