"""ROUTE card: one explicit background delegation caller through AgentGraph.
Default-off, allowlisted-only, durable operation identity, no silent legacy
fallback, and original-origin delivery owner preserved.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

from tools.delegate_tool import graph_route_decision, _dispatch_graph_background


def test_graph_handler_prefers_mcp_structured_content_when_text_is_also_present():
    from tools.delegate_tool import _graph_handler_value

    structured = {"ok": True, "data": {"run_id": "run-structured"}}
    raw = json.dumps({
        "result": "The workflow was accepted.",
        "structuredContent": structured,
    })

    assert _graph_handler_value(raw) == structured


def test_canonical_receipt_rejects_run_or_graph_identity_mismatch():
    from copy import deepcopy
    from tools.delegate_tool import _verified_terminal_receipt

    run_id = "run-fixture"
    version = "sha256:" + "a" * 64
    receipt = {"schema": "agent-graph-mcp-receipt-v2", "run_id": run_id,
               "graph_version": version, "persistence_status": "durable_terminal",
               "error_code": None}
    response = {"ok": True, "run_id": run_id, "data": {"receipt": receipt,
        "receipt_digest": "hmac-sha256:" + "b" * 64,
        "storage_class": "sqlite_terminal_receipt",
        "bundle": {"payload": {"run_id": run_id, "graph_version": version,
                   "receipt": receipt, "output": {"answer": "ok"}}}}}
    assert _verified_terminal_receipt(response, run_id, version)[0] is not None

    wrong_run = deepcopy(response)
    wrong_run["data"]["receipt"]["run_id"] = "another-run"
    assert _verified_terminal_receipt(wrong_run, run_id, version) == (
        None, "canonical_receipt_identity_or_integrity_invalid")

    wrong_version = deepcopy(response)
    wrong_version["data"]["bundle"]["payload"]["graph_version"] = "sha256:" + "c" * 64
    assert _verified_terminal_receipt(wrong_version, run_id, version) == (
        None, "canonical_receipt_bundle_mismatch")


def test_canonical_receipt_real_daemon_shape_and_usage_redaction():
    """The daemon returns run_id at top level and redacts usage in the bundle.

    A genuine completion must still verify; a non-usage field difference must
    still be rejected.
    """
    from copy import deepcopy
    from tools.delegate_tool import _verified_terminal_receipt

    run_id = "run-real-shape"
    version = "sha256:" + "d" * 64
    durable = {"schema": "agent-graph-mcp-receipt-v2", "run_id": run_id,
               "graph_version": version, "persistence_status": "durable_terminal",
               "error_code": None,
               "llm_invocations": [{"node_id": "s1", "usage": {"prompt_tokens": 42}}]}
    bundled = deepcopy(durable)
    bundled["llm_invocations"][0]["usage"]["prompt_tokens"] = "[REDACTED]"
    response = {"ok": True, "run_id": run_id,
                "data": {"receipt": durable, "receipt_digest": "hmac-sha256:" + "e" * 64,
                         "storage_class": "sqlite_terminal_receipt",
                         "bundle": {"payload": {"run_id": run_id, "graph_version": version,
                                                "receipt": bundled, "output": {"answer": "ok"}}}}}
    assert _verified_terminal_receipt(response, run_id, version)[0] is not None

    # A difference outside the documented usage redaction is a real mismatch.
    tampered = deepcopy(response)
    tampered["data"]["bundle"]["payload"]["receipt"]["error_code"] = "FORGED"
    assert _verified_terminal_receipt(tampered, run_id, version) == (
        None, "canonical_receipt_bundle_mismatch")

    # Conflicting inner run_id is rejected rather than silently ignored.
    conflict = deepcopy(response)
    conflict["data"]["run_id"] = "another-run"
    assert _verified_terminal_receipt(conflict, run_id, version) == (
        None, "canonical_receipt_identity_or_integrity_invalid")


def parent(depth=0, optout=False):
    return SimpleNamespace(session_id="route-owner", _delegate_depth=depth,
                           _orchestration_opt_out=optout,
                           _orchestration_admission=None)


def test_no_graph_id_retains_legacy_default():
    with patch("tools.delegate_tool._load_config", return_value={"graph_route_enabled": True,
                                                                    "graph_route_allowlist": ["research-v1"]}):
        d = graph_route_decision(parent_agent=parent(), background=True)
    assert d == {"route":"legacy", "reason":"no_explicit_graph_request"}


def test_explicit_graph_request_when_disabled_is_blocked_not_substituted():
    with patch("tools.delegate_tool._load_config", return_value={}):
        d = graph_route_decision(parent_agent=parent(), background=True,
                                 graph_id="research-v1")
    assert d["route"] == "blocked"
    assert d["reason"] == "graph_route_disabled"


def test_allowlisted_explicit_graph_route_and_nonallowlisted_denial():
    cfg = {"graph_route_enabled": True, "graph_route_allowlist": ["research-v1"]}
    with patch("tools.delegate_tool._load_config", return_value=cfg):
        good = graph_route_decision(parent_agent=parent(), background=True,
                                   graph_id="research-v1")
        bad = graph_route_decision(parent_agent=parent(), background=True,
                                  graph_id="other-graph")
    assert good["route"] == "graph"
    assert good["backend_generation"] == "graph-v1"
    assert bad["route"] == "blocked"
    assert bad["reason"] == "graph_id_not_allowlisted"


def test_optout_nested_and_sync_graph_requests_are_blocked():
    cfg = {"graph_route_enabled": True, "graph_route_allowlist": ["research-v1"]}
    with patch("tools.delegate_tool._load_config", return_value=cfg):
        cases = [
            graph_route_decision(parent_agent=parent(optout=True), background=True, graph_id="research-v1"),
            graph_route_decision(parent_agent=parent(depth=1), background=True, graph_id="research-v1"),
            graph_route_decision(parent_agent=parent(), background=False, graph_id="research-v1"),
        ]
    assert [x["route"] for x in cases] == ["blocked", "blocked", "blocked"]
    assert [x["reason"] for x in cases] == ["caller_opt_out", "nested_worker_not_graph_eligible", "graph_route_requires_background"]


def _install_mock_dispatch(monkeypatch, *, start_result=None, wait_results=None, receipt_result=None):
    from tools import delegate_tool as dt
    calls = []
    runner_result = {}
    def mcp(parent_agent, tool, args):
        calls.append((tool, args))
        if tool == "graph_run_start":
            return start_result or {"ok":True,"data":{"run_id":"run-fixture","status":"running"}}
        if tool == "graph_run_receipt":
            if receipt_result is not None:
                return receipt_result
            receipt = {"schema": "agent-graph-mcp-receipt-v2", "run_id": "run-fixture",
                       "graph_version": "sha256:" + "a" * 64,
                       "persistence_status": "durable_terminal", "error_code": None}
            return {"ok": True, "run_id": "run-fixture", "data": {"receipt": receipt,
                    "receipt_digest": "hmac-sha256:" + "b" * 64,
                    "storage_class": "sqlite_terminal_receipt",
                    "bundle": {"payload": {"run_id": "run-fixture",
                               "graph_version": "sha256:" + "a" * 64,
                               "receipt": receipt,
                               "output": {"research_result": {"answer": "verified"}}}}}}
        if tool == "graph_run_wait":
            if wait_results:
                return wait_results.pop(0)
            return {"ok":True,"data":{"status":"completed","success":True,
                "final_state":{"research_result":{"recommendation":"REC [S1]", "counterevidence":"CNTR [S2]"}},
                "budget_counters":{"llm_calls":2}}}
        return {"ok":True}
    def dispatch(**kwargs):
        runner_result.update(kwargs["runner"]())
        return {"status":"dispatched","delegation_id":"deleg-fixture"}
    monkeypatch.setattr(dt, "_graph_mcp_call", mcp)
    # delegate_tool imports these names function-locally from async_delegation,
    # so patch the source module. The bind mock returns a complete typed result
    # (the route now checks the bind outcome instead of ignoring it).
    monkeypatch.setattr("tools.async_delegation.dispatch_async_delegation", dispatch)
    monkeypatch.setattr("tools.async_delegation.bind_graph_backend",
                        lambda operation_key, **kw: {"ok": True, "delegation_id": "deleg-fixture"})
    return calls, runner_result


def test_immediately_finished_graph_worker_does_not_leave_stale_control_handle(durable_home, monkeypatch):
    import tools.delegate_tool as dt

    kwargs = _route_kwargs(graph_operation_id="research-immediate-refusal-01")
    monkeypatch.setattr(dt, "_graph_mcp_call", lambda *a, **k: {
        "ok": False, "error_code": "GRAPH_PREFLIGHT_REFUSED",
    })
    workers = []

    def finish_before_dispatch_returns(**dispatch_kwargs):
        worker = threading.Thread(target=dispatch_kwargs["runner"])
        workers.append(worker)
        worker.start()
        worker.join(timeout=3)
        assert not worker.is_alive(), "immediate graph worker did not finish"
        return {"status": "dispatched", "delegation_id": "immediate-fixture"}

    monkeypatch.setattr("tools.async_delegation.dispatch_async_delegation",
                        finish_before_dispatch_returns)
    result = json.loads(_dispatch_graph_background(**kwargs))
    assert result["status"] == "dispatched"
    with dt._active_subagents_lock:
        assert result["handle_id"] not in dt._active_subagents


def test_graph_background_refuses_when_async_delivery_has_no_wake_session(durable_home, monkeypatch):
    dispatched = []
    monkeypatch.setattr("gateway.session_context.async_delivery_supported", lambda: False)
    monkeypatch.setattr("tools.async_delegation._current_origin_session_id", lambda: "")
    monkeypatch.setattr("tools.async_delegation.dispatch_async_delegation",
                        lambda **kw: dispatched.append(kw) or {"status": "dispatched"})

    out = json.loads(_dispatch_graph_background(**_route_kwargs()))

    assert out["status"] == "blocked"
    assert out["code"] == "graph_async_delivery_unavailable"
    assert out["provider_calls"] == 0
    assert out["legacy_fallback"] is False
    assert dispatched == []


def test_graph_background_allows_api_server_wake_session_without_push_delivery(durable_home, monkeypatch):
    monkeypatch.setattr("gateway.session_context.async_delivery_supported", lambda: False)
    monkeypatch.setattr("gateway.session_context.get_session_env",
                        lambda name, default="": "api_server" if name == "HERMES_SESSION_PLATFORM" else default)
    monkeypatch.setattr("tools.async_delegation._current_origin_session_id", lambda: "api-session-17")
    _install_mock_dispatch(monkeypatch)

    out = json.loads(_dispatch_graph_background(**_route_kwargs()))

    assert out["status"] == "dispatched"
    assert out["origin_session_id"] == "api-session-17"


def test_allowlisted_graph_runs_once_and_never_calls_legacy(durable_home, monkeypatch):
    p=parent()
    cfg={"graph_route_enabled":True,"graph_route_allowlist":["research-v1"]}
    monkeypatch.setattr("tools.delegate_tool._load_config",lambda:cfg)
    calls, runner_result = _install_mock_dispatch(monkeypatch)
    out=json.loads(_dispatch_graph_background(
        parent_agent=p, admission=SimpleNamespace(resolved_optional_route=SimpleNamespace(model="gpt-6-luna")),
        graph_id="research-v1", graph_version="sha256:"+"a"*64,
        graph_input={"source":"frozen"}, graph_budgets={"max_llm_calls":2,"max_wall_clock_ms":30000},
        graph_operation_id="research-operation-0001", goal="Research F1", context=None))
    assert out["status"] == "dispatched"
    assert out["legacy_fallback"] is False
    assert [name for name,_ in calls] == ["graph_run_start","graph_run_wait","graph_run_receipt"]
    assert runner_result["status"] == "completed"
    assert runner_result["receipt_status"] == "verified"
    assert runner_result["summary"] == json.dumps({"research_result": {"answer": "verified"}})
    assert runner_result["graph_run_id"] == "run-fixture"


def test_graph_completion_without_canonical_receipt_is_unknown_not_success(durable_home, monkeypatch):
    monkeypatch.setattr("tools.delegate_tool._load_config",
        lambda: {"graph_route_enabled": True, "graph_route_allowlist": ["research-v1"]})
    calls, runner_result = _install_mock_dispatch(
        monkeypatch, receipt_result={"ok": False, "error_code": "RUN_NOT_FOUND"})
    _dispatch_graph_background(
        parent_agent=parent(),
        admission=SimpleNamespace(resolved_optional_route=SimpleNamespace(model="gpt-6-luna")),
        graph_id="research-v1", graph_version="sha256:" + "a" * 64,
        graph_input={"source": "frozen"},
        graph_budgets={"max_llm_calls": 2, "max_wall_clock_ms": 30000},
        graph_operation_id="research-operation-receipt-missing", goal="Research F1", context=None)
    assert runner_result["status"] == "unknown"
    assert runner_result["receipt_status"] == "unverified"
    assert runner_result["error"] == "canonical_receipt_unavailable"
    assert [name for name, _ in calls] == ["graph_run_start", "graph_run_wait", "graph_run_receipt"]


def test_graph_start_denial_is_typed_and_never_legacy(durable_home, monkeypatch):
    p=parent()
    monkeypatch.setattr("tools.delegate_tool._load_config",
        lambda:{"graph_route_enabled":True,"graph_route_allowlist":["research-v1"]})
    calls, runner_result = _install_mock_dispatch(monkeypatch,
        start_result={"ok":False,"error_code":"HOST_OWNER_DENIED"})
    out=json.loads(_dispatch_graph_background(
        parent_agent=p, admission=SimpleNamespace(resolved_optional_route=SimpleNamespace(model="gpt-6-luna")),
        graph_id="research-v1", graph_version="sha256:"+"a"*64,
        graph_input={"source":"frozen"}, graph_budgets={"max_llm_calls":2,"max_wall_clock_ms":30000},
        graph_operation_id="research-operation-0002", goal="Research F1", context=None))
    assert out["status"] == "dispatched"  # outer durable wrapper admitted
    assert runner_result["status"] == "blocked"
    assert runner_result["error"] == "HOST_OWNER_DENIED"
    assert [name for name,_ in calls] == ["graph_run_start"]


def test_bad_pin_budget_or_operation_key_is_blocked_before_runner(monkeypatch):
    p=parent()
    monkeypatch.setattr("tools.delegate_tool._load_config",
        lambda:{"graph_route_enabled":True,"graph_route_allowlist":["research-v1"]})
    for kwargs, code in [
        ({"graph_version":"bad"},"graph_version_pin_required"),
        ({"graph_budgets":{}},"graph_budget_required"),
        ({"graph_operation_id":"x"},"graph_operation_id_invalid"),
    ]:
        base={"graph_version":"sha256:"+"a"*64,
              "graph_budgets":{"max_llm_calls":2,"max_wall_clock_ms":30000},
              "graph_operation_id":"research-operation-0003"}
        base.update(kwargs)
        calls, runner_result = _install_mock_dispatch(monkeypatch)
        out=json.loads(_dispatch_graph_background(
            parent_agent=p, admission=SimpleNamespace(resolved_optional_route=SimpleNamespace(model="gpt-6-luna")),
            graph_id="research-v1", graph_input={"source":"frozen"},
            goal="x", context=None, **base))
        assert out["status"] == "blocked" and out["code"] == code
        assert not calls


def test_wait_failure_is_not_rewritten_as_success(durable_home, monkeypatch):
    p=parent()
    monkeypatch.setattr("tools.delegate_tool._load_config",
        lambda:{"graph_route_enabled":True,"graph_route_allowlist":["research-v1"]})
    waits=[{"ok":True,"data":{"status":"failed","success":False,"error":"PROVIDER_FAILURE","final_state":None,"budget_counters":{"llm_calls":1}}}]
    calls, runner_result = _install_mock_dispatch(monkeypatch, wait_results=waits)
    out=json.loads(_dispatch_graph_background(
        parent_agent=p, admission=SimpleNamespace(resolved_optional_route=SimpleNamespace(model="gpt-6-luna")),
        graph_id="research-v1", graph_version="sha256:"+"a"*64,
        graph_input={"source":"frozen"}, graph_budgets={"max_llm_calls":2,"max_wall_clock_ms":30000},
        graph_operation_id="research-operation-0004", goal="Research F1", context=None))
    assert out["status"] == "dispatched"
    assert runner_result["status"] == "error"
    assert runner_result["error"] == "PROVIDER_FAILURE"


# ── ROUTE durable-identity / control / negative witnesses ──────────────────
# These exercise the REAL async_delegation durable ledger (tmp HERMES_HOME),
# not a mocked dispatch: same operation reattaches without a second graph
# run or delivery; changed payload conflicts; cancel reaches graph_run_cancel
# through the registered control handle; deadline/graph-down stay typed.

import queue
import threading
import time

import pytest

from tools import async_delegation as ad
from tools.process_registry import process_registry


@pytest.fixture(autouse=True)
def durable_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("tools.delegate_tool._load_config",
                        lambda: {"graph_route_enabled": True,
                                 "graph_route_allowlist": ["research-v1"]})
    monkeypatch.setattr("tools.approval.get_current_session_key", lambda: "origin-A")
    monkeypatch.setattr("tools.async_delegation._current_origin_session_id", lambda: "origin-A")
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    deadline = time.monotonic() + 3.0
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _wait_idle(timeout=8.0):
    deadline = time.monotonic() + timeout
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ad.active_count() == 0, "worker did not reach terminal state"


def _drain_for(delegation_id, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            evt = process_registry.completion_queue.get_nowait()
            if evt.get("delegation_id") == delegation_id:
                return evt
            continue
        time.sleep(0.02)
    raise AssertionError(f"no completion event for {delegation_id}")


def _route_kwargs(**over):
    base = dict(
        parent_agent=parent(), admission=SimpleNamespace(resolved_optional_route=None),
        graph_id="research-v1", graph_version="sha256:" + "a" * 64,
        graph_input={"source": "frozen"},
        graph_budgets={"max_llm_calls": 2, "max_wall_clock_ms": 30000},
        graph_operation_id="research-operation-live-01", goal="Research F1", context=None)
    base.update(over)
    return base


def _counting_mcp(monkeypatch, *, wait_results=None, hold_until_cancel=False):
    from tools import delegate_tool as dt
    calls = []
    wait_results = list(wait_results or [])
    cancelled = threading.Event()

    def mcp(parent_agent, tool, args):
        calls.append((tool, args))
        if tool == "graph_run_start":
            return {"ok": True, "data": {"run_id": "run-live-1", "status": "running"}}
        if tool == "graph_run_receipt":
            receipt = {"schema": "agent-graph-mcp-receipt-v2", "run_id": "run-live-1",
                       "graph_version": "sha256:" + "a" * 64,
                       "persistence_status": "durable_terminal", "error_code": None}
            return {"ok": True, "run_id": "run-live-1", "data": {"receipt": receipt,
                    "receipt_digest": "hmac-sha256:" + "b" * 64,
                    "storage_class": "sqlite_terminal_receipt",
                    "bundle": {"payload": {"run_id": "run-live-1",
                               "graph_version": "sha256:" + "a" * 64,
                               "receipt": receipt, "output": {"answer": "REC [S1]"}}}}}
        if tool == "graph_run_cancel":
            cancelled.set()
            return {"ok": True}
        if tool == "graph_run_wait":
            if hold_until_cancel and not cancelled.is_set():
                # Long-poll slice; the run stays live until cancel lands.
                time.sleep(min(args.get("timeout_ms", 5000), 200) / 1000)
                return {"ok": True, "data": {"status": "running"}}
            if wait_results:
                return wait_results.pop(0)
            return {"ok": True, "data": {"status": "completed", "success": True,
                    "final_state": {"answer": "REC [S1]"},
                    "budget_counters": {"llm_calls": 2}}}
        return {"ok": True}

    monkeypatch.setattr(dt, "_graph_mcp_call", mcp)
    return calls


def test_durable_retry_reattaches_without_second_run_or_delivery(durable_home, monkeypatch):
    calls = _counting_mcp(monkeypatch)
    first = json.loads(_dispatch_graph_background(**_route_kwargs()))
    assert first["status"] == "dispatched" and first["legacy_fallback"] is False
    did = first["delegation_id"]
    _wait_idle()
    evt = _drain_for(did)
    assert evt["status"] == "completed"
    assert evt["graph_run_id"] == "run-live-1"
    assert evt["operation_key"] == first["operation_key"]
    assert evt["session_key"] == "origin-A"  # original origin, not a new caller
    starts = [c for c in calls if c[0] == "graph_run_start"]
    assert len(starts) == 1
    # Same logical operation, same payload: reattach, no second graph run.
    again = json.loads(_dispatch_graph_background(**_route_kwargs()))
    assert again["status"] == "reattached"
    assert again["delegation_id"] == did
    assert again["graph_run_id"] == "run-live-1"
    assert len([c for c in calls if c[0] == "graph_run_start"]) == 1
    row = ad.get_durable_delegation(did)
    assert row["request_digest"] and row["operation_key"] == first["operation_key"]
    assert row["graph_alias"] == "research-v1"
    assert row["backend_generation"] == "sha256:" + "a" * 64
    # Exactly one completion event was ever produced for this operation.
    assert process_registry.completion_queue.empty()


def test_changed_payload_under_same_operation_conflicts(durable_home, monkeypatch):
    calls = _counting_mcp(monkeypatch)
    first = json.loads(_dispatch_graph_background(**_route_kwargs()))
    _wait_idle()
    _drain_for(first["delegation_id"])
    changed = json.loads(_dispatch_graph_background(
        **_route_kwargs(graph_input={"source": "CHANGED"})))
    assert changed["status"] == "rejected"
    assert changed["error_code"] == "idempotency_conflict"
    assert changed["legacy_fallback"] is False
    assert len([c for c in calls if c[0] == "graph_run_start"]) == 1


def test_cancel_via_control_plane_routes_graph_run_cancel(durable_home, monkeypatch):
    from tools.delegate_tool import delegate_task, _active_subagents
    calls = _counting_mcp(monkeypatch, hold_until_cancel=True,
                          wait_results=[{"ok": True, "data": {"status": "cancelled", "success": False}}])
    out = json.loads(_dispatch_graph_background(
        **_route_kwargs(graph_budgets={"max_llm_calls": 2, "max_wall_clock_ms": 30000})))
    assert out["status"] == "dispatched"
    handle_id = out["handle_id"]
    # Wait until the run is live and the control handle is registered.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if any(c[0] == "graph_run_start" for c in calls) and handle_id in _active_subagents:
            break
        time.sleep(0.02)
    assert handle_id in _active_subagents
    assert _active_subagents[handle_id]["agent"].operation_key == out["operation_key"]
    stop = json.loads(delegate_task(action="stop", subagent_id=handle_id, parent_agent=parent()))
    assert stop.get("status") == "interrupt_requested", stop
    _wait_idle()
    evt = _drain_for(out["delegation_id"])
    assert evt["status"] == "cancelled"
    cancels = [c for c in calls if c[0] == "graph_run_cancel"]
    assert len(cancels) == 1
    assert cancels[0][1]["run_id"] == "run-live-1"  # same owner-bound run
    assert handle_id not in _active_subagents  # terminal handle cleaned up


def test_deadline_expiry_is_typed_unknown_and_cancels(durable_home, monkeypatch):
    from tools import delegate_tool as dt
    calls = []

    def slow_wait_mcp(parent_agent, tool, args):
        calls.append((tool, args))
        if tool == "graph_run_start":
            return {"ok": True, "data": {"run_id": "run-live-1", "status": "running"}}
        if tool == "graph_run_wait":
            # Real long-poll semantics: consume the requested timeout slice.
            time.sleep(min(args.get("timeout_ms", 5000), 5000) / 1000)
            return {"ok": True, "data": {"status": "running"}}
        return {"ok": True}

    monkeypatch.setattr(dt, "_graph_mcp_call", slow_wait_mcp)
    out = json.loads(_dispatch_graph_background(
        **_route_kwargs(graph_budgets={"max_llm_calls": 2, "max_wall_clock_ms": 250})))
    assert out["status"] == "dispatched"
    _wait_idle()
    evt = _drain_for(out["delegation_id"])
    assert evt["status"] == "unknown"
    assert evt["error"] == "graph_deadline_expired"
    assert any(c[0] == "graph_run_cancel" for c in calls)
    assert evt["graph_run_id"] == "run-live-1"


def test_graph_down_start_failure_is_typed_blocked(durable_home, monkeypatch):
    from tools import delegate_tool as dt
    calls = []

    def down(parent_agent, tool, args):
        calls.append((tool, args))
        return {"ok": False, "error_code": "graph_peer_unqualified"}

    monkeypatch.setattr(dt, "_graph_mcp_call", down)
    out = json.loads(_dispatch_graph_background(**_route_kwargs()))
    assert out["status"] == "dispatched"
    _wait_idle()
    evt = _drain_for(out["delegation_id"])
    assert evt["status"] == "blocked"
    assert evt["error"] == "graph_peer_unqualified"
    assert [c[0] for c in calls] == ["graph_run_start"]  # zero legacy starts


def test_binding_conflict_cancels_run_and_never_delivers(durable_home, monkeypatch):
    calls = _counting_mcp(monkeypatch)
    monkeypatch.setattr("tools.async_delegation.bind_graph_backend",
                        lambda *a, **k: {"ok": False, "code": "graph_backend_binding_conflict"})
    out = json.loads(_dispatch_graph_background(**_route_kwargs()))
    assert out["status"] == "dispatched"
    _wait_idle()
    evt = _drain_for(out["delegation_id"])
    assert evt["status"] == "blocked"
    assert evt["error"] == "graph_backend_binding_conflict"
    assert any(c[0] == "graph_run_cancel" and c[1]["run_id"] == "run-live-1" for c in calls)


def test_restart_after_terminal_before_visible_restores_once(durable_home, monkeypatch):
    calls = _counting_mcp(monkeypatch)
    out = json.loads(_dispatch_graph_background(**_route_kwargs()))
    did = out["delegation_id"]
    _wait_idle()
    row = ad.get_durable_delegation(did)
    assert row["state"] == "completed" and row["delivery_state"] == "pending"
    # Simulate process restart: in-memory records gone, durable row remains.
    ad._reset_for_tests()
    q = queue.Queue()
    assert ad.restore_undelivered_completions(q) == 1
    evt = q.get_nowait()
    assert evt["delegation_id"] == did and evt["restored"] is True
    assert evt["session_key"] == "origin-A"
    assert evt["graph_run_id"] == "run-live-1"
    assert evt["operation_key"] == out["operation_key"]
    claim = "claim-route-1"
    assert ad.claim_completion_delivery(did, claim) is True
    assert ad.claim_completion_delivery(did, "claim-other") is False
    assert ad.complete_completion_delivery(did, claim) is True
    q2 = queue.Queue()
    assert ad.restore_undelivered_completions(q2) == 0
    assert len([c for c in calls if c[0] == "graph_run_start"]) == 1


# ── Entry-point gates through delegate_task itself ─────────────────────────

def _entry(monkeypatch, cfg):
    monkeypatch.setattr("tools.delegate_tool._load_config", lambda: cfg)
    from tools import delegate_tool as dt
    spawned = []
    monkeypatch.setattr(dt, "_dispatch_graph_background",
                        lambda **kw: spawned.append(kw) or json.dumps({"status": "dispatched"}))
    return spawned


def test_entry_disabled_graph_request_is_blocked(monkeypatch):
    from tools.delegate_tool import delegate_task
    spawned = _entry(monkeypatch, {})
    out = json.loads(delegate_task(goal="g", background=True, graph_id="research-v1",
                                   parent_agent=parent()))
    assert out["status"] == "blocked" and out["code"] == "graph_route_disabled"
    assert out["legacy_fallback"] is False and spawned == []


def test_entry_nonallowlisted_graph_is_blocked(monkeypatch):
    from tools.delegate_tool import delegate_task
    spawned = _entry(monkeypatch, {"graph_route_enabled": True,
                                  "graph_route_allowlist": ["research-v1"]})
    out = json.loads(delegate_task(goal="g", background=True, graph_id="other-graph",
                                   parent_agent=parent()))
    assert out["status"] == "blocked" and out["code"] == "graph_id_not_allowlisted"
    assert spawned == []


def test_entry_single_task_goal_and_context_reach_graph_dispatch(monkeypatch):
    from tools.delegate_tool import delegate_task
    spawned = _entry(monkeypatch, {"graph_route_enabled": True,
                                  "graph_route_allowlist": ["research-v1"]})
    out = json.loads(delegate_task(
        background=True, graph_id="research-v1", graph_version="sha256:" + "a" * 64,
        graph_input={"source": "frozen"},
        graph_budgets={"max_llm_calls": 2, "max_wall_clock_ms": 30000},
        graph_operation_id="research-operation-entry-1",
        tasks=[{"goal": "task-goal", "context": "task-context"}],
        parent_agent=parent()))
    assert out["status"] == "dispatched" and len(spawned) == 1
    assert spawned[0]["goal"] == "task-goal"
    assert spawned[0]["context"] == "task-context"


def test_entry_multi_task_and_schema_and_override_are_blocked(monkeypatch):
    from tools.delegate_tool import delegate_task
    cfg = {"graph_route_enabled": True, "graph_route_allowlist": ["research-v1"]}
    spawned = _entry(monkeypatch, cfg)
    common = dict(background=True, graph_id="research-v1", parent_agent=parent())
    multi = json.loads(delegate_task(tasks=[{"goal": "a"}, {"goal": "b"}], **common))
    assert multi["code"] == "graph_route_one_task_only"
    schema = json.loads(delegate_task(goal="g", output_schema={"type": "object"}, **common))
    assert schema["code"] == "graph_output_schema_unqualified"
    task_schema = json.loads(delegate_task(
        tasks=[{"goal": "a", "output_schema": {"type": "object"}}], **common))
    assert task_schema["code"] == "graph_output_schema_unqualified"
    assert spawned == []


def test_entry_auth_denial_precedes_graph_dispatch(monkeypatch):
    from tools.delegate_tool import delegate_task
    from agent.orchestration_policy import OrchestrationError
    spawned = _entry(monkeypatch, {"graph_route_enabled": True,
                                  "graph_route_allowlist": ["research-v1"]})

    def deny(*a, **k):
        raise OrchestrationError("graph_disabled")

    monkeypatch.setattr("agent.orchestration_runtime.admit_worker", deny)
    out = json.loads(delegate_task(goal="g", background=True, graph_id="research-v1",
                                   parent_agent=parent()))
    assert out["status"] == "blocked" and out["code"] == "graph_disabled"
    assert out["provider_calls"] == 0 and spawned == []


# ── Phase 4: workload-fit routing (additive, default-off) ──────────────────
from tools.delegate_tool import resolve_workload_shape

_FANOUT_VERSION = "sha256:" + "b" * 64
_FIT_CFG = {
    "graph_route_enabled": True,
    "graph_route_allowlist": ["fanout4-v1"],
    "agent_graph": {"enabled": True, "routing": {"mode": "workload_fit", "rules": [
        {"id": "wide-fanout", "workload_shape": "wide_independent_fanout",
         "graph_id": "fanout4-v1", "graph_version": _FANOUT_VERSION,
         "graph_budgets": {"max_llm_calls": 4, "max_wall_clock_ms": 120000},
         "enabled": True},
    ]}},
}


def test_shape_absent_is_noop():
    assert resolve_workload_shape(workload_shape=None, graph_id=None, cfg=_FIT_CFG) == {"resolved": False}


def test_shape_resolves_qualified_rule():
    r = resolve_workload_shape(workload_shape="wide_independent_fanout", graph_id=None, cfg=_FIT_CFG)
    assert r["resolved"] is True
    assert r["graph_id"] == "fanout4-v1"
    assert r["graph_version"] == _FANOUT_VERSION
    assert r["graph_budgets"]["max_llm_calls"] == 4
    assert r["rule_id"] == "wide-fanout"


def test_unmatched_shape_falls_through_to_default():
    r = resolve_workload_shape(workload_shape="unknown_shape", graph_id=None, cfg=_FIT_CFG)
    assert r == {"resolved": False}


def test_shape_with_graph_id_is_blocked():
    r = resolve_workload_shape(workload_shape="wide_independent_fanout", graph_id="fanout4-v1", cfg=_FIT_CFG)
    assert r.get("blocked") == "workload_shape_conflicts_with_graph_id"


def test_shape_routing_mode_off_is_noop():
    cfg = {**_FIT_CFG, "agent_graph": {"enabled": True, "routing": {"mode": "off", "rules": _FIT_CFG["agent_graph"]["routing"]["rules"]}}}
    assert resolve_workload_shape(workload_shape="wide_independent_fanout", graph_id=None, cfg=cfg) == {"resolved": False}


def test_disabled_rule_is_not_used():
    import copy
    cfg = copy.deepcopy(_FIT_CFG)
    cfg["agent_graph"]["routing"]["rules"][0]["enabled"] = False
    assert resolve_workload_shape(workload_shape="wide_independent_fanout", graph_id=None, cfg=cfg) == {"resolved": False}


def test_unpinned_or_budgetless_rule_is_blocked():
    import copy
    for mutate in (lambda r: r.update(graph_version="not-a-pin"),
                   lambda r: r.update(graph_budgets={}),
                   lambda r: r.update(graph_budgets={"max_llm_calls": 0, "max_wall_clock_ms": 1000})):
        cfg = copy.deepcopy(_FIT_CFG)
        mutate(cfg["agent_graph"]["routing"]["rules"][0])
        r = resolve_workload_shape(workload_shape="wide_independent_fanout", graph_id=None, cfg=cfg)
        assert r.get("blocked") == "workload_fit_rule_unqualified", r


def test_ambiguous_rules_are_blocked():
    import copy
    cfg = copy.deepcopy(_FIT_CFG)
    cfg["agent_graph"]["routing"]["rules"].append(dict(cfg["agent_graph"]["routing"]["rules"][0]))
    r = resolve_workload_shape(workload_shape="wide_independent_fanout", graph_id=None, cfg=cfg)
    assert r.get("blocked") == "workload_fit_rule_ambiguous"


def test_invalid_shape_label_is_blocked():
    for bad in ("", "   ", 3, []):
        r = resolve_workload_shape(workload_shape=bad, graph_id=None, cfg=_FIT_CFG)
        assert r.get("blocked") == "workload_shape_invalid", r


def test_entry_shape_routes_through_graph_seam(monkeypatch):
    from tools.delegate_tool import delegate_task
    spawned = _entry(monkeypatch, _FIT_CFG)
    out = json.loads(delegate_task(goal="extract four spans", background=True,
                                   workload_shape="wide_independent_fanout",
                                   parent_agent=parent()))
    assert out["status"] == "dispatched", out
    assert len(spawned) == 1
    assert spawned[0]["graph_id"] == "fanout4-v1"
    assert spawned[0]["graph_version"] == _FANOUT_VERSION
    assert spawned[0]["graph_budgets"]["max_llm_calls"] == 4
    assert spawned[0]["graph_operation_id"].startswith("shape-")
    assert spawned[0]["graph_input"] == {"goal": "extract four spans", "context": None}
    # Same owner + same shape + same payload -> stable retry identity.
    retry = json.loads(delegate_task(goal="extract four spans", background=True,
                                     workload_shape="wide_independent_fanout",
                                     parent_agent=parent()))
    assert retry["status"] == "dispatched"
    assert spawned[1]["graph_operation_id"] == spawned[0]["graph_operation_id"]


def _shape_policy_parent(tmp_path, *, allow_delegation=False, allow_graph=True, opt_out=False):
    from agent.orchestration_policy import OrchestrationPolicy
    from hermes_state import SessionDB

    home = tmp_path / "shape-policy"
    home.mkdir()
    db = SessionDB(home / "state.db")
    db.create_session("shape-policy-owner", "cli", model="main-model")
    db.set_orchestration_policy(
        "shape-policy-owner",
        OrchestrationPolicy(
            allow_delegation=allow_delegation,
            allow_graph=allow_graph,
        ).to_dict(),
        expected_revision=0,
        origin="operator-command",
    )
    owner = SimpleNamespace(
        session_id="shape-policy-owner",
        _session_db=db,
        _delegate_depth=0,
        _orchestration_opt_out=opt_out,
    )
    return owner, db


def test_shape_resolved_direct_dispatch_uses_graph_admission_not_delegation(tmp_path, monkeypatch):
    from tools import delegate_tool as dt

    owner, db = _shape_policy_parent(tmp_path, allow_delegation=False, allow_graph=True)
    spawned = []
    monkeypatch.setattr(dt, "_load_config", lambda: _FIT_CFG)
    monkeypatch.setattr(dt, "_dispatch_graph_background",
                        lambda **kw: spawned.append(kw) or json.dumps({"status": "dispatched"}))
    try:
        out = json.loads(dt.delegate_task(
            goal="extract four spans", background=True,
            workload_shape="wide_independent_fanout", parent_agent=owner))
        assert out["status"] == "dispatched", out
        assert len(spawned) == 1
        assert spawned[0]["graph_id"] == "fanout4-v1"
    finally:
        db.close()


def test_shape_resolved_model_dispatch_uses_graph_admission_not_delegation(tmp_path, monkeypatch):
    from run_agent import AIAgent
    from tools import delegate_tool as dt

    owner, db = _shape_policy_parent(tmp_path, allow_delegation=False, allow_graph=True)
    spawned = []
    monkeypatch.setattr(dt, "_load_config", lambda: _FIT_CFG)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: _FIT_CFG)
    monkeypatch.setattr(dt, "_dispatch_graph_background",
                        lambda **kw: spawned.append(kw) or json.dumps({"status": "dispatched"}))
    try:
        out = json.loads(AIAgent._dispatch_delegate_task(owner, {
            "goal": "extract four spans",
            "workload_shape": "wide_independent_fanout",
        }))
        assert out["status"] == "dispatched", out
        assert len(spawned) == 1
        assert spawned[0]["graph_id"] == "fanout4-v1"
    finally:
        db.close()


@pytest.mark.parametrize(
    ("allow_graph", "opt_out", "expected_code"),
    [(False, False, "graph_disabled"), (True, True, "caller_opt_out")],
)
def test_shape_route_never_overrides_graph_policy_or_optout(
    tmp_path, monkeypatch, allow_graph, opt_out, expected_code,
):
    from tools import delegate_tool as dt

    owner, db = _shape_policy_parent(
        tmp_path, allow_delegation=False, allow_graph=allow_graph, opt_out=opt_out)
    spawned = []
    monkeypatch.setattr(dt, "_load_config", lambda: _FIT_CFG)
    monkeypatch.setattr(dt, "_dispatch_graph_background",
                        lambda **kw: spawned.append(kw) or json.dumps({"status": "dispatched"}))
    try:
        out = json.loads(dt.delegate_task(
            goal="extract four spans", background=True,
            workload_shape="wide_independent_fanout", parent_agent=owner))
        assert out["status"] == "blocked" and out["code"] == expected_code, out
        assert spawned == []
    finally:
        db.close()


def test_entry_shape_without_rule_stays_legacy(monkeypatch):
    from tools.delegate_tool import delegate_task
    spawned = _entry(monkeypatch, _FIT_CFG)
    # No matching rule -> normal legacy path; _dispatch_graph_background never called.
    legacy_calls = []
    monkeypatch.setattr("tools.async_delegation.dispatch_async_delegation_batch",
                        lambda **kw: legacy_calls.append(kw) or {"status": "dispatched", "delegation_id": "legacy-x"})
    # Legacy child construction needs a fuller parent double + iteration budget.
    from agent.iteration_budget import IterationBudget
    p = parent()
    p.model = "gpt-6-luna"; p.provider = "openai-codex"
    p.enabled_toolsets = ["file"]; p.reasoning_config = {"enabled": True, "effort": "high"}
    p.iteration_budget = IterationBudget(100)
    monkeypatch.setattr("tools.approval.get_current_session_key", lambda default="": "origin-A")
    monkeypatch.setattr("tools.delegate_tool._build_child_preserving_parent_tools",
                        lambda **kw: SimpleNamespace(_delegate_role="leaf", _subagent_id="s-x"))
    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials",
                        lambda *a, **k: {"model": "gpt-6-luna", "provider": "openai-codex",
                                          "base_url": None, "api_key": None, "api_mode": None,
                                          "command": None, "args": None})
    out = json.loads(delegate_task(goal="plain task", background=True,
                                   workload_shape="unqualified_shape",
                                   parent_agent=p))
    assert out.get("status") == "dispatched", out
    assert spawned == [] and len(legacy_calls) == 1


def test_entry_shape_blocked_when_graph_disabled(monkeypatch):
    from tools.delegate_tool import delegate_task
    cfg = {**_FIT_CFG, "graph_route_enabled": False}
    spawned = _entry(monkeypatch, cfg)
    out = json.loads(delegate_task(goal="g", background=True,
                                   workload_shape="wide_independent_fanout",
                                   parent_agent=parent()))
    assert out["status"] == "blocked" and out["code"] == "graph_route_disabled"
    assert out["legacy_fallback"] is False and spawned == []


def test_entry_shape_non_allowlisted_graph_is_blocked(monkeypatch):
    from tools.delegate_tool import delegate_task
    cfg = {**_FIT_CFG, "graph_route_allowlist": ["research-v1"]}
    spawned = _entry(monkeypatch, cfg)
    out = json.loads(delegate_task(goal="g", background=True,
                                   workload_shape="wide_independent_fanout",
                                   parent_agent=parent()))
    assert out["status"] == "blocked" and out["code"] == "graph_id_not_allowlisted"
    assert spawned == []
