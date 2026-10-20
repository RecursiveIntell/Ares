"""Row coordinates are projections of committed writes, never authority.

Actual SessionDB transactions, including SQL constraint failures and an injected
commit failure on its connection. No live database or provider is accessed.
"""
import copy
import sqlite3

import pytest

from hermes_state import SessionDB


OPERATIONS = ["append", "replace", "compact", "child"]


@pytest.fixture
def db(tmp_path):
    owner = SessionDB(db_path=tmp_path / "state.db")
    owner.create_session("parent", source="test")
    owner.append_message("parent", "user", "retained original")
    assert owner.try_acquire_compression_lock("parent", "owner", ttl_seconds=60)
    yield owner
    owner.close()


def write(db, operation, rows):
    if operation == "append":
        return db.append_messages_batch("parent", rows, compression_lock_holder="owner")
    if operation == "replace":
        return db.replace_messages("parent", rows)
    if operation == "compact":
        return db.archive_and_compact("parent", rows, lock_holder="owner")
    return db.publish_compression_child(
        parent_session_id="parent", child_session_id="child", source="test",
        messages=rows, compression_lock_holder="owner",
    )


def canonical_state(db):
    return {
        table: [tuple(row) for row in db._conn.execute(f"SELECT * FROM {table} ORDER BY id")]
        for table in ("sessions", "messages")
    }


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("failure", ["second_insert", "counter", "commit"])
def test_failed_write_does_not_publish_coordinates(db, monkeypatch, operation, failure):
    rows = [{"role": "user", "content": "first", "_row_id": -77},
            {"role": "assistant", "content": "reject"}]
    before_rows = copy.deepcopy(rows)
    before_db = canonical_state(db)
    if failure == "second_insert":
        db._conn.execute("""CREATE TRIGGER reject_row BEFORE INSERT ON messages
            WHEN NEW.content = 'reject'
            BEGIN SELECT RAISE(ABORT, 'injected insert failure'); END""")
    elif failure == "counter":
        db._conn.execute("""CREATE TRIGGER reject_counter BEFORE UPDATE OF message_count ON sessions
            WHEN NEW.message_count > 0
            BEGIN SELECT RAISE(ABORT, 'injected counter failure'); END""")
    else:
        def reject_commit():
            raise sqlite3.OperationalError("injected commit failure")
        monkeypatch.setattr(db._conn, "commit", reject_commit)
    with pytest.raises(sqlite3.Error, match="injected"):
        write(db, operation, rows)
    assert canonical_state(db) == before_db, "owner transaction did not roll back"
    assert rows == before_rows, "rolled-back coordinates escaped onto caller messages"


@pytest.mark.parametrize("operation", OPERATIONS)
def test_success_publishes_exact_committed_coordinates(db, operation):
    rows = [{"role": "user", "content": "same bytes", "_row_id": -77},
            {"role": "assistant", "content": "same bytes"}]
    result = write(db, operation, rows)
    target = "child" if operation == "child" else "parent"
    stored = db.get_messages(target)[-2:]
    assert [row["_row_id"] for row in rows] == [row["id"] for row in stored]
    assert [row["role"] for row in stored] == ["user", "assistant"]
    assert result == (2 if operation in {"append", "compact"} else None)


def test_chunk_failure_publishes_only_committed_prefix(db):
    rows = [{"role": "user", "content": "committed"},
            {"role": "user", "content": "rolled back"},
            {"role": "assistant", "content": "reject"}]
    db._conn.execute("""CREATE TRIGGER reject_row BEFORE INSERT ON messages
        WHEN NEW.content = 'reject'
        BEGIN SELECT RAISE(ABORT, 'injected insert failure'); END""")
    # First two-row chunk commits; add a fourth row so the second chunk has
    # one provisional insert before the rejecting row.
    rows.insert(2, {"role": "user", "content": "provisional"})
    with pytest.raises(sqlite3.Error, match="injected"):
        db.append_messages_batch("parent", rows, compression_lock_holder="owner", chunk_rows=2)
    stored = db.get_messages("parent")
    assert [r["_row_id"] for r in rows[:2]] == [r["id"] for r in stored[-2:]]
    assert all("_row_id" not in row for row in rows[2:])
    assert len(stored) == 3


def test_retry_does_not_expose_failed_attempt_coordinates(db, monkeypatch):
    rows = [{"role": "user", "content": "one"}]
    real_commit = db._conn.commit
    attempts = []

    def commit_once_busy():
        attempts.append(copy.deepcopy(rows))
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_commit()

    monkeypatch.setattr(db._conn, "commit", commit_once_busy)
    assert write(db, "append", rows) == 1
    assert len(attempts) == 2
    assert all("_row_id" not in attempt[0] for attempt in attempts)
    assert rows[0]["_row_id"] == db.get_messages("parent")[-1]["id"]


def test_insert_helper_returns_provisional_ids_without_mutating_inputs(db):
    rows = [{"role": "user", "content": "provisional", "_row_id": -77}]
    before = copy.deepcopy(rows)
    before_db = canonical_state(db)
    db._conn.execute("BEGIN IMMEDIATE")
    try:
        inserted, tool_calls, row_ids = db._insert_message_rows(db._conn, "parent", rows)
        assert inserted == 1 and tool_calls == 0
        assert row_ids == (db._conn.execute("SELECT MAX(id) FROM messages").fetchone()[0],)
        assert rows == before
    finally:
        db._conn.rollback()
    assert canonical_state(db) == before_db
    assert rows == before


def test_import_keeps_caller_coordinates_and_commits_content(db):
    payload = [{"id": "imported", "source": "test", "messages": [
        {"role": "user", "content": "imported request", "_row_id": -77},
        {"role": "assistant", "content": "imported answer"},
    ]}]
    before = copy.deepcopy(payload)
    result = db.import_sessions(payload)
    assert result["ok"] is True and result["imported_ids"] == ["imported"]
    stored = db.get_messages("imported")
    assert [(row["role"], row["content"]) for row in stored] == [
        ("user", "imported request"), ("assistant", "imported answer"),
    ]
    assert all(row["id"] > 0 for row in stored)
    assert db.get_session("imported")["message_count"] == 2
    assert payload == before, "import must not stamp its normalized copies onto caller data"


def test_repeated_dict_preserves_last_coordinate_projection(db):
    row = {"role": "user", "content": "same instance"}
    assert write(db, "append", [row, row]) == 2
    stored = db.get_messages("parent")
    assert stored[-1]["id"] != stored[-2]["id"]
    assert row["_row_id"] == stored[-1]["id"]


@pytest.mark.parametrize("operation", ["compact", "child"])
def test_tail_clone_does_not_shift_input_coordinate_mapping(db, operation):
    watermark = db.get_active_message_watermark("parent")
    db.append_message("parent", "user", "concurrent tail", compression_lock_holder="owner")
    rows = [{"role": "user", "content": "handoff", "_row_id": -77}]
    if operation == "compact":
        assert db.archive_and_compact("parent", rows, watermark=watermark, lock_holder="owner") == 2
    else:
        db.publish_compression_child(
            parent_session_id="parent", child_session_id="child", source="test",
            messages=rows, watermark=watermark, compression_lock_holder="owner",
        )
    target = "child" if operation == "child" else "parent"
    stored = db.get_messages(target)
    assert [row["content"] for row in stored] == ["handoff", "concurrent tail"]
    assert rows[0]["_row_id"] == stored[0]["id"]
