"""Owner todo continuity through real scratch SessionDB compaction transactions."""
import json

import pytest

from hermes_state import SessionDB, TodoSnapshotError


@pytest.fixture
def db(tmp_path):
    with SessionDB(tmp_path / "state.db") as store:
        store.create_session("s", source="test")
        yield store


def write(db, name, *, session="s", prior=None, items=None):
    db.append_message(session, "user", name)
    anchor = db.append_message(session, "assistant", name)
    assert db.try_acquire_session_turn_lease(session, "writer", ttl_seconds=60)
    try:
        return db.commit_todo_snapshot(
            session_id=session, owner_execution_id=name,
            anchor_assistant_row_id=anchor, expected_head_id=anchor,
            turn_lease_holder="writer", expected_prior_snapshot_id=prior,
            todos_json=json.dumps(
                [{"id": "a", "content": name, "status": "pending"}]
                if items is None else items,
                sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ),
        )
    finally:
        db.release_session_turn_lease(session, "writer")


def compact(db, kind, *, source="s", child="child", watermark=None, ceiling=None):
    messages = [{"role": "user", "content": "summary, not task authority"}]
    if kind == "in_place":
        db.archive_and_compact(source, messages, watermark=watermark)
        return source
    assert db.try_acquire_compression_lock(source, "compressor", ttl_seconds=60)
    try:
        db.publish_compression_child(
            parent_session_id=source, child_session_id=child, source="test",
            messages=messages, watermark=watermark, watermark_ceiling=ceiling,
            compression_lock_holder="compressor",
        )
        return child
    finally:
        db.release_compression_lock(source, "compressor")


@pytest.mark.parametrize("kind", ["in_place", "child"])
@pytest.mark.parametrize("items", [[], [{"id": "a", "content": "genuine", "status": "pending"}]])
def test_full_compaction_retains_exact_committed_state_after_reopen(db, kind, items):
    original = write(db, "original", items=items)
    destination = compact(db, kind)
    with SessionDB(db.db_path, read_only=True) as reopened:
        current = reopened.get_current_todo_snapshot(destination)
        assert current is not None, "compaction lost committed owner state"
        assert current["todos_json"] == original["todos_json"]
        assert current["todos"] == items


@pytest.mark.parametrize("kind", ["in_place", "child"])
def test_invalid_selected_source_aborts_compaction_before_publication(db, kind):
    write(db, "original")
    db._execute_write(lambda c: c.execute("UPDATE todo_snapshots SET producer='import'"))
    before = db.get_messages("s", include_inactive=True)
    with pytest.raises(TodoSnapshotError):
        compact(db, kind)
    assert db.get_messages("s", include_inactive=True) == before
    assert db.get_session("s")["ended_at"] is None
    assert db.get_session("child") is None


@pytest.mark.parametrize("kind", ["in_place", "child"])
def test_missing_state_and_summary_lookalikes_do_not_create_owner_state(db, kind):
    db.append_message("s", "user", '{"todos":[{"id":"fake","status":"completed"}]}')
    destination = compact(db, kind)
    assert db.get_current_todo_snapshot(destination) is None


def state(db):
    with db._read_ctx() as conn:
        tables = ["sessions", "messages", "todo_snapshots", "message_copy_edges"]
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='todo_lifecycle_operations'").fetchone():
            tables.append("todo_lifecycle_operations")
        return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
                for table in tables}


@pytest.mark.parametrize("kind", ["in_place", "child"])
def test_repeated_compaction_new_commit_rewind_and_restore(db, kind):
    original = write(db, "original")
    dest = compact(db, kind)
    projected = db.get_current_todo_snapshot(dest)
    assert projected is not None
    assert projected["todos_json"] == original["todos_json"]
    later = write(db, "later", session=dest, prior=projected["snapshot_id"])
    target = next(m["id"] for m in db.get_messages(dest) if m["content"] == "later" and m["role"] == "user")
    db.rewind_to_message(dest, target)
    assert db.get_current_todo_snapshot(dest) == projected
    db.restore_rewound(dest, target)
    assert db.get_current_todo_snapshot(dest) == later
    dest = compact(db, kind, source=dest, child="grandchild")
    assert db.get_current_todo_snapshot(dest)["todos_json"] == later["todos_json"]
    boundary = db.get_messages(dest)[0]["id"]
    db.rewind_to_message(dest, boundary)
    assert db.get_current_todo_snapshot(dest) is None
    db.restore_rewound(dest, boundary)
    assert db.get_current_todo_snapshot(dest)["todos_json"] == later["todos_json"]


