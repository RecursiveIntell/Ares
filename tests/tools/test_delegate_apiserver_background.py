"""delegate_task(background=true) on stateless API-server sessions.

Previously async_delivery_supported()=False forced SYNCHRONOUS execution for
every background dispatch on the API server, blocking the whole turn. Now
that background completions can wake the originating session via the
/v1/chat/completions self-post (gateway/wake.py), a session-continuable
turn (raw session id bound as the api_server chat_id) dispatches async; only
session-id-less one-shot requests keep the sync fallback.

The wake target must be captured from the request-scoped chat_id binding,
NOT from HERMES_SESSION_ID: constructing a child agent calls
set_current_session_id(child.session_id), clobbering the HERMES_SESSION_ID
ContextVar and os.environ with the subagent's internal id before the
dispatch code reads it — the fake child build below reproduces that clobber.
"""

import json
import time
from unittest.mock import MagicMock

import pytest

from gateway.session_context import set_session_vars
from tools.process_registry import process_registry


@pytest.fixture(autouse=True)
def _clean_queue_and_context(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    while not process_registry.completion_queue.empty():
        try:
            process_registry.completion_queue.get_nowait()
        except Exception:
            break
    yield
    # Restore ContextVars to the pristine "never set" sentinel rather than
    # clear_session_vars()'s explicit-"" state, which would mask env vars for
    # unrelated tests running later in the same worker.
    import gateway.session_context as sc

    for var in sc._VAR_MAP.values():
        var.set(sc._UNSET)
    sc._SESSION_ASYNC_DELIVERY.set(sc._UNSET)
    # set_current_session_id (invoked by the clobber-reproducing fake child
    # build) writes os.environ directly — scrub it so it can't leak into
    # other test modules.
    import os

    os.environ.pop("HERMES_SESSION_ID", None)
    while not process_registry.completion_queue.empty():
        try:
            process_registry.completion_queue.get_nowait()
        except Exception:
            break


def _drain_one(timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not process_registry.completion_queue.empty():
            return process_registry.completion_queue.get_nowait()
        time.sleep(0.02)
    return None


def _fake_parent():
    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None
    # Explicitly set orchestration attributes so getattr returns bool, not MagicMock
    parent._orchestration_opt_out = False
    parent._orchestration_admission = None
    parent._delegate_depth = 0
    return parent


def _patch_delegate(monkeypatch):
    import tools.delegate_tool as dt

    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"
    fake_child._subagent_id = "s1"

    def fast_child(task_index, goal, child=None, parent_agent=None, **kw):
        return {
            "task_index": 0, "status": "completed", "summary": f"done: {goal}",
            "api_calls": 1, "duration_seconds": 0.1, "model": "m",
            "exit_reason": "completed",
        }

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    def clobbering_build_child(**kw):
        # Reproduce what the real _build_child_agent -> AIAgent -> agent_init
        # path does: it synchronizes the child's internal session id into the
        # HERMES_SESSION_ID ContextVar + os.environ, clobbering the spawner's
        # id ~milliseconds before delegate_tool dispatches the batch.
        from gateway.session_context import set_current_session_id

        set_current_session_id("20260715_child1")
        return fake_child

    monkeypatch.setattr(dt, "_build_child_agent", clobbering_build_child)
    monkeypatch.setattr(dt, "_run_single_child", fast_child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    return dt


def test_apiserver_session_with_id_dispatches_background(monkeypatch):
    """async_delivery=False + a raw session id (HERMES_SESSION_ID) →
    background dispatch (the completion wakes the session via the
    api_server self-post), NOT the forced-sync fallback."""
    dt = _patch_delegate(monkeypatch)
    monkeypatch.setenv("HERMES_SESSION_ID", "raw-sid-7")
    set_session_vars(
        platform="api_server",
        chat_id="raw-sid-7",
        session_key="raw-sid-7",
        session_id="raw-sid-7",
        async_delivery=False,
    )

    out = dt.delegate_task(
        goal="bg on api_server", context="ctx",
        background=True, parent_agent=_fake_parent(),
    )
    parsed = json.loads(out)
    assert parsed["status"] == "dispatched", parsed
    assert parsed["mode"] == "background"

    evt = _drain_one()
    assert evt is not None
    assert evt["type"] == "async_delegation"
    # The raw session id is stamped so the gateway drain can self-post the
    # wake to the REAL session (session_key alone is the raw id here, which
    # carries no parseable routing metadata). Crucially this is the SPAWNER's
    # id, not the subagent-internal id the child build clobbered
    # HERMES_SESSION_ID with (see clobbering_build_child).
    assert evt["origin_session_id"] == "raw-sid-7"


# ---------------------------------------------------------------------------
# _current_origin_session_id — the clobber-proof origin capture helper
# ---------------------------------------------------------------------------


def test_apiserver_session_without_id_stays_synchronous(monkeypatch):
    """No session id to wake → keep the sync fallback (a detached result
    would never re-enter any conversation)."""
    dt = _patch_delegate(monkeypatch)
    set_session_vars(
        platform="api_server",
        chat_id="",
        session_key="",
        session_id="",
        async_delivery=False,
    )

    out = dt.delegate_task(
        goal="one-shot", context="ctx",
        background=True, parent_agent=_fake_parent(),
    )
    parsed = json.loads(out)
    assert parsed.get("status") != "dispatched", parsed
    assert "SYNCHRONOUSLY" in parsed.get("note", "")
    assert process_registry.completion_queue.empty()


# --- EDGES card (P5): graph-route admission seam (default-off) ---
# One caller row: delegate_task(background=True) on api_server. The seam
# decides graph vs legacy BEFORE dispatch; default-off keeps legacy exactly.

def test_graph_route_default_off_keeps_legacy_dispatch(monkeypatch):
    dt = _patch_delegate(monkeypatch)
    monkeypatch.setenv("HERMES_SESSION_ID", "raw-sid-edge-1")
    set_session_vars(platform="api_server", chat_id="raw-sid-edge-1",
                     session_key="raw-sid-edge-1", session_id="raw-sid-edge-1",
                     async_delivery=False)
    decision = dt.graph_route_decision(parent_agent=_fake_parent(), background=True)
    assert decision["route"] == "legacy"
    assert decision["reason"] == "no_explicit_graph_request"
    out = dt.delegate_task(goal="edge default-off", background=True,
                           parent_agent=_fake_parent())
    parsed = json.loads(out)
    assert parsed["status"] == "dispatched"
    assert parsed["mode"] == "background"
    assert parsed.get("backend_generation", "legacy") == "legacy"


def test_graph_route_enabled_in_test_selects_graph_once(monkeypatch):
    dt = _patch_delegate(monkeypatch)
    monkeypatch.setattr(dt, "_load_config",
                        lambda: {"graph_route_enabled": True, "graph_route_allowlist": ["research-v1"]})
    decision = dt.graph_route_decision(parent_agent=_fake_parent(), background=True,
                                       graph_id="research-v1")
    assert decision["route"] == "graph"
    assert decision["reason"] == "explicit_graph_opt_in"
    assert decision["backend_generation"] == "graph-v1"
    # Opt-out is a block for an explicit graph request, never a legacy substitute.
    parent = _fake_parent()
    parent._orchestration_opt_out = True
    decision2 = dt.graph_route_decision(parent_agent=parent, background=True,
                                        graph_id="research-v1")
    assert decision2["route"] == "blocked"
    assert decision2["reason"] == "caller_opt_out"


def test_graph_route_never_recursive_for_child_callers(monkeypatch):
    dt = _patch_delegate(monkeypatch)
    monkeypatch.setattr(dt, "_load_config",
                        lambda: {"graph_route_enabled": True})
    parent = _fake_parent()
    parent._delegate_depth = 1
    decision = dt.graph_route_decision(parent_agent=parent, background=True,
                                       graph_id="research-v1")
    assert decision["route"] == "blocked"
    assert decision["reason"] == "nested_worker_not_graph_eligible"


def test_graph_route_sync_calls_stay_legacy(monkeypatch):
    dt = _patch_delegate(monkeypatch)
    monkeypatch.setattr(dt, "_load_config",
                        lambda: {"graph_route_enabled": True})
    decision = dt.graph_route_decision(parent_agent=_fake_parent(), background=False)
    assert decision["route"] == "legacy"
    assert decision["reason"] == "no_explicit_graph_request"


# --- EDGES card (P5): model-dispatch seam carries the host admission ---
# run_agent._dispatch_delegate_task pre-admits and forwards the admission to
# delegate_task. These exercise the REAL seam (delegate_task is NOT mocked):
# the forwarded kwarg must be accepted, the effect must match the request,
# and a stale admission must fail closed. The earlier mocked-seam tests could
# not see the TypeError this row fixes.

def _real_seam_parent(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hermes_constants import set_hermes_home_override
    from hermes_state import SessionDB
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    token = set_hermes_home_override(home)
    db = SessionDB(home / "state.db")
    db.create_session("edge-parent", "cli", model="main-model")
    parent = SimpleNamespace(session_id="edge-parent", _session_db=db,
                             model="main-model", provider="main-provider",
                             _delegate_depth=0, enabled_toolsets=["file"],
                             reasoning_config={"enabled": True, "effort": "high"},
                             _orchestration_opt_out=False,
                             _orchestration_admission=None)
    return parent, token


def test_model_dispatch_seam_accepts_forwarded_admission(tmp_path, monkeypatch):
    from run_agent import AIAgent
    from hermes_constants import reset_hermes_home_override
    parent, token = _real_seam_parent(tmp_path, monkeypatch)
    try:
        dt = _patch_delegate(monkeypatch)
        monkeypatch.setenv("HERMES_SESSION_ID", "raw-sid-edge-seam")
        set_session_vars(platform="api_server", chat_id="raw-sid-edge-seam",
                         session_key="raw-sid-edge-seam", session_id="raw-sid-edge-seam",
                         async_delivery=False)
        out = AIAgent._dispatch_delegate_task(
            parent, {"goal": "real seam legacy task"})
        parsed = json.loads(out)
        assert parsed["status"] == "dispatched", parsed
        assert parsed.get("backend_generation", "legacy") == "legacy"
    finally:
        reset_hermes_home_override(token)
        parent._session_db.close()


def test_model_dispatch_graph_request_uses_graph_effect(tmp_path, monkeypatch):
    from run_agent import AIAgent
    from hermes_constants import reset_hermes_home_override
    from agent.orchestration_policy import OrchestrationPolicy
    parent, token = _real_seam_parent(tmp_path, monkeypatch)
    try:
        dt = _patch_delegate(monkeypatch)
        # Operator disables graph for this session: the forwarded admission
        # must be re-checked under the GRAPH effect and refuse, not fall back.
        parent._session_db.set_orchestration_policy(
            "edge-parent", OrchestrationPolicy(allow_graph=False).to_dict(),
            expected_revision=0, origin="operator-command")
        monkeypatch.setattr(dt, "_load_config",
                            lambda: {"graph_route_enabled": True,
                                     "graph_route_allowlist": ["research-v1"]})
        out = AIAgent._dispatch_delegate_task(parent, {
            "goal": "graph via model seam", "graph_id": "research-v1",
            "graph_version": "sha256:" + "a" * 64,
            "graph_input": {"source": "frozen"},
            "graph_budgets": {"max_llm_calls": 2, "max_wall_clock_ms": 30000},
            "graph_operation_id": "edge-seam-graph-0001"})
        parsed = json.loads(out)
        assert parsed["status"] == "blocked", parsed
        assert parsed["code"] == "graph_disabled", parsed
        assert parsed.get("provider_calls") == 0
    finally:
        reset_hermes_home_override(token)
        parent._session_db.close()


def test_model_dispatch_stale_admission_fails_closed(tmp_path, monkeypatch):
    from run_agent import AIAgent
    from hermes_constants import reset_hermes_home_override
    from agent.orchestration_policy import OrchestrationPolicy
    from agent.orchestration_runtime import admit_worker
    parent, token = _real_seam_parent(tmp_path, monkeypatch)
    try:
        dt = _patch_delegate(monkeypatch)
        admission = admit_worker(parent, effect="delegation")
        # Policy revision moves AFTER the host admission was issued: the
        # forwarded snapshot is stale and must be refused, not reused.
        parent._session_db.set_orchestration_policy(
            "edge-parent", OrchestrationPolicy(allow_delegation=False).to_dict(),
            expected_revision=0, origin="operator-command")
        out = dt.delegate_task(goal="stale admission", background=True,
                               parent_agent=parent,
                               _orchestration_admission=admission)
        parsed = json.loads(out)
        assert parsed["status"] == "blocked", parsed
        assert parsed["code"] in {"orchestration_revision_changed",
                                  "orchestration_policy_changed",
                                  "delegation_disabled"}, parsed
    finally:
        reset_hermes_home_override(token)
        parent._session_db.close()
