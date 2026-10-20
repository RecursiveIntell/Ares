"""Scratch-database witnesses for exact owner rewind/restore membership."""
import os

import pytest

from hermes_state import (
    CompressionSessionClosedError,
    SessionCompressionInProgressError,
    SessionDB,
    SessionTurnLeaseLostError,
)


@pytest.fixture
def db(tmp_path):
    state = SessionDB(db_path=tmp_path / "state.db")
    state.create_session("s", source="tui")
    yield state
    state.close()


def seed(db):
    first = db.append_message("s", "user", "first")
    target = db.append_message("s", "user", "second")
    call = db.append_message("s", "assistant", None, tool_calls=[{"id": "c", "function": {"name": "todo"}}])
    result = db.append_message("s", "tool", "ok", tool_call_id="c")
    return first, target, call, result


def counts(db):
    return tuple(db._conn.execute(
        "SELECT message_count,tool_call_count,rewind_count FROM sessions WHERE id='s'"
    ).fetchone())


def reject(db, target, reason):
    before = db.get_active_message_ids("s"), counts(db)
    with pytest.raises(RuntimeError) as error:
        db.restore_rewound("s", target)
    assert type(error.value).__name__ == "RewindRestoreError"
    assert error.value.reason == reason
    assert (db.get_active_message_ids("s"), counts(db)) == before


def test_restore_does_not_resurrect_an_older_rewind(db):
    first, target, call, result = seed(db)
    db.rewind_to_message("s", target)
    # A later operation removes only first, not the older inactive tail.
    db.rewind_to_message("s", first)
    assert db.restore_rewound("s", first) == 1
    assert db.get_active_message_ids("s") == [first]
    assert counts(db) == (1, 0, 2)


def test_restore_reconciles_counters_and_survives_reopen(db):
    ids = seed(db)
    db.rewind_to_message("s", ids[1])
    assert counts(db) == (1, 0, 1)
    path = db.db_path
    db.close()
    reopened = SessionDB(db_path=path)
    try:
        assert reopened.restore_rewound("s", ids[1]) == 3
        assert reopened.get_active_message_ids("s") == list(ids)
        assert counts(reopened) == (4, 1, 1)
        assert reopened.restore_rewound("s", ids[1]) == 0
    finally:
        reopened.close()


def test_append_refuses_stale_restore_and_stale_retry(db):
    ids = seed(db)
    db.rewind_to_message("s", ids[1])
    db.append_message("s", "user", "new")
    reject(db, ids[1], "watermark_mismatch")


def test_unknown_operation_cannot_restore_archived_rows(db):
    ids = seed(db)
    db._conn.execute("UPDATE messages SET active=0,compacted=1 WHERE id=?", (ids[1],))
    db._conn.commit()
    reject(db, ids[1], "unknown_operation")


@pytest.mark.parametrize("guard", ["lease", "compression", "closed"])
def test_restore_obeys_owner_guards(db, guard):
    ids = seed(db)
    db.rewind_to_message("s", ids[1])
    if guard == "lease":
        assert db.try_acquire_session_turn_lease("s", f"pid={os.getpid()}:turn=busy", ttl_seconds=60)
        error = SessionTurnLeaseLostError
    elif guard == "compression":
        assert db.try_acquire_compression_lock("s", "compressor", ttl_seconds=60)
        error = SessionCompressionInProgressError
    else:
        db.end_session("s", "compression")
        error = CompressionSessionClosedError
    before = db.get_active_message_ids("s"), counts(db)
    with pytest.raises(error):
        db.restore_rewound("s", ids[1])
    assert (db.get_active_message_ids("s"), counts(db)) == before


def test_composite_restore_deactivates_only_inserted_scaffold(db):
    from agent.context_compressor import HISTORICAL_TASK_HEADING, SUMMARY_PREFIX, _SUMMARY_END_MARKER
    first = db.append_message("s", "user", "first")
    carrier = f"{SUMMARY_PREFIX}\n{HISTORICAL_TASK_HEADING}\nold task\n\n{_SUMMARY_END_MARKER}\n\nREAL ASK"
    target = db.append_message("s", "user", carrier)
    last = db.append_message("s", "assistant", "failed")
    outcome = db.rewind_to_message("s", target, preserve_compaction_handoff=True)
    assert db.get_active_message_ids("s") == [first, outcome["replacement_message_id"]]
    assert db.restore_rewound("s", target) == 2
    assert db.get_active_message_ids("s") == [first, target, last]
    assert counts(db) == (3, 0, 1)
    assert db.restore_rewound("s", target) == 0


def test_repeated_noop_rewind_cannot_reveal_older_members(db):
    ids = seed(db)
    db.rewind_to_message("s", ids[1])
    db.rewind_to_message("s", ids[1])
    assert db.restore_rewound("s", ids[1]) == 0
    assert db.get_active_message_ids("s") == [ids[0]]
    assert counts(db) == (1, 0, 2)