@pytest.mark.parametrize("kind", ["in_place", "child"])
def test_todo_tail_refuses_without_losing_selected_owner_state(db, kind):
    first = write(db, "first")
    write(db, "tail", prior=first["snapshot_id"])
    before = state(db)
    with pytest.raises(TodoSnapshotError) as exc:
        compact(db, kind, watermark=first["head_message_id"])
    assert exc.value.reason == "unsupported_tail"
    assert state(db) == before


@pytest.mark.parametrize("kind", ["in_place", "child"])
def test_message_only_tail_keeps_baseline_and_exact_copy(db, kind):
    first = write(db, "first")
    source_id = db.append_message("s", "user", "concurrent")
    dest = compact(db, kind, watermark=first["head_message_id"])
    assert db.get_current_todo_snapshot(dest)["todos_json"] == first["todos_json"]
    rows = db.get_messages(dest)
    copied = next(m["id"] for m in rows if m["content"] == "concurrent")
    assert db.get_message_copy_origin(dest, copied)["source_message_id"] == source_id
    db.rewind_to_message(dest, copied)
    assert db.get_current_todo_snapshot(dest)["todos_json"] == first["todos_json"]


@pytest.mark.parametrize("kind", ["in_place", "child"])
def test_corrupt_predecessor_aborts_atomically(db, kind):
    first = write(db, "first")
    write(db, "second", prior=first["snapshot_id"])
    db._execute_write(lambda c: c.execute(
        "UPDATE todo_snapshots SET producer='import' WHERE snapshot_id=?", (first["snapshot_id"],)))
    before = state(db)
    with pytest.raises(TodoSnapshotError):
        compact(db, kind)
    assert state(db) == before


def test_empty_compaction_cannot_strand_committed_snapshot(db):
    write(db, "first")
    before = state(db)
    with pytest.raises(TodoSnapshotError):
        db.archive_and_compact("s", [])
    assert state(db) == before


def test_supplied_row_id_is_not_lifecycle_boundary(db):
    first = write(db, "first")
    messages = [{"role": "user", "content": "summary", "_row_id": first["head_message_id"]}]
    db.archive_and_compact("s", messages)
    current = db.get_current_todo_snapshot("s")
    assert current["anchor_message_id"] == db.get_messages("s")[0]["id"]
    assert current["anchor_message_id"] != first["head_message_id"]


@pytest.mark.parametrize("kind", ["in_place", "child"])
def test_compaction_after_rewind_keeps_missing_not_discarded_version(db, kind):
    write(db, "first")
    target = db.get_messages("s")[0]["id"]
    db.rewind_to_message("s", target)
    dest = compact(db, kind)
    assert db.get_current_todo_snapshot(dest) is None


@pytest.mark.parametrize("kind", ["in_place", "child"])
@pytest.mark.parametrize("mutation", [
    "DELETE FROM todo_lifecycle_operations",
    "UPDATE todo_lifecycle_operations SET schema_version=99",
    "UPDATE todo_lifecycle_operations SET kind='import'",
    "UPDATE todo_lifecycle_operations SET source_session_id='wrong'",
    "UPDATE todo_lifecycle_operations SET destination_session_id='wrong'",
    "UPDATE todo_lifecycle_operations SET source_current_snapshot_id=99999",
    "UPDATE todo_lifecycle_operations SET source_rewind_count=-1",
    "UPDATE todo_lifecycle_operations SET destination_rewind_count=99",
    "UPDATE todo_lifecycle_operations SET boundary_message_id=99999",
    "UPDATE todo_lifecycle_operations SET watermark=0",
    "UPDATE todo_lifecycle_operations SET watermark_ceiling=-1",
    "UPDATE todo_snapshots SET lifecycle_source_snapshot_id=99999 WHERE schema_version=2",
    "UPDATE todo_snapshots SET lifecycle_operation_id='absent' WHERE schema_version=2",
    "UPDATE todo_snapshots SET lifecycle_kind='copied_tail' WHERE schema_version=2",
    "UPDATE todo_snapshots SET execution_id='lookalike' WHERE schema_version=2",
    "UPDATE todo_snapshots SET todos_json='[]' WHERE schema_version=2",
    "UPDATE todo_snapshots SET producer='import' WHERE schema_version=1",
    "UPDATE todo_snapshots SET todos_json='[]' WHERE schema_version=1",
    "DELETE FROM todo_snapshots WHERE schema_version=1",
])
def test_lifecycle_read_rejects_unbound_projection(db, kind, mutation):
    write(db, "first")
    destination = compact(db, kind)
    db.create_session("wrong", source="test")
    db._execute_write(lambda c: c.execute(mutation))
    with pytest.raises(TodoSnapshotError):
        db.get_current_todo_snapshot(destination)


