"""Clear baselines are owner lifecycle provenance, never empty dispatch writes."""
import sqlite3

import pytest

from hermes_state import SessionDB, TodoSnapshotError
from tests.state.test_todo_compaction import compact, state, write


@pytest.fixture
def db(tmp_path):
    with SessionDB(tmp_path / "state.db") as store:
        store.create_session("s", source="test")
        yield store


def clear(db):
    write(db, "first")
    db.rewind_to_message("s", db.get_messages("s")[0]["id"])


def all_state(db):
    result = state(db)
    result["rewinds"] = [tuple(r) for r in db._conn.execute("SELECT * FROM session_rewinds")]
    return result


@pytest.mark.parametrize("kind", ["in_place", "child"])
def test_clear_baseline_survives_commit_rewind_restore_and_repeated_projection(db, kind):
    clear(db)
    dest = compact(db, kind)
    assert db.get_todo_recovery_state(dest).state == "cleared_by_rewind"
    assert db.get_current_todo_snapshot(dest) is None
    assert db._conn.execute("SELECT count(*) FROM todo_snapshots").fetchone()[0] == 1
    selected = write(db, "new", session=dest)
    assert db.get_todo_recovery_state(dest).state == "selected"
    target = next(m["id"] for m in db.get_messages(dest) if m["content"] == "new" and m["role"] == "user")
    db.rewind_to_message(dest, target)
    assert db.get_todo_recovery_state(dest).state == "cleared_by_rewind"
    db.restore_rewound(dest, target)
    assert db.get_current_todo_snapshot(dest) == selected
    db.rewind_to_message(dest, target)
    dest = compact(db, kind, source=dest, child="second")
    dest = compact(db, kind, source=dest, child="third")
    with SessionDB(db.db_path, read_only=True) as reopened:
        assert reopened.get_todo_recovery_state(dest).state == "cleared_by_rewind"
        assert reopened.get_current_todo_snapshot(dest) is None


@pytest.mark.parametrize("kind", ["in_place", "child"])
@pytest.mark.parametrize("initial", ["never", "empty"])
def test_never_used_and_explicit_empty_are_not_clear_baselines(db, kind, initial):
    if initial == "empty":
        write(db, "empty", items=[])
    dest = compact(db, kind)
    dest = compact(db, kind, source=dest, child="second")
    result = db.get_todo_recovery_state(dest)
    assert result.state == ("selected" if initial == "empty" else "never_committed")
    assert db._conn.execute("SELECT count(*) FROM todo_lifecycle_operations WHERE schema_version=3").fetchone()[0] == 0


@pytest.mark.parametrize("mutation", [
    "DELETE FROM todo_lifecycle_operations WHERE schema_version=3",
    "UPDATE todo_lifecycle_operations SET schema_version=99 WHERE schema_version=3",
    "UPDATE todo_lifecycle_operations SET selection_kind='snapshot' WHERE schema_version=3",
    "UPDATE todo_lifecycle_operations SET source_current_snapshot_id=1 WHERE schema_version=3",
    "UPDATE todo_lifecycle_operations SET source_session_id='wrong' WHERE schema_version=3",
    "UPDATE todo_lifecycle_operations SET destination_session_id='wrong' WHERE schema_version=3",
    "UPDATE todo_lifecycle_operations SET kind='import' WHERE schema_version=3",
    "UPDATE todo_lifecycle_operations SET source_rewind_count=999 WHERE schema_version=3",
    "UPDATE todo_lifecycle_operations SET destination_rewind_count=999 WHERE schema_version=3",
    "UPDATE todo_lifecycle_operations SET boundary_message_id=999 WHERE schema_version=3",
    "UPDATE todo_lifecycle_operations SET source_clear_operation_id=operation_id WHERE schema_version=3",
    "UPDATE session_rewinds SET schema_version=999",
    "UPDATE session_rewinds SET todo_after_snapshot_id=1",
    "UPDATE sessions SET todo_clear_baseline_operation_id='missing' WHERE id='child'",
    "UPDATE sessions SET todo_clear_baseline_operation_id=NULL WHERE id='child'",
])
def test_invalid_clear_lineage_cannot_become_legacy_missing(db, mutation):
    clear(db)
    dest = compact(db, "child")
    db.create_session("wrong", source="test")
    db._execute_write(lambda c: c.execute(mutation))
    before = all_state(db)
    with pytest.raises(TodoSnapshotError):
        db.get_todo_recovery_state(dest)
    with pytest.raises(TodoSnapshotError):
        compact(db, "child", source=dest, child="second")
    assert all_state(db) == before


