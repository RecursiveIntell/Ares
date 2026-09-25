"""Actual SQLite races at the continuation compilation/publication boundary."""
from contextlib import contextmanager
import json

import pytest

from ares_runtime.continuity import runtime
from hermes_cli.goals import GoalState
from hermes_state import SessionDB
from hermes_state_continuity import ContextContinuationError

# Reuse the owner-backed runtime fixture, not a second imitation of the caller.
from tests.ares_runtime.test_continuity_runtime import setup  # noqa: F401


def _attempt(setup):
    _, agent, messages, history, _ = setup
    return runtime.attempt_turn_start_context_rebase(
        agent, messages, conversation_history=history,
        active_system_prompt=agent._cached_system_prompt, before_tokens=100_000,
    )


@pytest.mark.parametrize("owner", ["goal", "heartbeat", "loop", "user", "effect", "workspace"])
def test_changed_compilation_read_set_cannot_publish(setup, monkeypatch, owner):
    db, agent, _, _, transitions = setup
    goal = GoalState(goal="Inspect only", created_at=1.0)
    db.set_meta("goal:s0", goal.to_json())
    effect_id = db.append_message(
        "s0", "tool", "settled observation", tool_call_id="call-1",
        tool_name="terminal", effect_disposition="none",
    )
    build = runtime.build_live_candidate

    def compile_then_change(*args, **kwargs):
        candidate = build(*args, **kwargs)
        if owner == "goal":
            goal.status = "paused"
            db.set_meta("goal:s0", goal.to_json())
        elif owner in {"heartbeat", "loop"}:
            # Invalid new owner state is still a change, never missing state.
            db.set_meta(f"{owner}:s0", '{"changed":true}')
        elif owner == "user":
            db._execute_write(lambda conn: conn.execute(
                "UPDATE messages SET content=? WHERE session_id='s0' AND role='user'",
                ("Stop; do not execute",),
            ))
        elif owner == "effect":
            db._execute_write(lambda conn: conn.execute(
                "UPDATE messages SET effect_disposition='unknown' WHERE id=?", (effect_id,),
            ))
        else:
            db._execute_write(lambda conn: conn.execute(
                "UPDATE sessions SET cwd='/different-workspace' WHERE id='s0'"
            ))
        return candidate

    monkeypatch.setattr(runtime, "build_live_candidate", compile_then_change)
    result = _attempt(setup)
    assert result.status is runtime.AutomaticRebaseStatus.BLOCKED
    assert result.reason == "CONTEXT_REBASE_STALE_SNAPSHOT"
    assert db.get_session("s0")["ended_at"] is None
    assert agent.session_id == "s0"
    assert transitions == []
    assert db.get_context_continuation_tip("s0") == "s0"


def test_unknown_effect_cannot_become_normal_execution_authority(setup):
    db, agent, _, _, transitions = setup
    db.append_message("s0", "tool", "acknowledgment missing",
                      tool_name="terminal", tool_call_id="uncertain-write",
                      effect_disposition="unknown")
    result = _attempt(setup)
    assert result.status is runtime.AutomaticRebaseStatus.BLOCKED
    assert result.reason == "CONTEXT_REBASE_UNRESOLVED_EFFECTS"
    assert db.get_session("s0")["ended_at"] is None
    assert agent.session_id == "s0"
    assert transitions == []


def test_snapshot_is_one_wal_transaction_and_releases_reader(setup, monkeypatch):
    db, _, _, _, _ = setup
    if not db._wal_active:
        pytest.skip("Requires actual WAL to allow the simultaneous writer")
    goal = GoalState(goal="old owner view", created_at=1.0)
    db.set_meta("goal:s0", goal.to_json())
    with db._read_ctx() as conn:
        old_message = conn.execute(
            "SELECT content FROM messages WHERE session_id='s0' AND role='user'"
        ).fetchone()[0]
    db.append_message("s0", "user", "current human input")
    reader = db._read_ctx
    writer = SessionDB(db_path=db.db_path)
    observed_transactions = []
    changed = False

    @contextmanager
    def interleaved_read():
        nonlocal changed
        with reader() as conn:
            def trace(sql):
                nonlocal changed
                # The first session SELECT has established the read snapshot.
                if "AS watermark FROM messages" in sql and not changed:
                    changed = True
                    goal.goal = "new owner view"
                    def write(c):
                        c.execute("UPDATE messages SET content='new human input' WHERE session_id='s0' AND role='user'")
                        c.execute("UPDATE state_meta SET value=? WHERE key='goal:s0'", (goal.to_json(),))
                    writer._execute_write(write)
            conn.set_trace_callback(trace)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)
                observed_transactions.append(conn.in_transaction)

    monkeypatch.setattr(db, "_read_ctx", interleaved_read)
    try:
        snapshot = db.read_context_rebase_snapshot("s0")
        assert changed
        assert snapshot.first_user["content"] == old_message
        assert json.loads(snapshot.goal_raw)["goal"] == "old owner view"
        assert observed_transactions == [False]
        refreshed = db.read_context_rebase_snapshot("s0")
        assert refreshed.first_user["content"] == "new human input"
        assert json.loads(refreshed.goal_raw)["goal"] == "new owner view"
    finally:
        writer.close()


def test_snapshot_refusal_returns_clean_pooled_reader(setup):
    db, _, _, _, _ = setup
    # The fixture ends with a summary and no current user anchor.
    with pytest.raises(ContextContinuationError, match="USER_ANCHOR_MISSING"):
        db.read_context_rebase_snapshot("s0")
    with db._read_ctx() as conn:
        assert not conn.in_transaction
    db.append_message("s0", "user", "new input")
    assert db.read_context_rebase_snapshot("s0").current_users[-1]["content"] == "new input"


@pytest.mark.parametrize("refuse", [False, True])
def test_snapshot_savepoint_preserves_borrowed_outer_transaction(setup, monkeypatch, refuse):
    db, _, _, _, _ = setup
    monkeypatch.setattr(db, "_checkout_read_conn", lambda: None)
    db._conn.execute("BEGIN IMMEDIATE")
    db._conn.execute("INSERT INTO state_meta(key,value) VALUES('outer-proof','uncommitted')")
    try:
        if refuse:
            with pytest.raises(ContextContinuationError, match="USER_ANCHOR_MISSING"):
                db.read_context_rebase_snapshot("s0")
        else:
            db._conn.execute("UPDATE messages SET _compressed_summary=0 WHERE session_id='s0'")
            assert db.read_context_rebase_snapshot("s0").first_user is not None
        assert db._conn.in_transaction
        assert db._conn.execute("SELECT value FROM state_meta WHERE key='outer-proof'").fetchone()[0] == "uncommitted"
    finally:
        db._conn.rollback()
    assert db.get_meta("outer-proof") is None
