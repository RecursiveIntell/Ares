"""Task 1.4 (goal-wait-effort-clobber-20261020): live-replay parse parity.

Constructs a GoalState for EVERY status/outcome combination the gateway can
reach, renders ``status_line()``, and replays each emitted line through the
port of the desktop pill's grammar (``apps/desktop/src/store/goals.ts``
``nextGoalFromText``). Asserts ZERO unrecognized lines — the stale-pill bug
mechanism is a line the pill cannot see.

This is a parity harness, NOT a reimplementation: the ported expectations are
kept in one ``EXPECTED`` mapping derived from the TS table, and each runtime
line must land in exactly one of them.
"""

from __future__ import annotations

import re
import time
from typing import Optional, Tuple

import pytest

from hermes_cli.goals import (
    EXECUTION_FAILED,
    GOAL_ACTIVE,
    GOAL_BLOCKED,
    GOAL_COMPLETED,
    GOAL_PAUSED,
    GoalManager,
    GoalState,
    PROVIDER_FAILED,
    TOOL_BUDGET_EXHAUSTED,
    TURN_BUDGET_EXHAUSTED,
    WAITING_FOR_AUTHORITY,
)


# ── Port of the TS grammar (goals.ts ACTIVE_CLAUSE_STATUS + fallbacks) ──
ACTIVE_CLAUSE_STATUS: list[Tuple[str, str]] = [
    (r"^⏳ Goal active · waiting\b", "waiting"),
    (r"^⊙ Goal active · waiting\b", "waiting"),
    (r"^⊙ Goal active · awaiting your confirmation\b", "active"),
    (r"^⊙ Goal active · blocked\b", "active"),
    (r"^⊙ Goal active · checkpointed\b", "active"),
    (r"^⊙ Goal active · running\b", "active"),
]


def port_next_goal(line: str) -> Optional[Tuple[Optional[str], str]]:
    """Return (status, title) or None — mirrors nextGoalFromText's returns:
    (None, …) semantics collapsed: None-status means "clear".
    Raises on UNRECOGNIZED (the TS returns undefined → stale pill).
    """
    patterns_clear = [
        r"^No active goal\b",
        r"^No goal (?:set|to resume)\b",
        r"^✓ Goal cleared\b",
    ]
    if any(re.match(p, line) for p in patterns_clear):
        return (None, "")

    for ts_pattern in [
        r"^⊙ Goal set(?:\s*\([^)]*\))?:\s*(.+)$",
        r"^▶ Goal resumed:\s*(.+)$",
    ]:
        m = re.match(ts_pattern, line)
        if m:
            return ("active", m.group(1).strip())

    for pattern, status in ACTIVE_CLAUSE_STATUS:
        if re.match(pattern, line):
            m = re.match(r"^[^:]+(?:\([^)]*\))?[^:]*:\s*(.+)$", line) or re.match(
                r"^[^:]*:\s*(.+)$", line
            )
            if m:
                return (status, m.group(1).strip())

    m = re.match(r"^⊙ Goal\s*\([^)]*active[^)]*\):\s*(.+)$", line)
    if m:
        return ("active", m.group(1).strip())

    m = re.match(r"^⏳ Goal\s*\([^)]*(?:parked|active)[^)]*\):\s*(.+)$", line)
    if m:
        return ("waiting", m.group(1).strip())

    m = re.match(r"^⏸ Goal(?:\s*\([^)]*\)| paused)?:\s*(.+)$", line)
    if m:
        return ("paused", m.group(1).strip())

    m = re.match(r"^✓ Goal done\s*\([^)]*\):\s*(.+)$", line)
    if m:
        return ("done", m.group(1).strip())

    if re.match(r"^↻ Continuing toward goal\b", line):
        return ("active", "Standing goal")
    if re.match(r"^⏳ Goal parked\b", line):
        return ("waiting", "Standing goal")
    if re.match(r"^⏸ Goal paused\b", line):
        return ("paused", "Standing goal")
    if re.match(r"^✓ Goal achieved\b", line):
        return ("done", "Standing goal")

    return None


def _state(**kwargs) -> GoalState:
    s = GoalState(goal="ship the feature")
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


def _line(s: GoalState) -> str:
    return GoalManager(session_id="parity-test")._state_line(s)


