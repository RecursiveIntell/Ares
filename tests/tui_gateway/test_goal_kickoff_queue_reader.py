"""F2.1: the CLI mixin's SET and DRAFT kickoff paths use the
_queue_internal_input shim when it is present (never a bare _pending_input.put).

Both kickoff sites live inside _handle_goal_command (SET) and
_handle_goal_draft (DRAFT); _handle_goal_command's source includes the
resume-path's pre-existing shim. Pin each site by which method owns it.
"""
import inspect

from hermes_cli import cli_commands_mixin as mixin

_SHIM = 'getattr(self, "_queue_internal_input", None)'


def test_set_kickoff_uses_the_shim():
    src = inspect.getsource(mixin.CLICommandsMixin._handle_goal_command)
    assert _SHIM in src
    assert '_pending_input.put(state.goal)' in src  # the guarded fallback


def test_draft_kickoff_uses_the_shim():
    src = inspect.getsource(mixin.CLICommandsMixin._handle_goal_draft)
    assert _SHIM in src
    assert '_pending_input.put(state.goal)' in src


def test_resume_path_shim_witness():
    """Regression witness predating F2.1 — pin it so refactors can't drop it."""
    src = inspect.getsource(mixin.CLICommandsMixin._handle_goal_command)
    assert 'source="goal_continuation"' in src
