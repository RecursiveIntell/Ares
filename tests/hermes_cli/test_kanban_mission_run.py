"""Real current-root task/run checks; dependency edges are not authority."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from ares_runtime.collaboration import ContractError, contract_ref
from hermes_cli import kanban_db as kb
from tests.hermes_cli.test_kanban_mission_binding import mission

pytest_plugins = ("tests.hermes_cli.test_kanban_mission_binding",)
pytestmark = pytest.mark.linux_only
NOW = 1800000000


@pytest.fixture
def running(owner, monkeypatch):
    conn, task = owner
    monkeypatch.setattr(kb.time, "time", lambda: NOW)
    artifact = mission(task)
    attachment = kb.attach_task_mission_contract(conn, task, artifact, board="default")
    claim = kb.claim_task(conn, task, claimer="host:fixture", ttl_seconds=120)
    assert claim is not None and claim.status == "running"
    return conn, task, artifact, attachment, claim.current_run_id


def read(running, **overrides):
    conn, task, artifact, attachment, run_id = running
    args = {
        "board": "default",
        "expected_contract_ref": contract_ref(artifact),
        "expected_run_id": run_id,
        "expected_claim_lock": "host:fixture",
    }
    args.update(overrides)
    return kb.read_running_task_mission_contract(conn, task, attachment, **args)


def test_current_run_api_is_in_existing_owner():
    assert (
        Path(kb.__file__).resolve()
        == Path(__file__).resolve().parents[2] / "hermes_cli/kanban_db.py"
    )
    assert callable(getattr(kb, "read_running_task_mission_contract", None))


def test_real_current_claim_reopen_readback_does_not_mutate(running):
    conn, task, artifact, attachment, run_id = running
    before = conn.total_changes
    events = kb.list_events(conn, task)
    assert read(running).canonical_bytes() == artifact.canonical_bytes()
    assert conn.total_changes == before and not conn.in_transaction
    assert kb.list_events(conn, task) == events
    with kb.connect_closing(board="default") as reopened:
        assert (
            read((reopened, task, artifact, attachment, run_id)).to_dict()
            == artifact.to_dict()
        )
        assert reopened.total_changes == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_run_id", None),
        ("expected_run_id", True),
        ("expected_run_id", 0),
        ("expected_run_id", "1"),
        ("expected_run_id", 1.0),
        ("expected_run_id", 1 << 63),
        ("expected_run_id", -1),
        ("expected_claim_lock", None),
        ("expected_claim_lock", ""),
        ("expected_claim_lock", "host:fixture\n"),
        ("expected_claim_lock", "x" * 513),
    ],
)
def test_malformed_current_identity_refuses_without_transaction(running, field, value):
    conn = running[0]
    before = conn.total_changes
    with pytest.raises(ContractError, match="MISSION_RUN_REFERENCE_INVALID"):
        read(running, **{field: value})
    assert conn.total_changes == before and not conn.in_transaction


def test_wrong_claim_and_run_cannot_select_current_or_latest(running):
    for override in (
        {"expected_claim_lock": "host:foreign"},
        {"expected_run_id": running[4] + 1000},
    ):
        with pytest.raises(ContractError, match="MISSION_RUN_NOT_CURRENT"):
            read(running, **override)


@pytest.mark.parametrize(
    "table,field,value",
    [
        ("tasks", "status", "done"),
        ("tasks", "status", "blocked"),
        ("tasks", "current_run_id", None),
        ("tasks", "claim_lock", "host:other"),
        ("tasks", "claim_expires", NOW),
        ("tasks", "claim_expires", NOW - 1),
        ("tasks", "claim_expires", NOW + 121),
        ("tasks", "completed_at", NOW),
        ("task_runs", "task_id", "foreign-root"),
        ("task_runs", "status", "reclaimed"),
        ("task_runs", "claim_lock", "host:other"),
        ("task_runs", "claim_expires", NOW),
        ("task_runs", "ended_at", NOW),
        ("task_runs", "outcome", "completed"),
        ("task_runs", "started_at", NOW + 1),
        ("task_runs", "started_at", "bad"),
        ("task_runs", "claim_expires", None),
        ("task_runs", "claim_expires", NOW + 0.5),
    ],
)
def test_noncurrent_raw_owner_rows_refuse_without_repair(running, table, field, value):
    conn, task, _, _, run_id = running
    key = task if table == "tasks" else run_id
    # task_id mutation requires a real foreign task because FK remains enabled.
    if table == "task_runs" and field == "task_id":
        value = kb.create_task(conn, title="foreign root", board="default")
    conn.execute(f"UPDATE {table} SET {field}=? WHERE id=?", (value, key))
    conn.commit()
    before = conn.total_changes
    with pytest.raises(ContractError, match="MISSION_RUN_NOT_CURRENT"):
        read(running)
    assert conn.total_changes == before and not conn.in_transaction


def test_missing_run_cannot_be_reconstructed(running):
    conn, _, _, _, run_id = running
    # Keep the task's dangling pointer to establish refusal, not auto repair.
    conn.execute("DELETE FROM task_runs WHERE id=?", (run_id,))
    conn.commit()
    before = conn.total_changes
    with pytest.raises(ContractError, match="MISSION_RUN_NOT_CURRENT"):
        read(running)
    assert kb.get_run(conn, run_id) is None and conn.total_changes == before


@pytest.mark.parametrize("kind", ["read-snapshot", "uncommitted-write"])
def test_existing_transaction_is_never_committed_or_used_as_fresh_state(running, kind):
    conn, task, _, _, _ = running
    conn.execute("BEGIN")
    if kind == "read-snapshot":
        conn.execute("SELECT * FROM tasks").fetchall()
    else:
        conn.execute("UPDATE tasks SET title='not committed' WHERE id=?", (task,))
    with pytest.raises(ContractError, match="MISSION_RUN_TRANSACTION_UNQUALIFIED"):
        read(running)
    assert conn.in_transaction
    conn.rollback()


@pytest.mark.parametrize("elapsed", [59.5, 60, 60.5])
def test_runtime_uses_active_attempt_not_task_lifetime(running, monkeypatch, elapsed):
    conn, task, _, _, run_id = running
    conn.execute(
        "UPDATE tasks SET started_at=?, max_runtime_seconds=60 WHERE id=?",
        (NOW - 10000, task),
    )
    conn.execute("UPDATE task_runs SET max_runtime_seconds=60 WHERE id=?", (run_id,))
    conn.commit()
    monkeypatch.setattr(kb.time, "time", lambda: NOW + elapsed)
    if elapsed < 60:
        assert read(running)
    else:
        with pytest.raises(ContractError, match="MISSION_RUN_NOT_CURRENT"):
            read(running)


@pytest.mark.parametrize(
    "which,cap",
    [
        ("tasks", -1),
        ("tasks", 0),
        ("tasks", 1.5),
        ("tasks", 40),
        ("task_runs", 0),
        ("task_runs", -1),
    ],
)
def test_invalid_or_differing_runtime_caps_are_not_defaulted(running, which, cap):
    conn, task, _, _, run_id = running
    conn.execute(
        f"UPDATE {which} SET max_runtime_seconds=? WHERE id=?",
        (cap, task if which == "tasks" else run_id),
    )
    conn.commit()
    with pytest.raises(ContractError, match="MISSION_RUN_NOT_CURRENT"):
        read(running)


@pytest.mark.parametrize("clock", [float("nan"), float("inf"), True, -1])
def test_invalid_clock_cannot_qualify_currentness(running, monkeypatch, clock):
    monkeypatch.setattr(kb.time, "time", lambda: clock)
    with pytest.raises(ContractError, match="MISSION_RUN_CLOCK_UNQUALIFIED"):
        read(running)
    assert not running[0].in_transaction


@pytest.mark.parametrize("after", [NOW - 0.01, NOW + 120, NOW + 120.5, float("nan")])
def test_clock_and_expiry_rechecked_after_artifact_read(running, monkeypatch, after):
    original = kb.read_task_mission_contract

    def read_and_advance(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setattr(kb.time, "time", lambda: after)
        return result

    monkeypatch.setattr(kb, "read_task_mission_contract", read_and_advance)
    with pytest.raises(ContractError):
        read(running)
    assert not running[0].in_transaction


def test_other_sqlite_writer_cannot_change_claim_during_readback(running, monkeypatch):
    original = kb.read_task_mission_contract
    conn, task, _, _, _ = running
    blocked = []

    def contested_read(*args, **kwargs):
        # Independent real connection attempts a mutation at the artifact seam.
        with sqlite3.connect(
            kb.kanban_db_path(board="default"), timeout=0
        ) as contender:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute(
                    "UPDATE tasks SET claim_lock='host:foreign' WHERE id=?", (task,)
                )
            blocked.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(kb, "read_task_mission_contract", contested_read)
    assert read(running)
    assert blocked == [True]
    assert kb.get_task(conn, task).claim_lock == "host:fixture"


def test_foreign_artifact_and_dependency_link_do_not_grant_membership(running):
    conn, root, artifact, attachment, run_id = running
    child = kb.create_task(conn, title="dependent, not delegate", board="default")
    kb.link_tasks(conn, root, child)
    with pytest.raises(ContractError):
        kb.read_running_task_mission_contract(
            conn,
            child,
            attachment,
            board="default",
            expected_contract_ref=contract_ref(artifact),
            expected_run_id=run_id,
            expected_claim_lock="host:fixture",
        )


def test_exact_reference_required_even_for_current_worker(running):
    with pytest.raises(ContractError):
        read(running, expected_contract_ref="contract:" + "0" * 64)
    assert not running[0].in_transaction


def test_real_reclaim_and_same_claimer_reclaim_cannot_reuse_old_attempt(running):
    conn, task, artifact, attachment, original_run = running
    assert kb.reclaim_task(conn, task, reason="offline recovery control")
    successor = kb.claim_task(conn, task, claimer="host:fixture", ttl_seconds=120)
    assert successor is not None and successor.current_run_id != original_run
    with pytest.raises(ContractError, match="MISSION_RUN_NOT_CURRENT"):
        read(running)
    assert read((conn, task, artifact, attachment, successor.current_run_id))
    old_run = kb.get_run(conn, original_run)
    assert old_run is not None and old_run.outcome == "reclaimed"


def test_real_retained_noncurrent_run_cannot_substitute_for_current_pointer(running):
    conn, task, _, _, original_run = running
    assert kb.reclaim_task(conn, task, reason="pointer substitution control")
    successor = kb.claim_task(conn, task, claimer="host:fixture", ttl_seconds=120)
    assert successor is not None
    # Damage the retained projection to make every value *except* the current
    # pointer agree. Both rows exist. This detects removal of that exact guard.
    conn.execute(
        "UPDATE task_runs SET status='running', ended_at=NULL, outcome=NULL, "
        "claim_lock='host:fixture', claim_expires=? WHERE id=?",
        (NOW + 120, original_run),
    )
    conn.commit()
    with pytest.raises(ContractError, match="MISSION_RUN_NOT_CURRENT"):
        read(running)


def test_clock_is_sampled_only_inside_owner_writer_lock(running, monkeypatch):
    conn = running[0]
    observations = []

    def clock():
        observations.append(conn.in_transaction)
        assert conn.in_transaction, "time sampled outside current-owner transaction"
        return NOW

    monkeypatch.setattr(kb.time, "time", clock)
    assert read(running)
    assert observations == [True, True]


def test_real_process_writer_is_excluded_and_lock_released(running, monkeypatch):
    conn, task, _, _, _ = running
    original = kb.read_task_mission_contract
    script = (
        "import sqlite3,sys; assert sys.version_info >= (3,14); "
        "db=sqlite3.connect(sys.argv[1],timeout=0); "
        "db.execute('BEGIN IMMEDIATE'); db.rollback()"
    )

    def contest():
        return subprocess.run(
            [sys.executable, "-c", script, str(kb.kanban_db_path(board="default"))],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def guarded_read(*args, **kwargs):
        result = contest()
        assert result.returncode != 0 and "database is locked" in result.stderr
        return original(*args, **kwargs)

    monkeypatch.setattr(kb, "read_task_mission_contract", guarded_read)
    assert read(running)
    assert contest().returncode == 0
    current = kb.get_task(conn, task)
    assert (
        not conn.in_transaction
        and current is not None
        and current.claim_lock == "host:fixture"
    )


def test_readback_failure_releases_lock_without_repair(running):
    conn, _, _, attachment, _ = running
    row = kb.get_attachment(conn, attachment)
    assert row is not None
    path = Path(row.stored_path)
    bad = path.read_bytes().replace(b'"verify"', b'"VERIFY"')
    path.write_bytes(bad)
    before = conn.total_changes
    with pytest.raises(ContractError):
        read(running)
    assert not conn.in_transaction and conn.total_changes == before
    assert path.read_bytes() == bad


def test_old_wal_snapshot_cannot_qualify_after_other_connection_reclaims(running):
    conn, task, _, _, _ = running
    # The production owner legitimately uses DELETE instead of WAL on SQLite
    # builds affected by the WAL-reset corruption advisory. A concurrent writer
    # cannot advance while this DELETE reader holds a transaction, so testing a
    # WAL-only race there causes an unbounded lock wait rather than exercising
    # the currentness guard. Both journal modes must still refuse stale reads.
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    assert mode in {"wal", "delete"}
    if mode == "delete":
        conn.execute("BEGIN")
        assert conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task,)
        ).fetchone()[0] == "running"
        with pytest.raises(ContractError, match="MISSION_RUN_TRANSACTION_UNQUALIFIED"):
            read(running)
        assert conn.in_transaction
        conn.rollback()
        with kb.connect_closing(board="default") as other:
            assert kb.reclaim_task(other, task, reason="invalidate stale snapshot")
        with pytest.raises(ContractError, match="MISSION_RUN_NOT_CURRENT"):
            read(running)
        return

    conn.execute("BEGIN")
    assert (
        conn.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()[0]
        == "running"
    )
    with kb.connect_closing(board="default") as other:
        assert kb.reclaim_task(other, task, reason="invalidate stale snapshot")
    # WAL lets this reader retain 'running'; the new API must refuse the txn,
    # not mistake that retained image for canonical current authority.
    assert (
        conn.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()[0]
        == "running"
    )
    with pytest.raises(ContractError, match="MISSION_RUN_TRANSACTION_UNQUALIFIED"):
        read(running)
    assert conn.in_transaction
    conn.rollback()
    with pytest.raises(ContractError, match="MISSION_RUN_NOT_CURRENT"):
        read(running)