def test_rewind_inactive_target_restores_only_newly_active_tail(db):
    ids = seed(db)
    db.rewind_to_message("s", ids[1])
    added = db.append_message("s", "user", "new tail")
    db.rewind_to_message("s", ids[1])
    assert db.restore_rewound("s", ids[1]) == 1
    assert db.get_active_message_ids("s") == [ids[0], added]
    assert counts(db) == (2, 0, 2)
    assert db.restore_rewound("s", ids[1]) == 0


def operation(db):
    return dict(db._conn.execute(
        "SELECT * FROM session_rewinds WHERE session_id='s' ORDER BY rewind_count DESC LIMIT 1"
    ).fetchone())


def physical(db):
    return [tuple(row) for row in db._conn.execute(
        "SELECT * FROM messages WHERE session_id='s' ORDER BY id"
    )]


@pytest.mark.parametrize("column,value,reason", [
    ("schema_version", 99, "unsupported_schema"),
    ("state", "future", "malformed_membership"),
    ("target_was_active", -1, "malformed_membership"),
    ("target_was_active", 2, "malformed_membership"),
    ("target_was_active", "broken", "malformed_membership"),
    ("target_was_active", 0.5, "malformed_membership"),
    ("target_was_active", 0, "malformed_membership"),
    ("removed_message_ids", "broken", "malformed_membership"),
    ("removed_message_ids", "{}", "malformed_membership"),
    ("removed_message_ids", "[2,2]", "malformed_membership"),
    ("removed_message_ids", "[3,4]", "malformed_membership"),
    ("removed_message_ids", "[]", "malformed_membership"),
    ("removed_message_ids", "[3,2]", "malformed_membership"),
    ("removed_message_ids", "[true]", "malformed_membership"),
    ("removed_message_ids", "[2.0]", "malformed_membership"),
    ("removed_message_ids", "[0]", "malformed_membership"),
    ("removed_message_ids", "[2, 3, 4]", "malformed_membership"),
    ("post_rewind_active_ids", "[1,2]", "malformed_membership"),
    ("replacement_message_id", 1, "malformed_membership"),
    ("physical_watermark_id", 1, "malformed_membership"),
])
def test_corrupt_operation_refuses_without_changes(db, column, value, reason):
    ids = seed(db)
    db.rewind_to_message("s", ids[1])
    # Test-owned scratch corruption, never an adapter repair of live data.
    db._conn.execute(f"UPDATE session_rewinds SET {column}=?", (value,))
    db._conn.commit()
    before = physical(db), operation(db)
    reject(db, ids[1], reason)
    assert (physical(db), operation(db)) == before


@pytest.mark.parametrize("mutation,reason", [
    ("delete", "missing_member"),
    ("move", "missing_member"),
    ("activate", "membership_state_mismatch"),
    ("prefix", "active_projection_mismatch"),
    ("counter", "not_latest"),
])
def test_member_damage_and_version_drift_refuse(db, mutation, reason):
    ids = seed(db)
    db.rewind_to_message("s", ids[1])
    if mutation == "delete":
        db._conn.execute("DELETE FROM messages WHERE id=?", (ids[2],))
    elif mutation == "move":
        db.create_session("other", source="tui")
        db._conn.execute("UPDATE messages SET session_id='other' WHERE id=?", (ids[2],))
    elif mutation == "activate":
        db._conn.execute("UPDATE messages SET active=1 WHERE id=?", (ids[2],))
    elif mutation == "prefix":
        db._conn.execute("UPDATE messages SET active=0 WHERE id=?", (ids[0],))
    else:
        db._conn.execute("UPDATE sessions SET rewind_count=rewind_count+1 WHERE id='s'")
    db._conn.commit()
    before = physical(db), operation(db)
    reject(db, ids[1], reason)
    assert (physical(db), operation(db)) == before


@pytest.mark.parametrize("mutation,reason", [
    ("append", "watermark_mismatch"),
    ("prefix", "active_projection_mismatch"),
])
def test_idempotent_retry_validates_entire_restored_projection(db, mutation, reason):
    ids = seed(db)
    db.rewind_to_message("s", ids[1])
    assert db.restore_rewound("s", ids[1]) == 3
    if mutation == "append":
        db.append_message("s", "user", "new")
    else:
        db._conn.execute("UPDATE messages SET active=0 WHERE id=?", (ids[0],))
        db._conn.commit()
    reject(db, ids[1], reason)


def test_wrong_target_and_session_cannot_select_historical_operation(db):
    ids = seed(db)
    db.rewind_to_message("s", ids[1])
    reject(db, ids[0], "target_mismatch")
    db.create_session("other", source="tui")
    from hermes_state import RewindRestoreError
    with pytest.raises(RewindRestoreError, match="unknown_operation"):
        db.restore_rewound("other", ids[1])
    assert db.get_active_message_ids("s") == [ids[0]]


