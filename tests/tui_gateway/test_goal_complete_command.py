"""`/goal complete` must exist on the desktop path and must never replace the goal.

The CLI (``hermes_cli/cli_commands_mixin.py``) and the messaging gateway
(``gateway/slash_commands.py``) both accept ``/goal complete <evidence>``. The
TUI/desktop dispatcher did not: the input fell through to ``mgr.set(arg)``, so
running the one command the authority gate's own ``next_action`` instructs
silently REPLACED the standing goal with the literal text ``complete …``.

These tests pin the shared grammar on the desktop surface:

- ``complete <evidence>`` and ``done <evidence>`` complete the goal;
- bare ``done`` still clears (unchanged);
- empty evidence is a usage error, never a state mutation;
- ordinary prose still sets a new goal.
"""

import threading
import types

import pytest

import tui_gateway.methods_tools  # noqa: F401 — registers command.dispatch
from tui_gateway import server


class _FakeManager:
    instances: list["_FakeManager"] = []
    complete_result = True

    def __init__(self, session_id=None, default_max_turns=20):
        self.session_id = session_id
        self.default_max_turns = default_max_turns
        self.calls: list[tuple] = []
        self.state = types.SimpleNamespace(goal="ship the feature")
        _FakeManager.instances.append(self)

    # --- surface used by the dispatcher ---------------------------------
    def has_goal(self):
        return True

    def clear(self):
        self.calls.append(("clear",))

        return True

    def set(self, arg, **_kwargs):
        self.calls.append(("set", arg))

        return types.SimpleNamespace(goal=arg, max_turns=self.default_max_turns)

    def confirm_completion(self, evidence, *, source="user"):
        self.calls.append(("complete", evidence, source))

        return self.complete_result

    def status_line(self):
        return "⊙ Goal active · running (4/20 turns): ship the feature"


@pytest.fixture
def dispatch(monkeypatch):
    session = {
        "session_key": "stored-session",
        "history_lock": __import__("threading").Lock(),
        "running": False,
        "agent": types.SimpleNamespace(),
    }
    monkeypatch.setitem(server._sessions, "sid", session)
    # Handlers are rebound onto server's globals by HandlerRegistry.install(),
    # so the config seam to patch is server's, not methods_tools'.
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr("hermes_cli.goals.GoalManager", _FakeManager)
    _FakeManager.instances.clear()
    _FakeManager.complete_result = True

    def _call(arg: str, name: str = "goal"):
        return server._methods["command.dispatch"](
            "r1", {"name": name, "arg": arg, "session_id": "sid"}
        )

    _call.manager = lambda: _FakeManager.instances[-1]

    return _call


def _output(response):
    assert "result" in response, response
    return response["result"].get("output", "")


def test_complete_with_evidence_records_user_completion(dispatch):
    response = dispatch('complete "pytest 412 passed"')

    out = _output(response)
    assert out.startswith("✓ Goal done")
    assert "ship the feature" in out
    assert dispatch.manager().calls == [
        ("complete", '"pytest 412 passed"', "user-command")
    ]


def test_done_with_evidence_completes_instead_of_replacing_the_goal(dispatch):
    """Bare `done` clears, but `done <text>` is evidence on every surface."""
    dispatch('done "CI green on #188"')

    assert dispatch.manager().calls == [("complete", '"CI green on #188"', "user-command")]


def test_complete_without_evidence_is_a_usage_error_and_mutates_nothing(dispatch):
    response = dispatch("complete")

    assert "Usage: /goal complete <evidence>" in _output(response)
    assert dispatch.manager().calls == []


def test_bare_done_still_clears(dispatch):
    dispatch("done")

    assert dispatch.manager().calls == [("clear",)]


def test_plain_prose_still_sets_a_new_goal(dispatch):
    dispatch("ship the migration")

    assert dispatch.manager().calls == [("set", "ship the migration")]


def test_complete_on_no_active_goal_reports_without_creating_one(dispatch):
    dispatch("status")  # exercise the handler once for realism
    _FakeManager.complete_result = False

    response = dispatch('complete "nothing to finish"')

    assert _output(response) == "No active goal to complete."
    assert all(call[0] != "set" for call in _FakeManager.instances[-1].calls)