@pytest.mark.parametrize("kind", ["in_place", "child"])
@pytest.mark.parametrize("mutation", [
    "DELETE FROM todo_lifecycle_operations WHERE source_current_snapshot_id=1",
    "UPDATE todo_snapshots SET producer='import' WHERE snapshot_id=1",
    "UPDATE todo_snapshots SET todos_json='[]' WHERE snapshot_id=1",
])
def test_repeated_projection_does_not_hide_corrupt_historical_source(db, kind, mutation):
    write(db, "first")
    destination = compact(db, kind)
    destination = compact(db, kind, source=destination, child="grandchild")
    db._execute_write(lambda c: c.execute(mutation))
    with pytest.raises(TodoSnapshotError):
        db.get_current_todo_snapshot(destination)


@pytest.mark.parametrize("kind", ["in_place", "child"])
def test_failed_projection_insert_rolls_back_transcript_operation_and_pointer(db, kind):
    import sqlite3

    write(db, "first")
    db._execute_write(lambda c: c.execute(
        "CREATE TRIGGER fail_projection BEFORE INSERT ON todo_snapshots "
        "WHEN NEW.schema_version=2 BEGIN SELECT RAISE(ABORT,'projection-fault'); END"))
    before = state(db)
    with pytest.raises(sqlite3.IntegrityError, match="projection-fault"):
        compact(db, kind)
    assert state(db) == before


def test_old_schema_reconciles_without_rewriting_dispatch_evidence(db):
    original = write(db, "first")

    def old_schema(c):
        c.execute("DROP INDEX idx_todo_lifecycle_source")
        c.execute("DROP TABLE todo_lifecycle_operations")
        for col in ("lifecycle_source_snapshot_id", "lifecycle_operation_id", "lifecycle_kind"):
            c.execute(f"ALTER TABLE todo_snapshots DROP COLUMN {col}")

    db._execute_write(old_schema)
    with SessionDB(db.db_path, read_only=True) as old_reader:
        with pytest.raises(TodoSnapshotError, match="unsupported_schema"):
            old_reader.get_current_todo_snapshot("s")
    with SessionDB(db.db_path) as reconciled:
        assert reconciled.get_current_todo_snapshot("s") == original
        with reconciled._read_ctx() as c:
            assert c.execute("SELECT COUNT(*) FROM todo_lifecycle_operations").fetchone()[0] == 0
        compact(reconciled, "in_place")
        assert reconciled.get_current_todo_snapshot("s")["todos_json"] == original["todos_json"]


def test_dispatch_snapshot_cannot_claim_lifecycle_metadata(db):
    write(db, "first")
    db._execute_write(lambda c: c.execute("UPDATE todo_snapshots SET lifecycle_kind='baseline'"))
    with pytest.raises(TodoSnapshotError, match="lifecycle_binding"):
        db.get_current_todo_snapshot("s")