@pytest.mark.parametrize("stage", ["operation", "pointer"])
@pytest.mark.parametrize("kind", ["in_place", "child"])
def test_clear_publication_is_atomic(db, kind, stage):
    clear(db)
    if stage == "operation":
        sql = "CREATE TRIGGER fail_clear BEFORE INSERT ON todo_lifecycle_operations WHEN NEW.schema_version=3 BEGIN SELECT RAISE(ABORT,'injected'); END"
    else:
        sql = "CREATE TRIGGER fail_clear BEFORE UPDATE OF todo_clear_baseline_operation_id ON sessions WHEN NEW.todo_clear_baseline_operation_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'injected'); END"
    db._execute_write(lambda c: c.execute(sql))
    before = all_state(db)
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        compact(db, kind)
    assert all_state(db) == before


def test_arbitrary_parent_cannot_inherit_clear_baseline(db):
    clear(db)
    dest = compact(db, "child")
    db.create_session("unrelated", source="test", parent_session_id=dest)
    assert db.get_todo_recovery_state("unrelated").state == "never_committed"


def legacy_table(db):
    """Re-create exact legacy operation table shape only in disposable test DB."""
    columns = [r[1] for r in db._conn.execute("PRAGMA table_info(todo_lifecycle_operations)")
               if r[1] not in ("selection_kind", "source_clear_operation_id")]
    original = [tuple(r) for r in db._conn.execute("SELECT " + ','.join(columns) + " FROM todo_lifecycle_operations")]
    db._conn.execute("ALTER TABLE todo_lifecycle_operations RENAME TO old_operations")
    # The legacy declaration is an explicit test fixture, not production migration.
    db._conn.execute("CREATE TABLE todo_lifecycle_operations (operation_id TEXT PRIMARY KEY,schema_version INTEGER NOT NULL,kind TEXT NOT NULL,source_session_id TEXT NOT NULL,destination_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,source_current_snapshot_id INTEGER NOT NULL,source_rewind_count INTEGER NOT NULL,destination_rewind_count INTEGER NOT NULL,boundary_message_id INTEGER NOT NULL,watermark INTEGER,watermark_ceiling INTEGER,created_at REAL NOT NULL,UNIQUE(destination_session_id,boundary_message_id))")
    db._conn.execute("INSERT INTO todo_lifecycle_operations SELECT " + ','.join(columns) + " FROM old_operations")
    db._conn.execute("DROP TABLE old_operations")
    db._conn.commit()
    return columns, original


def test_legacy_operation_migration_preserves_all_old_fields_and_is_idempotent(db):
    write(db, "first")
    compact(db, "in_place")
    columns, original = legacy_table(db)
    with SessionDB(db.db_path) as migrated:
        after = [tuple(r) for r in migrated._conn.execute("SELECT " + ','.join(columns) + " FROM todo_lifecycle_operations")]
        assert after == original
        field = next(r for r in migrated._conn.execute("PRAGMA table_info(todo_lifecycle_operations)") if r[1] == "source_current_snapshot_id")
        assert field[3] == 0, "legacy NOT NULL still forbids a clear-selection operation"
        assert migrated.get_current_todo_snapshot("s")["todos"][0]["content"] == "first"
    with SessionDB(db.db_path) as reopened:
        assert [tuple(r) for r in reopened._conn.execute("SELECT " + ','.join(columns) + " FROM todo_lifecycle_operations")] == original


