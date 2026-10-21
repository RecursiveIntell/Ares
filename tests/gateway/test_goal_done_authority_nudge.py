"""Task 1.1 (goal-wait-effort-clobber-20261020): free-form vs evidence-gated goals.

A judge-``done`` verdict on a FREE-FORM goal (no contract, no gates) must not
park in ``WAITING_FOR_AUTHORITY`` immediately. Instead it fires ONE bounded
confirmation-nudge turn (``should_continue=True`` + an evidence-leaning
continuation prompt, persisted via ``done_confirmation_nudged=True``). A SECOND
consecutive judge-done without new evidence parks as today — but labeled
"awaiting your confirmation", and ONLY after the nudge. Goals WITH a contract
or gates keep the strict evidence gate unchanged (no nudge turn).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionEntry, SessionSource, build_session_key


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import goals

    goals._DB_CACHE.clear()
    goals._get_session_db()
    yield home
    goals._DB_CACHE.clear()


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


class _RecordingAdapter:
    def __init__(self) -> None:
        self._pending_messages: dict = {}
        self.sends: list[dict] = []

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None):
        self.sends.append({"chat_id": chat_id, "content": content, "metadata": metadata})

        class _R:
            success = True
            message_id = "mock-msg"

        return _R()


    # _enqueue_fifo reads pending_slot keyed by session_key
    def reset(self):
        self._pending_messages = {}


def _make_runner(session_id: str = None):
    from gateway.run import GatewayRunner
    import uuid

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")},
    )
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._queued_events = {}

    src = _make_source()
    session_entry = SessionEntry(
        session_key=build_session_key(src),
        session_id=session_id or f"goal-nudge-{uuid.uuid4().hex[:8]}",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )

    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store._generate_session_key.return_value = build_session_key(src)

    adapter = _RecordingAdapter()
    runner.adapters[Platform.TELEGRAM] = adapter
    return runner, adapter, session_entry, src


from unittest.mock import MagicMock

import asyncio


async def _drain_until(condition, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while not condition() and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.01)


def _fresh_manager(sid: str, contract=None, gates=None):
    from hermes_cli.goals import GoalManager, save_goal

    mgr = GoalManager(sid)
    mgr.set("ship the thing")
    state = mgr.state
    if contract:
        from hermes_cli.goals import GoalContract

        state.contract = contract
    if gates:
        for g in gates:
            mgr.add_gate(g)
    save_goal(sid, state)
    return mgr


def test_free_form_goal_judge_done_nudges_once_then_parks(hermes_home):
    """First free-form judge-done: nudge turn with evidence-leaning prompt,
    no WAITING_FOR_AUTHORITY yet. Second consecutive: parks honestly."""
    from hermes_cli import goals as goals_mod
    from hermes_cli.goals import GoalManager, WAITING_FOR_AUTHORITY, load_goal, save_goal

    import uuid

    sid = f"nudge-{uuid.uuid4().hex[:8]}"
    mgr = GoalManager(sid)
    mgr.set("polish the docs")
    state = mgr.state
    assert not state.has_contract() and not state.gates

    with patch("hermes_cli.goals.judge_goal", return_value=("done", "looks done", False, None, False)):
        decision = mgr.evaluate_after_turn("here is the finished docs pass")

    assert decision["status"] == "active"
    assert decision["should_continue"] is True
    assert decision.get("continuation_prompt"), "nudge turn must fire a continuation prompt"
    assert "complete" in decision.get("continuation_prompt", "").lower()

    reloaded = load_goal(sid)
    assert reloaded.outcome != WAITING_FOR_AUTHORITY
    assert reloaded.done_confirmation_nudged is True

    # Second consecutive judge-done without new evidence parks honestly.
    mgr2 = GoalManager(sid)
    with patch("hermes_cli.goals.judge_goal", return_value=("done", "still looks done", False, None, False)):
        decision2 = mgr2.evaluate_after_turn("no real new evidence")

    assert decision2["should_continue"] is False
    assert load_goal(sid).outcome == WAITING_FOR_AUTHORITY
    assert "confirmation" in decision2["message"].lower()


def test_contracted_goal_judge_done_still_waits_for_authority(hermes_home):
    """Gated goals keep the strict evidence gate: no nudge turn."""
    from hermes_cli.goals import (
        GoalManager,
        GoalContract,
        WAITING_FOR_AUTHORITY,
        load_goal,
        save_goal,
    )

    import uuid

    sid = f"gate-{uuid.uuid4().hex[:8]}"
    mgr = GoalManager(sid)
    mgr.set("ship with verification")
    state = mgr.state
    state.contract = GoalContract(outcome="tests green", verification="pytest exit 0")
    save_goal(sid, state)
    assert state.has_contract()

    with patch("hermes_cli.goals.judge_goal", return_value=("done", "looks done", False, None, False)):
        decision = mgr.evaluate_after_turn("finished")

    assert decision["should_continue"] is False
    assert load_goal(sid).outcome == WAITING_FOR_AUTHORITY


def test_gate_failure_precedes_authority_wait(hermes_home):
    """Gates failing keeps the goal working (existing gate path). Assert it
    is not regressed by the new free-form branch."""
    from hermes_cli.goals import GoalManager, WAITING_FOR_AUTHORITY, load_goal, save_goal

    import uuid

    sid = f"gfail-{uuid.uuid4().hex[:8]}"
    mgr = GoalManager(sid)
    mgr.set("ship the thing")
    mgr.add_gate("false  # always failing quality gate")
    state = load_goal(sid)
    assert state.gates
    save_goal(sid, state)

    with patch("hermes_cli.goals.judge_goal", return_value=("done", "looks done", False, None, False)) as j:
        decision = mgr.evaluate_after_turn("done-looking response")

    # The judge must NOT have been called: gate failure short-circuits first.
    j.assert_not_called()
    assert decision["should_continue"] is True, "failing gate means the loop keeps working"
    assert load_goal(sid).outcome != WAITING_FOR_AUTHORITY


def test_non_done_verdict_resets_confirmation_nudge(hermes_home):
    """After a nudge, any non-done verdict clears the flag so a FUTURE
    judge-done (much later, after more work) nudges again."""
    from hermes_cli.goals import GoalManager, load_goal, save_goal

    import uuid

    sid = f"reset-{uuid.uuid4().hex[:8]}"
    mgr = GoalManager(sid)
    mgr.set("polish the docs")
    state = mgr.state
    state.done_confirmation_nudged = True
    save_goal(sid, state)

    with patch("hermes_cli.goals.judge_goal", return_value=("continue", "more work", False, None, False)):
        decision = mgr.evaluate_after_turn("here is more work")

    assert decision["should_continue"] is True
    assert load_goal(sid).done_confirmation_nudged is False


@pytest.mark.asyncio
async def test_nudge_turn_reaches_gateway_continuation_path(hermes_home):
    """The FIRST free-form judge-done must enqueue a continuation through the
    gateway FIFO (the driver actually fires), and message the user that we
    believe the goal looks done but need confirmation."""
    runner, adapter, session_entry, src = _make_runner()

    from hermes_cli.goals import GoalManager

    mgr = GoalManager(session_entry.session_id)
    mgr.set("finish the phases")

    with patch("hermes_cli.goals.judge_goal", return_value=("done", "looks done", False, None, False)):
        await runner._post_turn_goal_continuation(
            session_entry=session_entry,
            source=src,
            final_response="all phases finished",
        )
        deadline = asyncio.get_event_loop().time() + 5.0
        while not (adapter.sends and adapter._pending_messages) and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.01)

    assert adapter._pending_messages, "nudge continuation must be enqueued through the FIFO"
    assert adapter.sends, "a status notice must be sent"
    assert "confirmation" in " ".join(s["content"] for s in adapter.sends).lower() or "complete" in " ".join(s["content"] for s in adapter.sends).lower()
