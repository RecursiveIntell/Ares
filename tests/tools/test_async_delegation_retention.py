"""Durable receipt pruning against a tiny in-memory ledger; no live DB."""
import json
import sqlite3
from contextlib import contextmanager

import pytest
from tools import async_delegation as subject

NOW = 200000.0

@pytest.fixture
def ledger(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE async_delegations (
      delegation_id TEXT PRIMARY KEY, origin_session TEXT, state TEXT,
      dispatched_at REAL, completed_at REAL, updated_at REAL, result_json TEXT,
      delivery_state TEXT, delivery_attempts INTEGER, origin_session_id TEXT,
      delivery_claim TEXT, event_json TEXT)""")
    @contextmanager
    def transaction():
        with conn:
            yield conn
    def forbid(*args, **kwargs):
        raise AssertionError("live database/worker seam must not run")
    monkeypatch.setattr(subject, "_transaction", transaction)
    monkeypatch.setattr(subject, "_connect", forbid)
    monkeypatch.setattr(subject, "_initialize_schema", forbid)
    monkeypatch.setattr(subject, "_get_executor", forbid)
    monkeypatch.setattr(subject, "recover_abandoned_delegations", lambda: 0)
    monkeypatch.setattr(subject.time, "time", lambda: NOW)
    yield conn
    conn.close()

def insert(conn, identifier, *, state="completed", delivery="pending", claim=None, age=0, compact=False):
    event = {"delegation_id": identifier, "type": "async_delegation", "summary": "tiny receipt"}
    result = {"summary": "tiny result"}
    if compact:
        event, result = {}, {}
    conn.execute("INSERT INTO async_delegations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
        identifier, "" if compact else "inert-origin", state, NOW - age, NOW - age,
        NOW - age, json.dumps(result), delivery, 0, "" if compact else "inert-session", claim, json.dumps(event)))
    conn.commit()

def snapshot(conn):
    return conn.execute("SELECT * FROM async_delegations ORDER BY delegation_id").fetchall()

@pytest.mark.parametrize("count", [51, 1001])
def test_history_cap_preserves_fresh_pending_completions(ledger, count):
    for i in range(count):
        insert(ledger, f"p-{i:04}", age=i, compact=count > 51)
    before = snapshot(ledger)
    subject._prune_durable_records()
    after = snapshot(ledger)
    print({"pre_count": len(before), "post_count": len(after), "lost": [r for r in before if r not in after]})
    assert after == before

def test_history_prune_preserves_claimed_and_live_rows(ledger):
    old = subject._DURABLE_RETENTION_SECONDS + 1
    cases = [
        ("pending", "completed", "pending", None),
        ("claimed-delivered", "completed", "delivered", "claim"),
        ("claimed-dropped", "completed", "dropped", "claim"),
        ("empty-claim", "completed", "delivered", ""),
        ("running", "running", "delivered", None),
        ("finalizing", "finalizing", "delivered", None),
        ("unknown-disposition", "completed", "unknown", None),
    ]
    for identifier, state, delivery, claim in cases:
        insert(ledger, identifier, state=state, delivery=delivery, claim=claim, age=old)
    before = snapshot(ledger)
    subject._prune_durable_records()
    assert snapshot(ledger) == before

def test_history_cap_prunes_only_unclaimed_terminal_history(ledger):
    for i in range(40):
        insert(ledger, f"delivered-{i:03}", delivery="delivered", age=100 - i)
    for i in range(20):
        insert(ledger, f"dropped-{i:03}", delivery="dropped", age=500 - i)
    insert(ledger, "protected-pending")
    insert(ledger, "protected-claim", delivery="delivered", claim="c")
    protected = [r for r in snapshot(ledger) if r[0].startswith("protected-")]
    subject._prune_durable_records()
    after = snapshot(ledger)
    assert len(after) == 52
    assert [r for r in after if r[0].startswith("protected-")] == protected
    assert not any(r[0] == "delivered-000" for r in after)
    assert all(any(r[0] == f"dropped-{i:03}" for r in after) for i in range(20))

def test_delivered_age_expiry_preserves_other_receipts(ledger):
    old = subject._DURABLE_RETENTION_SECONDS + 1
    insert(ledger, "expired", delivery="delivered", age=old)
    insert(ledger, "old-dropped", delivery="dropped", age=old)
    insert(ledger, "old-pending", age=old)
    insert(ledger, "old-claimed", delivery="delivered", claim="c", age=old)
    insert(ledger, "old-running", state="running", delivery="delivered", age=old)
    before = [r for r in snapshot(ledger) if r[0] != "expired"]
    subject._prune_durable_records()
    assert snapshot(ledger) == before

def test_pending_payload_survives_prune_lookup_and_replay(ledger):
    for i in range(51):
        insert(ledger, f"pending-{i:03}", age=i)
    before = snapshot(ledger)
    subject._prune_durable_records()
    for row in before:
        receipt = subject.get_durable_delegation(row[0])
        assert receipt is not None
        assert receipt["result"] == json.loads(row[6])
        assert receipt["delivery_state"] == "pending"
    class Queue:
        def __init__(self):
            self.events = []
        def put(self, event):
            self.events.append(event)
    queue = Queue()
    assert subject.restore_undelivered_completions(queue) == 51
    expected = [dict(json.loads(row[11]), restored=True) for row in before]
    assert sorted(queue.events, key=lambda event: event["delegation_id"]) == expected
    assert snapshot(ledger) == before


# SD03B controls deliberately avoid workers, providers and production state.db.
@pytest.fixture
def admission_ledger(monkeypatch):
    from gateway import status as process_status
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE async_delegations (
        delegation_id TEXT PRIMARY KEY, origin_session TEXT,
        origin_ui_session_id TEXT, parent_session_id TEXT, state TEXT,
        dispatched_at REAL, completed_at REAL, updated_at REAL,
        event_json TEXT, result_json TEXT, delivery_state TEXT,
        delivery_attempts INTEGER, delivered_at REAL, owner_pid INTEGER,
        owner_started_at INTEGER, task_json TEXT, delivery_claim TEXT,
        delivery_claimed_at REAL, origin_session_id TEXT)""")
    calls = {"executor": 0, "submit": 0, "monitor": 0, "runner": 0}
    captured = []
    trace = []
    conn.set_trace_callback(trace.append)

    @contextmanager
    def transaction():
        with conn:
            yield conn

    def forbid(*args, **kwargs):
        raise AssertionError("live DB/provider boundary must not run")

    class RecordingExecutor:
        def submit(self, callback):
            calls["submit"] += 1
            captured.append(callback)
            return object()

    executor = RecordingExecutor()
    def get_executor(_limit):
        calls["executor"] += 1
        return executor

    monkeypatch.setattr(subject, "_transaction", transaction)
    monkeypatch.setattr(subject, "_connect", forbid)
    monkeypatch.setattr(subject, "_initialize_schema", forbid)
    monkeypatch.setattr(subject, "_records", {})
    monkeypatch.setattr(subject, "_new_delegation_id", lambda: "sd03b-new")
    monkeypatch.setattr(subject, "_capture_routing_origin", lambda: {})
    monkeypatch.setattr(subject, "_get_executor", get_executor)
    monkeypatch.setattr(subject, "_ensure_stale_monitor", lambda: calls.__setitem__("monitor", calls["monitor"] + 1))
    monkeypatch.setattr(subject, "_prune_durable_records", lambda: None)
    monkeypatch.setattr(subject.time, "time", lambda: NOW)
    monkeypatch.setattr(process_status, "get_process_start_time", lambda _pid: 42)
    yield conn, calls, executor, captured, trace, transaction
    conn.close()