@pytest.mark.parametrize("stage", ["row", "counter", "operation"])
def test_restore_transaction_failure_rolls_back_all_effects(db, stage):
    import sqlite3
    ids = seed(db)
    db.rewind_to_message("s", ids[1])
    table, column = {"row": ("messages", "active"),
                     "counter": ("sessions", "message_count"),
                     "operation": ("session_rewinds", "state")}[stage]
    db._conn.execute(f"CREATE TRIGGER abort_restore BEFORE UPDATE OF {column} ON {table} "
                     "BEGIN SELECT RAISE(ABORT, 'injected restore failure'); END")
    db._conn.commit()
    before = physical(db), counts(db), operation(db)
    with pytest.raises(sqlite3.IntegrityError, match="injected restore failure"):
        db.restore_rewound("s", ids[1])
    assert (physical(db), counts(db), operation(db)) == before


def test_rewind_membership_insert_failure_rolls_back_rows_and_counters(db):
    import sqlite3
    ids = seed(db)
    db._conn.execute("CREATE TRIGGER abort_rewind BEFORE INSERT ON session_rewinds "
                     "BEGIN SELECT RAISE(ABORT, 'injected rewind failure'); END")
    db._conn.commit()
    before = physical(db), counts(db)
    with pytest.raises(sqlite3.IntegrityError, match="injected rewind failure"):
        db.rewind_to_message("s", ids[1])
    assert (physical(db), counts(db)) == before
    assert db._conn.execute("SELECT COUNT(*) FROM session_rewinds").fetchone()[0] == 0


def test_membership_survives_replacement_but_cannot_restore_it(db):
    ids = seed(db)
    db.rewind_to_message("s", ids[1])
    old_operation = operation(db)
    db.replace_messages("s", [{"role": "user", "content": "replacement"}])
    assert operation(db) == old_operation
    reject(db, ids[1], "missing_member")


def test_owner_records_target_prior_state_and_refuses_null(db):
    import sqlite3
    ids = seed(db)
    db.rewind_to_message("s", ids[1])
    assert operation(db)["target_was_active"] == 1
    before = operation(db)
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        db._conn.execute("UPDATE session_rewinds SET target_was_active=NULL")
    db._conn.rollback()
    assert operation(db) == before
    db.rewind_to_message("s", ids[1])
    assert operation(db)["target_was_active"] == 0


@pytest.mark.parametrize("restored", [False, True])
def test_inactive_target_must_remain_inactive_on_restore_and_retry(db, restored):
    ids = seed(db)
    db.rewind_to_message("s", ids[1])
    db.rewind_to_message("s", ids[1])
    if restored:
        assert db.restore_rewound("s", ids[1]) == 0
    db._conn.execute("UPDATE messages SET active=1 WHERE id=?", (ids[1],))
    db._conn.commit()
    before = physical(db), operation(db)
    reject(db, ids[1], "membership_state_mismatch")
    assert (physical(db), operation(db)) == before


def test_inactive_target_coordinate_cannot_claim_active_membership(db):
    ids = seed(db)
    db.rewind_to_message("s", ids[1])
    db.rewind_to_message("s", ids[1])
    db._conn.execute("UPDATE session_rewinds SET target_was_active=1 WHERE rewind_count=2")
    db._conn.commit()
    before = physical(db), operation(db)
    reject(db, ids[1], "malformed_membership")
    assert (physical(db), operation(db)) == before


@pytest.mark.parametrize("column", [
    "session_id", "rewind_count", "schema_version", "target_message_id",
    "target_was_active", "removed_message_ids", "replacement_message_id",
    "post_rewind_active_ids", "physical_watermark_id", "state", "restored_at",
])
@pytest.mark.parametrize("restored", [False, True])
def test_missing_restore_column_refuses_with_typed_reason(db, column, restored):
    ids = seed(db)
    db.rewind_to_message("s", ids[1])
    if restored:
        assert db.restore_rewound("s", ids[1]) == 3
    # Corrupt only this test-owned scratch schema, retaining the original values.
    db._conn.execute(f"ALTER TABLE session_rewinds RENAME COLUMN {column} TO missing_column")
    db._conn.commit()
    def stored_rows():
        return [tuple(row) for row in db._conn.execute("SELECT * FROM session_rewinds")]
    before = physical(db), counts(db), stored_rows()
    reject(db, ids[1], "unsupported_schema")
    assert (physical(db), counts(db), stored_rows()) == before


def test_legacy_store_migration_does_not_invent_membership(db):
    from hermes_state_common import SCHEMA_VERSION
    ids = seed(db)
    path = db.db_path
    db._conn.execute("UPDATE messages SET active=0 WHERE id>=?", (ids[1],))
    db._conn.execute("UPDATE sessions SET rewind_count=1 WHERE id='s'")
    db._conn.execute("DROP TABLE session_rewinds")
    db._conn.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION - 1,))
    db._conn.commit()
    db.close()
    reopened = SessionDB(db_path=path)
    try:
        reject(reopened, ids[1], "unknown_operation")
        assert reopened._conn.execute("SELECT COUNT(*) FROM session_rewinds").fetchone()[0] == 0
    finally:
        reopened.close()