@pytest.mark.parametrize("stage", [sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE])
def test_operation_migration_failure_preserves_legacy_rows_and_schema(db, stage):
    write(db, "first")
    compact(db, "in_place")
    legacy_table(db)
    # Reconciliation adds columns before the canonical shape migration.
    db._conn.execute("ALTER TABLE todo_lifecycle_operations ADD COLUMN selection_kind TEXT NOT NULL DEFAULT 'snapshot'")
    db._conn.execute("ALTER TABLE todo_lifecycle_operations ADD COLUMN source_clear_operation_id TEXT")
    db._conn.commit()
    before = list(db._conn.iterdump())

    def deny(action, arg1, arg2, database, trigger):
        if action == stage and (
            arg1 in ("todo_lifecycle_operations", "todo_lifecycle_operations_clear_migration")
            or arg2 == "todo_lifecycle_operations_clear_migration"
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    db._conn.set_authorizer(deny)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            db._migrate_todo_clear_lifecycle()
    finally:
        db._conn.set_authorizer(None)
    assert list(db._conn.iterdump()) == before
    assert not db._conn.in_transaction
    db._migrate_todo_clear_lifecycle()
    assert db.get_current_todo_snapshot("s")["todos"][0]["content"] == "first"


@pytest.mark.parametrize("mutation", [
    "UPDATE todo_lifecycle_operations SET selection_kind='cleared_by_rewind' WHERE schema_version=1",
    "UPDATE todo_lifecycle_operations SET source_clear_operation_id='unbound' WHERE schema_version=1",
])
def test_snapshot_operations_do_not_accept_clear_discriminator_or_lineage(db, mutation):
    write(db, "first")
    compact(db, "in_place")
    db._execute_write(lambda c: c.execute(mutation))
    with pytest.raises(TodoSnapshotError):
        db.get_current_todo_snapshot("s")


@pytest.mark.parametrize("mutation", [
    "UPDATE session_rewinds SET target_message_id=999",
    "UPDATE session_rewinds SET physical_watermark_id=999",
    "UPDATE session_rewinds SET state='unknown'",
])
def test_clear_root_binds_physical_rewind_coordinates(db, mutation):
    clear(db)
    compact(db, "child")
    db._execute_write(lambda c: c.execute(mutation))
    with pytest.raises(TodoSnapshotError):
        db.get_todo_recovery_state("child")


def test_clear_baseline_cannot_select_an_older_projection(db):
    clear(db)
    compact(db, "in_place")
    original = db.get_session("s")["todo_clear_baseline_operation_id"]
    compact(db, "in_place")
    db._execute_write(lambda c: c.execute(
        "UPDATE sessions SET todo_clear_baseline_operation_id=? WHERE id='s'", (original,)))
    with pytest.raises(TodoSnapshotError):
        db.get_todo_recovery_state("s")


def test_new_selected_child_does_not_inherit_old_clear_baseline(db):
    clear(db)
    compact(db, "in_place")
    selected = write(db, "new")
    compact(db, "child")
    assert db.get_session("child")["todo_clear_baseline_operation_id"] is None
    assert db.get_current_todo_snapshot("child")["todos"] == selected["todos"]


def test_read_only_legacy_schema_rejects_without_migration(db):
    legacy_table(db)
    with SessionDB(db.db_path, read_only=True) as reopened:
        with pytest.raises(TodoSnapshotError, match="unsupported_schema"):
            reopened.get_todo_recovery_state("s")


SCHEMA_DRIFT = [
    ("source_current_snapshot_id INTEGER", "source_current_snapshot_id TEXT"),
    ("operation_id TEXT PRIMARY KEY", "operation_id TEXT"),
    ("UNIQUE(destination_session_id, boundary_message_id)", "UNIQUE(destination_session_id)"),
    ("ON DELETE CASCADE", "ON DELETE RESTRICT"),
    ("kind TEXT NOT NULL", "kind TEXT"),
    ("DEFAULT 'snapshot'", "DEFAULT 'unknown'"),
    ("source_clear_operation_id TEXT", "source_clear_operation_id TEXT, unknown_future_column TEXT"),
]


def drift_operation_schema(db, before, after, legacy):
    """Rebuild only a disposable fixture; no writable_schema escape hatch."""
    if legacy:
        legacy_table(db)
    ddl = db._conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='todo_lifecycle_operations'"
    ).fetchone()[0]
    # The explicit legacy fixture uses compact spacing.
    ddl = ddl.replace("UNIQUE(destination_session_id,boundary_message_id)",
                      "UNIQUE(destination_session_id, boundary_message_id)")
    assert before in ddl
    columns = [r[1] for r in db._conn.execute("PRAGMA table_info(todo_lifecycle_operations)")]
    rows = [tuple(r) for r in db._conn.execute("SELECT * FROM todo_lifecycle_operations")]
    db._conn.execute("DROP TABLE todo_lifecycle_operations")
    db._conn.execute(ddl.replace(before, after))
    db._conn.executemany(
        "INSERT INTO todo_lifecycle_operations (" + ','.join(columns) + ") VALUES ("
        + ','.join('?' for _ in columns) + ")", rows,
    )
    db._conn.commit()