def _sd03b_seed(conn, count, mixed=False):
    rows = []
    for i in range(count):
        state = ("running", "finalizing", "completed")[i % 3] if mixed else "completed"
        claim = "held" if mixed and i % 2 else None
        rows.append((f"p-{i:04}", "fixture", state, NOW, NOW,
                     "pending", claim, "{}", "{}"))
    conn.executemany("""INSERT INTO async_delegations
        (delegation_id, origin_session, state, dispatched_at, updated_at,
         delivery_state, delivery_claim, event_json, result_json)
        VALUES (?,?,?,?,?,?,?,?,?)""", rows)
    conn.commit()


def _sd03b_dispatch(calls, batch=False, **overrides):
    def runner():
        calls["runner"] += 1
        raise AssertionError("inert executor must not run a provider")
    kwargs = dict(session_key="fixture", runner=runner, context=None,
                  toolsets=None, role="leaf", model=None,
                  progress_fn=lambda: (0, False))
    kwargs.update(overrides)
    if batch:
        return subject.dispatch_async_delegation_batch(goals=["inert"], **kwargs)
    return subject.dispatch_async_delegation(goal="inert", **kwargs)


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize("count", [999, 1000])
def test_sd03b_pending_admission_boundary(admission_ledger, batch, count):
    conn, calls, _executor, _captured, _trace, _transaction = admission_ledger
    assert subject._MAX_DURABLE_PENDING == 1000
    _sd03b_seed(conn, count)
    before = snapshot(conn)
    result = _sd03b_dispatch(calls, batch=batch)
    if count == 999:
        assert result["status"] == "dispatched"
        assert conn.execute("SELECT COUNT(*) FROM async_delegations WHERE delivery_state='pending'").fetchone()[0] == 1000
        assert calls == {"executor": 1, "submit": 1, "monitor": 1, "runner": 0}
    else:
        assert result["status"] == "rejected"
        assert result["error_code"] == "durable_backlog_full"
        assert result["execution_started"] is False
        assert snapshot(conn) == before
        assert calls == {"executor": 0, "submit": 0, "monitor": 0, "runner": 0}
        assert subject._records == {}


