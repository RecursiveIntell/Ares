"""Task 2.3 guard: turning the legacy canary off must not remove an enrolled session's guards.

The native operations doc states: "canary configuration does not remove an enrolled
session's admission guards." Any future retirement of the legacy canary must keep this
true. This test pins the invariant at the code that implements it, so a removal that
breaks it fails here before it reaches a release.

It does not retire the canary and changes no behavior.
"""

import pytest

from ares_runtime.continuity import runtime as runtime_mod
from ares_runtime import collaboration as collab


class _Ctx:
    session_id = "enrolled-session"


def test_enrolled_binding_is_returned_regardless_of_canary_flags(monkeypatch):
    registration = {"transport": "t", "approval_verifier": "v"}
    binding = (object(), object(), "sess", "holder", "attempt", registration, "route")

    token = runtime_mod._tool_control.set(binding)
    try:
        # Canary off: the legacy enablement reports False for this session.
        monkeypatch.setattr(collab, "production_permit_canary_enabled", lambda session_id=None: False)
        monkeypatch.setattr(runtime_mod, "assert_context_tool_control_current", lambda session_id=None: None)
        got = runtime_mod.native_tool_control_binding(session_id="sess")
    finally:
        runtime_mod._tool_control.reset(token)

    assert got is binding, "an enrolled session must keep its binding when the canary is off"


def test_unenrolled_session_has_no_native_binding(monkeypatch):
    monkeypatch.setattr(runtime_mod, "assert_context_tool_control_current", lambda session_id=None: None)
    assert runtime_mod.native_tool_control_binding(session_id="none") is None


def test_binding_without_registration_is_not_treated_as_enrolled(monkeypatch):
    binding = (object(), object(), "sess", "holder", "attempt", None, "route")
    token = runtime_mod._tool_control.set(binding)
    try:
        monkeypatch.setattr(runtime_mod, "assert_context_tool_control_current", lambda session_id=None: None)
        assert runtime_mod.native_tool_control_binding(session_id="sess") is None
    finally:
        runtime_mod._tool_control.reset(token)
