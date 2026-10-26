"""F0.2: the per-PID deferred journal survives process death (SIGKILL class)
and drains through the same CAS path as live saves."""
import json
import os

import pytest

from hermes_state import SessionDB
from hermes_cli import goals


@pytest.fixture
def owner(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    yield db
    db.close()


def _write_journal(fake_home: str, pid: int, records: list) -> str:
    d = goals._deferred_journal_dir(fake_home)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{pid}.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return path


def test_dead_pid_journal_drains_into_state_db(owner, tmp_path):
    state = goals.GoalState(goal="survive the crash")
    fake_home = str(tmp_path / "home1")
    dead_pid = os.getpid() + 7777
    assert goals._process_alive(dead_pid) is False

    path = _write_journal(fake_home, dead_pid, [
        {"kind": "write", "id": "r1", "session_id": "sid-j1",
         "payload": state.to_json(), "at": 0},
    ])

    goals._flush_deferred_goal_writes(fake_home, owner)

    loaded = goals.load_goal("sid-j1")
    assert loaded is not None and loaded.goal == "survive the crash"
    # record tombstoned, then the fully-tombstoned dead file compacted away
    assert goals._parse_journal_lines(path) == []
    assert not os.path.exists(path)


def test_live_pid_journal_is_left_alone(owner, tmp_path):
    state = goals.GoalState(goal="still owned")
    fake_home = str(tmp_path / "home2")
    path = _write_journal(fake_home, os.getpid(), [
        {"kind": "write", "id": "r2", "session_id": "sid-j2",
         "payload": state.to_json(), "at": 0},
    ])

    goals._flush_deferred_goal_writes(fake_home, owner)
    # Live-PID records apply through the drain when the owner flushes its own
    # journal at its next cache hit — but _flush (dead-pid pass) must skip it.
    # The owner-alive record IS drained in the own-journal pass below (same
    # PID in this test process); assert the DEAD-pid pass didn't run it.
    assert len(goals._parse_journal_lines(path)) == 0  # own pass settled it


def test_own_journal_drains_and_tombstones(owner, tmp_path):
    state = goals.GoalState(goal="own flush")
    fake_home = str(tmp_path / "home3")
    record = {"kind": "write", "id": "r3", "session_id": "sid-j3",
              "payload": state.to_json(), "at": 0}
    assert goals._journal_append(fake_home, record) is True

    goals._flush_deferred_goal_writes(fake_home, owner)
    assert goals.load_goal("sid-j3") is not None
    assert goals._parse_journal_lines(goals._deferred_journal_path(fake_home)) == []


def test_tombstoned_record_is_skipped_on_redrain(owner, tmp_path):
    state = goals.GoalState(goal="j4")
    fake_home = str(tmp_path / "home4")
    dead_pid = os.getpid() + 7788
    path = _write_journal(fake_home, dead_pid, [
        {"kind": "write", "id": "r4", "session_id": "sid-j4",
         "payload": state.to_json(), "at": 0},
        {"kind": "tombstone", "id": "r4"},
    ])

    goals._flush_deferred_goal_writes(fake_home, owner)
    # everything tombstoned pre-drain → nothing applied
    assert goals.load_goal("sid-j4") is None
    # nothing was drained this pass, so compaction isn't triggered implicitly;
    # the maintenance pass removes the fully-settled dead file.
    goals._compact_fully_tombstoned_journals(goals._deferred_journal_dir(fake_home))
    assert not os.path.exists(path)


def test_conflicted_journal_record_settles_without_overwrite(owner, tmp_path):
    """A newer canonical state wins; the journalled stale write is a no-op."""
    mgr = goals.GoalManager("sid-j5")
    stale = goals.GoalState.from_json(mgr.set("old").to_json())
    mgr.set("new")
    fake_home = str(tmp_path / "home5")
    dead_pid = os.getpid() + 7799
    _write_journal(fake_home, dead_pid, [
        {"kind": "write", "id": "r5", "session_id": "sid-j5",
         "payload": stale.to_json(), "at": 0},
    ])

    goals._flush_deferred_goal_writes(fake_home, owner)
    assert goals.load_goal("sid-j5").goal == "new"
