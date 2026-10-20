"""Scratch SessionDB owner boundaries; no runtime recovery claim."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

import hermes_state
from hermes_state import SessionDB
from tools.todo_tool import TodoStore


def payload(items=None):
    store = TodoStore()
    return json.dumps(store.write(items if items is not None else [
        {"id": "a", "content": "Task", "status": "pending"}
    ]), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@pytest.fixture
def owner(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s", source="test")
    rows = [{"role": "user", "content": "start"},
            {"role": "assistant", "content": "call", "tool_calls": [
                {"id": "model-id", "type": "function", "function": {"name": "todo", "arguments": "{}"}}
            ]}]
    db.append_messages_batch("s", rows)
    assert db.try_acquire_session_turn_lease("s", "test-holder", ttl_seconds=60)
    yield db, rows[-1]["_row_id"]
    db.close()


def commit(owner, **kwargs):
    db, anchor = owner
    args = dict(session_id="s", owner_execution_id="execution-1",
                anchor_assistant_row_id=anchor, expected_head_id=anchor,
                turn_lease_holder="test-holder", expected_prior_snapshot_id=None,
                todos_json=payload())
    args.update(kwargs)
    return db.commit_todo_snapshot(**args)


def refused(fn, reason):
    # Resolve at runtime so the pre-implementation RED reaches missing owner API.
    with pytest.raises(hermes_state.TodoSnapshotError) as exc:
        fn()
    assert exc.value.reason == reason


def test_missing_imported_pair_and_committed_empty_are_distinct(owner):
    db, anchor = owner
    assert db.get_current_todo_snapshot("s") is None
    db.append_message("s", "tool", '{"todos": []}', tool_call_id="model-id", tool_name="todo")
    assert db.get_current_todo_snapshot("s") is None
    head = db.get_messages("s")[-1]["id"]
    first = commit(owner, expected_head_id=head)
    empty = commit(owner, owner_execution_id="execution-2", expected_head_id=head,
                   expected_prior_snapshot_id=first["snapshot_id"], todos_json="[]")
    assert empty["todos"] == [] and empty["supersedes_snapshot_id"] == first["snapshot_id"]
    assert db.get_current_todo_snapshot("s") == empty
    assert empty["anchor_message_id"] == anchor
    assert empty["producer"] == "todo_dispatch_v1"


def test_snapshot_survives_reopen_and_later_ordinary_rows(owner):
    db, _ = owner
    snapshot = commit(owner)
    db.append_message("s", "user", "next")
    with SessionDB(db.db_path, read_only=True) as reopened:
        assert reopened.get_current_todo_snapshot("s") == snapshot


def test_same_execution_retry_is_idempotent_but_not_latest_override(owner):
    db, _ = owner
    first = commit(owner)
    assert commit(owner) == first
    second = commit(owner, owner_execution_id="execution-2", todos_json="[]",
                    expected_prior_snapshot_id=first["snapshot_id"])
    refused(lambda: commit(owner), "stale_version")
    assert db.get_current_todo_snapshot("s") == second


@pytest.mark.parametrize("change,reason", [
    ({"turn_lease_holder": "foreign"}, "lease"),
    ({"turn_lease_holder": ""}, "lease"),
    ({"expected_head_id": 999}, "head"),
    ({"anchor_assistant_row_id": 999}, "anchor"),
    ({"expected_prior_snapshot_id": 999}, "stale_version"),
    ({"owner_execution_id": ""}, "execution_id"),
    ({"owner_execution_id": "x" * 257}, "execution_id"),
    ({"todos_json": "[ ]"}, "payload"),
    ({"todos_json": '[{"id":"x","content":"x","status":"unknown"}]'}, "payload"),
    ({"todos_json": "{}"}, "payload"),
    ({"todos_json": " " * 512001}, "payload"),
])
def test_commit_denials_leave_no_version(owner, change, reason):
    refused(lambda: commit(owner, **change), reason)
    assert owner[0].get_current_todo_snapshot("s") is None


def test_reused_execution_mismatch_cannot_overwrite(owner):
    first = commit(owner)
    refused(lambda: commit(owner, todos_json="[]"), "execution_conflict")
    assert owner[0].get_current_todo_snapshot("s") == first


@pytest.mark.parametrize("mutation,reason", [
    ("UPDATE session_turn_leases SET expires_at=0", "lease"),
    ("DELETE FROM session_turn_leases", "lease"),
    ("UPDATE messages SET role='user' WHERE role='assistant'", "anchor"),
    ("UPDATE messages SET active=0 WHERE role='assistant'", "head"),
    ("UPDATE messages SET compacted=1 WHERE role='assistant'", "head"),
])
def test_expired_or_changed_scope_cannot_commit(owner, mutation, reason):
    db, _ = owner
    db._execute_write(lambda conn: conn.execute(mutation))
    refused(lambda: commit(owner), reason)


@pytest.mark.parametrize("mutation,reason", [
    ("UPDATE todo_snapshots SET schema_version=99", "schema"),
    ("UPDATE todo_snapshots SET producer='import'", "producer"),
    ("UPDATE todo_snapshots SET todos_json='[ ]'", "payload"),
    ("UPDATE sessions SET rewind_count=rewind_count+1", "causal_scope"),
    ("UPDATE messages SET active=0 WHERE role='assistant'", "causal_scope"),
    ("UPDATE messages SET compacted=1 WHERE role='assistant'", "causal_scope"),
    ("UPDATE sessions SET ended_at=1,end_reason='compression'", "closed_session"),
    ("UPDATE todo_snapshots SET supersedes_snapshot_id=999", "predecessor"),
])
def test_latest_invalid_snapshot_never_falls_back(owner, mutation, reason):
    db, _ = owner
    commit(owner)
    db._execute_write(lambda conn: conn.execute(mutation))
    refused(lambda: db.get_current_todo_snapshot("s"), reason)


def test_cross_session_anchor_and_snapshot_are_rejected(owner):
    db, anchor = owner
    db.create_session("other", source="test")
    other_anchor = db.append_message("other", "assistant", "other")
    refused(lambda: commit(owner, anchor_assistant_row_id=other_anchor), "anchor")
    first = commit(owner)
    assert db.get_current_todo_snapshot("other") is None
    db._execute_write(lambda conn: conn.execute(
        "UPDATE todo_snapshots SET anchor_message_id=? WHERE snapshot_id=?",
        (other_anchor, first["snapshot_id"])))
    refused(lambda: db.get_current_todo_snapshot("s"), "causal_scope")


def test_real_transaction_abort_publishes_no_snapshot(owner):
    db, _ = owner
    # First call establishes that the API exists before installing the trigger.
    assert db.get_current_todo_snapshot("s") is None
    db._execute_write(lambda conn: conn.execute(
        "CREATE TRIGGER refuse_todo BEFORE INSERT ON todo_snapshots "
        "BEGIN SELECT RAISE(ABORT, 'injected snapshot failure'); END"))
    with pytest.raises(sqlite3.IntegrityError, match="injected snapshot failure"):
        commit(owner)
    assert db.get_current_todo_snapshot("s") is None


def test_two_connections_same_predecessor_only_one_commits(owner):
    db, anchor = owner
    other = SessionDB(db.db_path)
    barrier = Barrier(2)
    def attempt(store, execution):
        barrier.wait(timeout=5)
        try:
            return commit((store, anchor), owner_execution_id=execution)
        except hermes_state.TodoSnapshotError as exc:
            return exc.reason
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda pair: attempt(*pair), [(db, "one"), (other, "two")]))
        assert sum(isinstance(result, dict) for result in results) == 1
        assert results.count("stale_version") == 1
        assert db.get_current_todo_snapshot("s")["snapshot_id"] == next(
            result["snapshot_id"] for result in results if isinstance(result, dict))
    finally:
        other.close()


def test_snapshot_commit_never_implicitly_renews_lease(owner, monkeypatch):
    """The existing transcript guard can renew; this owner API must not."""
    db, _ = owner
    clock = [100.0]
    real_time = hermes_state.time

    class Clock:
        def time(self):
            return clock[0]

        def __getattr__(self, name):
            return getattr(real_time, name)

    db._execute_write(lambda conn: conn.execute(
        "UPDATE session_turn_leases SET expires_at=101"))
    monkeypatch.setattr(hermes_state, "time", Clock())
    guard = db._check_transcript_write_guards

    def cross_expiry(*args, **kwargs):
        clock[0] = 102.0
        return guard(*args, **kwargs)

    monkeypatch.setattr(db, "_check_transcript_write_guards", cross_expiry)
    refused(lambda: commit(owner), "lease")
    assert db.get_current_todo_snapshot("s") is None
    with db._read_ctx() as conn:
        assert conn.execute(
            "SELECT expires_at FROM session_turn_leases"
        ).fetchone()[0] == 101.0


def test_old_read_only_schema_is_explicitly_unsupported(owner):
    db, _ = owner
    assert db.get_current_todo_snapshot("s") is None
    db._execute_write(lambda conn: conn.execute("DROP TABLE todo_snapshots"))
    with SessionDB(db.db_path, read_only=True) as legacy:
        refused(lambda: legacy.get_current_todo_snapshot("s"), "unsupported_schema")
    with SessionDB(db.db_path) as upgraded:
        assert upgraded.get_current_todo_snapshot("s") is None