@pytest.mark.parametrize("before,after", SCHEMA_DRIFT)
@pytest.mark.parametrize("reader", ["read_only", "writable"])
def test_unknown_current_shape_rejected_without_mutation(db, before, after, reader):
    write(db, "first")
    compact(db, "in_place")
    drift_operation_schema(db, before, after, legacy=False)
    original = list(db._conn.iterdump())
    if reader == "read_only":
        with SessionDB(db.db_path, read_only=True) as reopened:
            with pytest.raises(TodoSnapshotError, match="unsupported_schema"):
                reopened.get_todo_recovery_state("s")
    else:
        with pytest.raises(sqlite3.OperationalError, match="unsupported todo lifecycle schema"):
            with SessionDB(db.db_path):
                pass
    assert list(db._conn.iterdump()) == original


@pytest.mark.parametrize("before,after", SCHEMA_DRIFT[:5])
def test_unknown_legacy_shape_is_not_laundered_by_migration(db, before, after):
    write(db, "first")
    compact(db, "in_place")
    drift_operation_schema(db, before, after, legacy=True)
    original = list(db._conn.iterdump())
    with pytest.raises(sqlite3.OperationalError, match="unsupported todo lifecycle schema"):
        with SessionDB(db.db_path):
            pass
    assert list(db._conn.iterdump()) == original


@pytest.mark.parametrize("session", ["s", "missing"])
def test_schema_evidence_is_returned_even_without_selected_session(db, session):
    from hermes_state_schema import _todo_lifecycle_shape

    class CursorWitness:
        def fetchall(self):
            assert cursor is not None
            rows = cursor.fetchall()
            observed.extend(rows)
            return rows

    class ConnectionWitness:
        def __getattr__(self, name):
            return getattr(db._conn, name)

        def execute(self, sql, *args):
            nonlocal cursor
            cursor = db._conn.execute(sql, *args)
            return CursorWitness() if sql.startswith("WITH RECURSIVE ") else cursor

    observed, cursor = [], None
    expected = _todo_lifecycle_shape(db._conn)
    db._todo_snapshot_row_on_conn(ConnectionWitness(), session)
    assert observed
    import json
    assert tuple(tuple(c) for c in json.loads(observed[0]["lifecycle_columns"])) == expected[0]


@pytest.mark.parametrize("session", ["s", "missing"])
def test_drift_never_becomes_missing_session(db, session):
    drift_operation_schema(db, "kind TEXT NOT NULL", "kind TEXT", legacy=False)
    with pytest.raises(TodoSnapshotError, match="unsupported_schema"):
        db.get_todo_recovery_state(session)


def test_schema_and_pointer_share_one_wal_statement_snapshot(db):
    import json
    import threading
    from concurrent.futures import ThreadPoolExecutor

    first = write(db, "first")
    second = write(db, "second", prior=first["snapshot_id"])
    # Disposable fixture: two legitimate versions; select the older epoch.
    db._conn.execute("UPDATE sessions SET todo_current_snapshot_id=? WHERE id='s'",
                     (first["snapshot_id"],))
    db._conn.commit()
    assert db._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    entered, finished = threading.Event(), threading.Event()
    projected = []

    def gate(columns):
        projected.append(json.loads(columns))
        entered.set()
        assert finished.wait(10), "writer did not complete while statement held its snapshot"
        return 1

    def mutate():
        try:
            assert entered.wait(10), "owner statement never evaluated schema projection"
            with sqlite3.connect(db.db_path, timeout=5) as writer:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("ALTER TABLE todo_lifecycle_operations ADD COLUMN future_field TEXT")
                writer.execute("UPDATE sessions SET todo_current_snapshot_id=? WHERE id='s'",
                               (second["snapshot_id"],))
                writer.commit()
        finally:
            finished.set()

    # Test-only instrumentation of the real owner query; no production hook.
    class GatedConnection:
        def __getattr__(self, name):
            return getattr(db._conn, name)

        def execute(self, sql, *args):
            if sql.startswith("WITH RECURSIVE "):
                sql = sql.replace("SELECT t.*,", "SELECT todo_schema_gate(lifecycle_columns),t.*,", 1)
            return db._conn.execute(sql, *args)

    db._conn.create_function("todo_schema_gate", 1, gate)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(mutate)
        try:
            row = db._todo_snapshot_row_on_conn(GatedConnection(), "s")
            future.result(timeout=15)
        finally:
            entered.set()
            db._conn.create_function("todo_schema_gate", 1, None)
    assert projected and all(c[-1][1] == "source_clear_operation_id" for c in projected)
    assert row["current_id"] == first["snapshot_id"]
    assert db._conn.execute("SELECT todo_current_snapshot_id FROM sessions WHERE id='s'").fetchone()[0] == second["snapshot_id"]
    with pytest.raises(TodoSnapshotError, match="unsupported_schema"):
        db.get_todo_recovery_state("s")
