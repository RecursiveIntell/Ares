"""F2.1: the CLI mixin's SET, DRAFT and RESUME kickoff paths queue through the
_queue_internal_input shim when present (never a bare _pending_input.put).

Exercised as BEHAVIOR — a recording shim observes the queued continuation —
rather than by inspecting handler source (root AGENTS.md bans source-reading
tests).
"""
from types import SimpleNamespace

import pytest

from hermes_cli import cli_commands_mixin as mixin
from hermes_cli import goals


def _state(goal: str = "ship the fix"):
    return SimpleNamespace(
        goal=goal,
        max_turns=50,
        has_contract=lambda: False,
        contract=SimpleNamespace(render_block=lambda: ""),
    )


def _make_obj(*, shim: bool, mgr):
    calls: list = []
    obj = SimpleNamespace(
        _queue_internal_input=(
            (lambda text, **kw: calls.append((text, kw))) if shim else None
        ),
        _pending_input=SimpleNamespace(put=lambda _: None),
        _get_goal_manager=lambda: mgr,
    )
    return obj, calls


def test_set_kickoff_uses_shim():
    mgr = SimpleNamespace(session_id="sess", set=lambda text, contract=None: _state(text))
    obj, calls = _make_obj(shim=True, mgr=mgr)
    mixin.CLICommandsMixin._handle_goal_command(obj, "ship the fix")
    assert calls == [("ship the fix", {"source": "goal_continuation"})]


def test_set_kickoff_falls_back_to_raw_queue():
    mgr = SimpleNamespace(session_id="sess", set=lambda text, contract=None: _state(text))
    obj, calls = _make_obj(shim=False, mgr=mgr)
    obj._pending_input.put = lambda text: calls.append((text, {}))
    mixin.CLICommandsMixin._handle_goal_command(obj, "ship the fix")
    assert calls == [("ship the fix", {})]


def test_draft_kickoff_uses_shim(monkeypatch):
    monkeypatch.setattr(goals, "draft_contract", lambda objective, session_id=None: None)
    mgr = SimpleNamespace(session_id="sess", set=lambda text, contract=None: _state(text))
    obj, calls = _make_obj(shim=True, mgr=mgr)
    mixin.CLICommandsMixin._handle_goal_draft(obj, "ship the fix")
    assert calls == [("ship the fix", {"source": "goal_continuation"})]


def test_resume_kickoff_uses_shim():
    state = _state("ship the fix")
    mgr = SimpleNamespace(
        session_id="sess",
        set=lambda text, contract=None: state,
        resume=lambda: state,
        next_continuation_prompt=lambda: "continue now",
    )
    obj, calls = _make_obj(shim=True, mgr=mgr)
    mixin.CLICommandsMixin._handle_goal_command(obj, "/goal resume")
    assert calls == [("continue now", {"source": "goal_continuation"})]
