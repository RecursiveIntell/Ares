"""Background notifications must not wake a goal parked on operator authority.

No automatic event can supply completion evidence, so a wakeup only spends a
turn re-judging the same "done without evidence" verdict and leaves the session
busy again. A goal parked on a *barrier* (pid/session/time) is the opposite: its
wakeup is the event it is waiting for, so barrier precedence must win.
"""

import types

import pytest

import hermes_cli.goals as goals
import tui_gateway.server as server


class _Manager:
    def __init__(self, *, active=True, waiting=False, outcome=None):
        self.active = active
        self.waiting = waiting
        self.state = types.SimpleNamespace(outcome=outcome)

    def is_active(self):
        return self.active

    def is_waiting(self):
        return self.waiting


def _install(monkeypatch, **kwargs):
    monkeypatch.setattr(goals, "GoalManager", lambda **_kw: _Manager(**kwargs))


def test_authority_wait_holds_notifications(monkeypatch):
    _install(
        monkeypatch,
        active=True,
        waiting=False,
        outcome=goals.WAITING_FOR_AUTHORITY,
    )

    assert server._authority_holds_notifications({"session_key": "s1"}) is True


def test_barrier_wins_over_authority_outcome(monkeypatch):
    # Parked on a pid/session: that event IS the wakeup. Holding it would
    # deadlock the goal loop.
    _install(
        monkeypatch,
        active=True,
        waiting=True,
        outcome=goals.WAITING_FOR_AUTHORITY,
    )

    assert server._authority_holds_notifications({"session_key": "s1"}) is False


def test_inactive_goal_does_not_hold(monkeypatch):
    _install(
        monkeypatch,
        active=False,
        waiting=False,
        outcome=goals.WAITING_FOR_AUTHORITY,
    )

    assert server._authority_holds_notifications({"session_key": "s1"}) is False


def test_ordinary_running_goal_does_not_hold(monkeypatch):
    _install(monkeypatch, active=True, waiting=False, outcome=goals.GOAL_ACTIVE)

    assert server._authority_holds_notifications({"session_key": "s1"}) is False


def test_missing_session_key_does_not_hold():
    assert server._authority_holds_notifications({}) is False


def test_storage_failure_fails_open(monkeypatch):
    def _boom(**_kwargs):
        raise RuntimeError("state.db unavailable")

    monkeypatch.setattr(goals, "GoalManager", _boom)

    assert server._authority_holds_notifications({"session_key": "s1"}) is False