def test_sd03b_pending_active_and_claimed_rows_reserve_capacity(admission_ledger):
    conn, calls, *_rest = admission_ledger
    _sd03b_seed(conn, 1000, mixed=True)
    before = snapshot(conn)
    result = _sd03b_dispatch(calls)
    assert result["status"] == "rejected"
    assert snapshot(conn) == before
    assert not any(calls.values())


def test_sd03b_disposition_frees_one_admission_slot(admission_ledger):
    conn, calls, *_rest = admission_ledger
    _sd03b_seed(conn, 1000)
    assert subject.mark_completion_delivered("p-0000")
    delivered = conn.execute("SELECT * FROM async_delegations WHERE delegation_id='p-0000'").fetchone()
    assert _sd03b_dispatch(calls)["status"] == "dispatched"
    assert conn.execute("SELECT * FROM async_delegations WHERE delegation_id='p-0000'").fetchone() == delivered
    assert conn.execute("SELECT COUNT(*) FROM async_delegations WHERE delivery_state='pending'").fetchone()[0] == 1000


def test_sd03b_count_and_insert_use_immediate_write_transaction(admission_ledger):
    conn, calls, _executor, _captured, trace, _transaction = admission_ledger
    trace.clear()
    assert _sd03b_dispatch(calls)["status"] == "dispatched"
    normalized = [line.upper().strip() for line in trace]
    begin = next(i for i, line in enumerate(normalized) if line == "BEGIN IMMEDIATE")
    count = next(i for i, line in enumerate(normalized) if line.startswith("SELECT COUNT(*)"))
    insert_at = next(i for i, line in enumerate(normalized) if line.startswith("INSERT INTO"))
    commit = next(i for i, line in enumerate(normalized) if line == "COMMIT")
    assert begin < count < insert_at < commit


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
def test_sd03b_storage_failure_removes_provisional_record(admission_ledger, monkeypatch, batch):
    conn, calls, *_rest = admission_ledger
    @contextmanager
    def full_storage():
        raise sqlite3.OperationalError("inert database full")
        yield conn
    monkeypatch.setattr(subject, "_transaction", full_storage)
    result = _sd03b_dispatch(calls, batch=batch)
    assert result["status"] == "rejected"
    assert result["error_code"] == "durable_storage_unavailable"
    assert result["execution_started"] is False
    assert subject._records == {}
    assert not any(calls.values())


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
def test_sd03b_commit_failure_rolls_back_before_execution(admission_ledger, monkeypatch, batch):
    conn, calls, *_rest = admission_ledger
    @contextmanager
    def failed_commit():
        try:
            yield conn
            raise sqlite3.OperationalError("inert commit failure")
        finally:
            conn.rollback()
    monkeypatch.setattr(subject, "_transaction", failed_commit)
    result = _sd03b_dispatch(calls, batch=batch)
    assert result["status"] == "rejected"
    assert result["execution_started"] is False
    assert subject._records == {}
    assert snapshot(conn) == []
    assert not any(calls.values())