@pytest.mark.parametrize("kind", ["in_place", "child"])
@pytest.mark.parametrize("repeated", [False, True])
@pytest.mark.parametrize("mutation", [
    "missing_anchor", "wrong_scope", "wrong_role", "missing_prior", "wrong_prior_scope",
    "foreign_anchor", "foreign_head", "foreign_session", "future_rewind",
    "self_prior", "future_prior", "later_prior_head", "later_prior_rewind",
    "prior_producer", "prior_payload", "prior_anchor", "prior_cycle",
])
def test_historical_source_coordinates_still_validate_after_compaction(db, kind, repeated, mutation):
    db.create_session("wrong", source="test")
    foreign = write(db, "foreign", session="wrong")
    first = write(db, "first")
    selected = write(db, "selected", prior=first["snapshot_id"])
    destination = compact(db, kind)
    projection = db.get_current_todo_snapshot(destination)
    if repeated:
        destination = compact(db, kind, source=destination, child="grandchild")

    def corrupt(c):
        if mutation == "missing_anchor":
            c.execute("DELETE FROM messages WHERE id=?", (selected["anchor_message_id"],))
        elif mutation == "wrong_scope":
            c.execute("UPDATE messages SET session_id='wrong' WHERE id=?", (selected["anchor_message_id"],))
        elif mutation == "wrong_role":
            c.execute("UPDATE messages SET role='user' WHERE id=?", (selected["anchor_message_id"],))
        elif mutation == "missing_prior":
            c.execute("DELETE FROM todo_snapshots WHERE snapshot_id=?", (first["snapshot_id"],))
        else:
            column, value, target = {
                "wrong_prior_scope": ("session_id", "wrong", first),
                "foreign_anchor": ("anchor_message_id", foreign["anchor_message_id"], selected),
                "foreign_head": ("head_message_id", foreign["head_message_id"], selected),
                "foreign_session": ("session_id", "wrong", selected),
                "future_rewind": ("rewind_count", 1, selected),
                "self_prior": ("supersedes_snapshot_id", selected["snapshot_id"], selected),
                "future_prior": ("supersedes_snapshot_id", projection["snapshot_id"], selected),
                "later_prior_head": ("head_message_id", projection["head_message_id"], first),
                "later_prior_rewind": ("rewind_count", 1, first),
                "prior_producer": ("producer", "import", first),
                "prior_payload": ("todos_json", "not-json", first),
                "prior_anchor": ("anchor_message_id", foreign["anchor_message_id"], first),
                "prior_cycle": ("supersedes_snapshot_id", first["snapshot_id"], first),
            }[mutation]
            c.execute(f"UPDATE todo_snapshots SET {column}=? WHERE snapshot_id=?",
                      (value, target["snapshot_id"]))

    db._execute_write(corrupt)
    before = state(db)
    with pytest.raises(TodoSnapshotError):
        db.get_current_todo_snapshot(destination)
    with pytest.raises(TodoSnapshotError):
        compact(db, kind, source=destination, child="rejected")
    assert state(db) == before


@pytest.mark.parametrize("kind", ["in_place", "child"])
@pytest.mark.parametrize("restored", [False, True])
def test_new_dispatch_after_rewind_is_not_bound_to_old_transition_pointer(db, kind, restored):
    first = write(db, "first")
    second = write(db, "second", prior=first["snapshot_id"])
    target = next(m["id"] for m in db.get_messages("s")
                  if m["role"] == "user" and m["content"] == "second")
    db.rewind_to_message("s", target)
    if restored:
        db.restore_rewound("s", target)
    previous = second if restored else first
    current = write(db, "new branch", prior=previous["snapshot_id"])
    rewind = db.get_session("s")["rewind_count"]
    with db._read_ctx() as c:
        transition = c.execute(
            "SELECT * FROM session_rewinds WHERE session_id='s' AND rewind_count=?", (rewind,),
        ).fetchone()
        selected_at_transition = transition[
            "todo_before_snapshot_id" if restored else "todo_after_snapshot_id"
        ]
        assert selected_at_transition == previous["snapshot_id"]
        assert selected_at_transition != current["snapshot_id"]
    # The rewind record witnesses its transition, not every subsequent dispatch
    # or compaction within that rewind coordinate. Neither equality is authority.
    destination = compact(db, kind)
    assert db.get_current_todo_snapshot(destination)["todos_json"] == current["todos_json"]
    destination = compact(db, kind, source=destination, child="grandchild")
    with SessionDB(db.db_path, read_only=True) as reopened:
        assert reopened.get_current_todo_snapshot(destination)["todos_json"] == current["todos_json"]


