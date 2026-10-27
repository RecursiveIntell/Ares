"""Cached or deferred goal writes cannot revoke later operator decisions."""
import pytest

from hermes_state import SessionDB
from hermes_cli import goals


@pytest.fixture
def owner(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    monkeypatch.setattr(goals, "_MEMORY_DEFERRED_GOAL_WRITES", {})
    yield db
    db.close()


def test_stale_active_write_cannot_resurrect_cleared_goal(owner):
    manager = goals.GoalManager("s")
    first = manager.set("first")
    stale = goals.GoalState.from_json(first.to_json())
    manager.clear()
    assert goals.save_goal("s", stale) is False
    assert goals.load_goal("s").status == "cleared"


def test_stale_active_write_cannot_unpause_goal(owner):
    manager = goals.GoalManager("s")
    first = manager.set("first")
    stale = goals.GoalState.from_json(first.to_json())
    manager.pause()
    stale.last_stop_reason = "USER_RESUMED"
    assert goals.save_goal("s", stale) is False
    assert goals.load_goal("s").status == "paused"


def test_old_goal_cannot_replace_new_goal(owner):
    manager = goals.GoalManager("s")
    first = goals.GoalState.from_json(manager.set("first").to_json())
    second = manager.set("second")
    assert goals.save_goal("s", first) is False
    assert goals.load_goal("s").goal_id == second.goal_id


def test_deferred_old_goal_cannot_overwrite_canonical_clear(owner, monkeypatch, tmp_path):
    """A journalled stale write is settled-as-superseded by the canonical clear.

    The journal is per-PID; point this process's journal at tmp_path by
    injecting a fake home and temporarily overriding the path helpers.
    """
    manager = goals.GoalManager("s")
    original = goals.GoalState.from_json(manager.set("first").to_json())
    manager.clear()
    # Simulate a journalled stale write owned by a DEAD process in owner's home.
    fake_home = str(tmp_path / "fake-home")
    journal_dir = goals._deferred_journal_dir(fake_home)
    import os as _os
    _os.makedirs(journal_dir, exist_ok=True)
    dead_pid = _os.getpid() + 4321  # extremely unlikely to exist; verified below
    assert goals._process_alive(dead_pid) is False
    record = {"kind": "write", "id": "j1", "session_id": "s",
              "payload": original.to_json(), "at": 0}
    with open(goals._deferred_journal_dir(fake_home) + f"/{dead_pid}.jsonl", "w") as f:
        f.write(__import__("json").dumps(record) + "\n")
    goals._flush_deferred_goal_writes(fake_home, owner)
    assert goals.load_goal("s").status == "cleared"
    # record settled: nothing live remains in that journal
    assert goals._parse_journal_lines(goals._deferred_journal_dir(fake_home) + f"/{dead_pid}.jsonl") == []


def test_set_refuses_concurrent_replacement_without_false_success(owner, monkeypatch):
    manager = goals.GoalManager("s")
    first = manager.set("first")
    monkeypatch.setattr(owner, "compare_and_set_meta", lambda *a: False)
    with pytest.raises(RuntimeError, match="persist"):
        manager.set("second")
    assert goals.load_goal("s").goal_id == first.goal_id