def test_sd03b_duplicate_id_preserves_durable_receipt(admission_ledger):
    conn, _calls, *_rest = admission_ledger
    _sd03b_seed(conn, 1)
    before = snapshot(conn)
    record = {"delegation_id": "p-0000", "session_key": "other", "dispatched_at": NOW, "goal": "new"}
    with pytest.raises(sqlite3.IntegrityError):
        subject._persist_dispatch(record)
    assert snapshot(conn) == before


def test_sd03b_duplicate_memory_id_preserves_existing_record(admission_ledger):
    conn, calls, *_rest = admission_ledger
    original = {"status": "completed", "result": {"summary": "retained"}}
    subject._records["sd03b-new"] = original
    result = _sd03b_dispatch(calls)
    assert result["status"] == "rejected"
    assert subject._records["sd03b-new"] is original
    assert snapshot(conn) == []
    assert not any(calls.values())


def test_sd03b_pruning_failure_keeps_committed_admission(admission_ledger, monkeypatch):
    conn, calls, *_rest = admission_ledger
    def failed_prune():
        raise OSError(28, "inert ENOSPC")
    monkeypatch.setattr(subject, "_prune_durable_records", failed_prune)
    assert _sd03b_dispatch(calls)["status"] == "dispatched"
    assert conn.execute("SELECT COUNT(*) FROM async_delegations").fetchone()[0] == 1
    assert calls["submit"] == 1 and calls["runner"] == 0


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
def test_sd03b_executor_lookup_failure_releases_unstarted_reservation(admission_ledger, monkeypatch, batch):
    conn, calls, *_rest = admission_ledger
    def unavailable(_limit):
        raise RuntimeError("inert executor unavailable before submission")
    monkeypatch.setattr(subject, "_get_executor", unavailable)
    result = _sd03b_dispatch(calls, batch=batch)
    assert result["status"] == "rejected"
    assert result["execution_started"] is False
    assert result["durable_reservation_released"] is True
    assert subject._records == {} and snapshot(conn) == []
    assert calls["submit"] == calls["runner"] == 0


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
def test_sd03b_ambiguous_submit_retains_record_without_replay(admission_ledger, monkeypatch, batch):
    conn, calls, executor, captured, *_rest = admission_ledger
    def enqueue_then_raise(callback):
        calls["submit"] += 1
        captured.append(callback)
        raise RuntimeError("inert submission outcome unknown")
    monkeypatch.setattr(executor, "submit", enqueue_then_raise)
    result = _sd03b_dispatch(calls, batch=batch)
    assert result["status"] == "dispatch_uncertain"
    assert result["error_code"] == "scheduling_uncertain"
    assert result["execution_started"] is None
    assert result["delegation_id"] == "sd03b-new"
    assert "sd03b-new" in subject._records
    assert conn.execute("SELECT COUNT(*) FROM async_delegations").fetchone()[0] == 1
    assert len(captured) == 1 and calls["runner"] == 0


@pytest.mark.parametrize("protected", ["claim", "event", "completion", "owner", "timestamp"])
def test_sd03b_release_never_deletes_nonmatching_or_protected_rows(admission_ledger, protected):
    conn, _calls, *_rest = admission_ledger
    record = {"delegation_id": "owned", "session_key": "fixture", "dispatched_at": NOW}
    reservation = subject._persist_dispatch(record)
    changes = {
        "claim": ("delivery_claim", "held"), "event": ("event_json", "{}"),
        "completion": ("completed_at", NOW), "owner": ("owner_started_at", 43),
        "timestamp": ("dispatched_at", NOW + 1),
    }
    column, value = changes[protected]
    conn.execute(f"UPDATE async_delegations SET {column}=? WHERE delegation_id='owned'", (value,))
    conn.commit()
    before = snapshot(conn)
    assert subject._delete_durable_delegation("owned", reservation=reservation) is False
    assert snapshot(conn) == before