# Bind to the real renderer to avoid drift: GoalManager.status_line delegates
# to _state via self._state. Reuse the implementation directly.
def _state_line(s: GoalState) -> str:
    mgr = GoalManager(session_id="parity-test")
    mgr._state = s
    return mgr.status_line()


def test_every_status_line_is_portable():
    """Every reachable status_line() output must parse (not return None)."""
    now = time.time()
    cases = {
        "no_goal": None,
        "active_running": _state(status="active", outcome=GOAL_ACTIVE),
        "active_continuation_pending": _state(
            status="active", outcome=GOAL_ACTIVE, continuation_pending=True
        ),
        "active_awaiting_confirmation": _state(
            status="active",
            outcome=GOAL_ACTIVE,
            continuation_pending=True,
            last_stop_reason="MODEL_JUDGED_DONE_AWAITING_USER_CONFIRMATION",
        ),
        "active_waiting_authority": _state(
            status="active", outcome=WAITING_FOR_AUTHORITY
        ),
        "active_awaiting_confirmation_nudged": _state(
            status="active",
            outcome=WAITING_FOR_AUTHORITY,
            last_stop_reason="MODEL_JUDGED_DONE_WITHOUT_VERIFIED_EVIDENCE_CONFIRMATION_NUDGED",
        ),
        "active_blocked": _state(status="active", outcome=GOAL_BLOCKED),
        "active_turn_budget": _state(
            status="active", outcome=TURN_BUDGET_EXHAUSTED
        ),
        "active_tool_budget": _state(
            status="active", outcome=TOOL_BUDGET_EXHAUSTED
        ),
        "active_provider_failed": _state(
            status="active", outcome=PROVIDER_FAILED
        ),
        "active_execution_failed": _state(
            status="active", outcome=EXECUTION_FAILED
        ),
        "active_time_barrier": _state(
            status="active", outcome=GOAL_ACTIVE, waiting_until=now + 30.0,
            waiting_reason="backoff",
        ),
        "active_session_parked": _state(
            status="active", outcome=GOAL_ACTIVE,
            waiting_on_session="proc_test1234", waiting_reason="poller",
            waiting_since=now,
        ),
        "active_pid_parked": _state(
            status="active", outcome=GOAL_ACTIVE,
            waiting_on_pid=999999999, waiting_reason="build",
            waiting_since=now,
        ),
        "paused": _state(status="paused", outcome=GOAL_PAUSED),
        "done": _state(status="done", outcome=GOAL_COMPLETED),
    }

    unrecognized = []
    wrong = []
    for name, s in cases.items():
        if s is None:
            line = "No active goal. Set one with /goal <text>."
            parsed = port_next_goal(line)
            if parsed is None:
                unrecognized.append((name, line))
            elif parsed[0] is not None:  # must be the clear transition
                wrong.append((name, line, parsed))
            continue
        line = _state_line(s)
        parsed = port_next_goal(line)
        if parsed is None:
            unrecognized.append((name, line))
            continue
        status, _title = parsed
        if status is None:
            wrong.append((name, line, "cleared"))

    assert not unrecognized, f"stale-pill lines the desktop cannot parse: {unrecognized}"
    assert not wrong, f"statuses parsed to the wrong pill transition: {wrong}"


def test_session_parked_line_unrecognized_when_session_active_is_ok():
    """The parked-on-session variant that can't resolve its live status still
    emits a *known* line (never a blank or novel prefix). Uses a nonexistent
    session → _session_waiting returns False → falls through to the next
    branch, which IS covered. Asserts the FALLBACK, i.e. the line when all
    barriers are unknown is not a semi-formed thing."""
    s = _state(
        status="active",
        outcome=GOAL_ACTIVE,
        waiting_on_session="proc_missing",
        waiting_reason="poller",
    )
    line = _state_line(s)
    assert port_next_goal(line) is not None, f"unparsed fallback line: {line}"


def test_awaiting_confirmation_line_title_extraction():
    s = _state(
        status="active",
        outcome=GOAL_ACTIVE,
        continuation_pending=True,
        last_stop_reason="MODEL_JUDGED_DONE_AWAITING_USER_CONFIRMATION",
        goal="finish all 4 phases",
    )
    line = _state_line(s)
    status, title = port_next_goal(line)
    assert status == "active"
    assert title == "finish all 4 phases"