def test_missing_selected_predecessor_keeps_owner_error_category(db):
    first = write(db, "first")
    write(db, "second", prior=first["snapshot_id"])
    db._execute_write(lambda c: c.execute(
        "DELETE FROM todo_snapshots WHERE snapshot_id=?", (first["snapshot_id"],)))
    with pytest.raises(TodoSnapshotError) as exc:
        db.get_current_todo_snapshot("s")
    assert exc.value.reason == "predecessor"


def test_reachable_older_payload_corruption_is_still_rejected(db):
    first = write(db, "first")
    second = write(db, "second", prior=first["snapshot_id"])
    write(db, "third", prior=second["snapshot_id"])
    db._execute_write(lambda c: c.execute(
        "UPDATE todo_snapshots SET todos_json='invalid' WHERE snapshot_id=?", (first["snapshot_id"],)))
    with pytest.raises(TodoSnapshotError) as exc:
        db.get_current_todo_snapshot("s")
    assert exc.value.reason == "payload"


@pytest.mark.parametrize("kind", ["in_place", "child"])
@pytest.mark.parametrize("mutation", ["boundary", "message_scope", "source_rewind", "future_source_rewind"])
def test_historical_projection_coordinates_reject_after_repeated_compaction(db, kind, mutation):
    write(db, "first")
    destination = compact(db, kind)
    historical = db.get_current_todo_snapshot(destination)
    destination = compact(db, kind, source=destination, child="grandchild")
    db.create_session("wrong", source="test")

    def corrupt(c):
        if mutation == "message_scope":
            c.execute("UPDATE messages SET session_id='wrong' WHERE id=?",
                      (historical["anchor_message_id"],))
        else:
            column = "boundary_message_id" if mutation == "boundary" else "source_rewind_count"
            value = 1 if mutation == "future_source_rewind" else -1
            c.execute(f"UPDATE todo_lifecycle_operations SET {column}=? "
                      "WHERE operation_id=(SELECT lifecycle_operation_id FROM todo_snapshots WHERE snapshot_id=?)",
                      (value, historical["snapshot_id"]))

    db._execute_write(corrupt)
    before = state(db)
    with pytest.raises(TodoSnapshotError):
        db.get_current_todo_snapshot(destination)
    with pytest.raises(TodoSnapshotError):
        compact(db, kind, source=destination, child="rejected")
    assert state(db) == before


@pytest.mark.parametrize("kind", ["in_place", "child"])
def test_compaction_of_restored_snapshot_records_current_source_rewind(db, kind):
    original = write(db, "first")
    target = db.get_messages("s")[0]["id"]
    db.rewind_to_message("s", target)
    db.restore_rewound("s", target)
    assert db.get_current_todo_snapshot("s") == original
    current_rewind = db.get_session("s")["rewind_count"]
    assert current_rewind > original["rewind_count"]
    destination = compact(db, kind)
    with db._read_ctx() as c:
        operation = c.execute("SELECT * FROM todo_lifecycle_operations").fetchone()
        assert operation["source_rewind_count"] == current_rewind
        assert operation["source_rewind_count"] != original["rewind_count"]
    with SessionDB(db.db_path, read_only=True) as reopened:
        assert reopened.get_current_todo_snapshot(destination)["todos_json"] == original["todos_json"]
    destination = compact(db, kind, source=destination, child="grandchild")
    assert db.get_current_todo_snapshot(destination)["todos_json"] == original["todos_json"]


def test_duplicate_source_projection_within_operation_is_rejected(db):
    import sqlite3

    write(db, "first")
    compact(db, "in_place")
    before = state(db)
    with pytest.raises(sqlite3.IntegrityError, match="todo_snapshots.lifecycle_operation_id"):
        db._execute_write(lambda c: c.execute(
            "INSERT INTO todo_snapshots (session_id,schema_version,producer,execution_id,"
            "anchor_message_id,head_message_id,rewind_count,supersedes_snapshot_id,"
            "todos_json,created_at,lifecycle_source_snapshot_id,lifecycle_operation_id,lifecycle_kind) "
            "SELECT session_id,schema_version,producer,'duplicate',anchor_message_id,"
            "head_message_id,rewind_count,supersedes_snapshot_id,todos_json,created_at,"
            "lifecycle_source_snapshot_id,lifecycle_operation_id,lifecycle_kind "
            "FROM todo_snapshots WHERE schema_version=2"))
    assert state(db) == before
