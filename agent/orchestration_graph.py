"""Agent Graph host admission, deliberately NOT a daemon security boundary.

The captured daemon has no consumed authenticated session-policy binding. Until
that boundary is qualified, launch/resume is a typed refusal, not a client-only
'permit'. Read/cleanup and inert spec/storage operations retain their own MCP
permissions. Raw RPC/shell access is outside this host adapter's containment.
"""
from agent.orchestration_policy import OrchestrationError
from agent.orchestration_runtime import admit_worker
from agent.subagent_lifecycle import get_active_subagent_parent
import atexit
import hashlib
import json
import math
import os
from pathlib import Path
import threading

_graph_admission_lock = threading.Lock()
_graph_admission_service = None
_graph_admission_socket = None

def _shutdown_graph_admission_socket():
    global _graph_admission_service, _graph_admission_socket
    with _graph_admission_lock:
        server = _graph_admission_socket
        _graph_admission_socket = None
        _graph_admission_service = None
    if server is not None:
        server.__exit__(None, None, None)


def retire_graph_host_grant(grant):
    """Dispose an issued reference if a preflight/trust gate blocks dispatch."""
    if type(grant) is not dict or _graph_admission_service is None:
        return False
    return _graph_admission_service.retire_reference(grant.get("reference"))


def reset_graph_admission_for_tests():
    """Close the process-local authority owner and forget ephemeral scopes."""
    _shutdown_graph_admission_socket()


atexit.register(_shutdown_graph_admission_socket)


def _canonical_digest(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _host_request_material(tool_name, arguments):
    # Both Python and Rust serialize the same closed object. The explicit nulls
    # are part of v1 canonicalization; caller-supplied host_grant is excluded.
    return {
        "operation": tool_name,
        "graph_id": arguments.get("graph_id"),
        "graph_version": arguments.get("graph_version"),
        "input": arguments.get("input"),
        "thread_id": arguments.get("thread_id"),
        "budgets": arguments.get("budgets"),
        "checkpoint": arguments.get("checkpoint", False),
        "idempotency_key": arguments.get("idempotency_key"),
    }


def _issue_graph_host_grant(tool_name, arguments):
    """Issue a short-lived owner reference and start the local authority socket.

    Managed launch is intentionally narrow: pinned graph version, explicit
    positive deadline+invocation budget, and no caller-provided authority.
    """
    global _graph_admission_service, _graph_admission_socket
    parent = get_active_subagent_parent()
    if parent is None:
        raise OrchestrationError("policy_owner_required")
    admission = admit_worker(parent, effect="graph")
    if tool_name != "graph_run_start":
        # Sync execute and resume have no qualified managed adapter yet.
        raise OrchestrationError("graph_operation_not_managed")
    graph_id = arguments.get("graph_id")
    graph_version = arguments.get("graph_version")
    if not isinstance(graph_id, str) or not isinstance(graph_version, str):
        raise OrchestrationError("graph_version_pin_required")
    budgets = arguments.get("budgets")
    if not isinstance(budgets, dict):
        raise OrchestrationError("graph_budget_required")
    max_calls = budgets.get("max_llm_calls")
    max_wall_ms = budgets.get("max_wall_clock_ms")
    if type(max_calls) is not int or max_calls <= 0 or type(max_wall_ms) is not int or max_wall_ms <= 0:
        raise OrchestrationError("graph_budget_required")
    request_digest = _canonical_digest(_host_request_material(tool_name, arguments))
    with _graph_admission_lock:
        if _graph_admission_service is None:
            from agent.graph_admission import GraphAdmissionService, GraphAdmissionSocket
            from hermes_constants import get_hermes_home
            service = GraphAdmissionService()
            # Profile-aware default: two profile backends under one OS account
            # must not collide on a single authority endpoint. The explicit
            # AGENT_GRAPH_ADMISSION_SOCKET override still wins; the default
            # profile's path is unchanged.
            endpoint = Path(os.getenv(
                "AGENT_GRAPH_ADMISSION_SOCKET",
                str(get_hermes_home() / "run" / "graph-admission" / "authority.sock"),
            ))
            if endpoint.name != "authority.sock":
                raise OrchestrationError("graph_admission_endpoint_unqualified")
            directory = endpoint.parent
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            server = GraphAdmissionSocket(service, directory)
            server.__enter__()
            _graph_admission_service = service
            _graph_admission_socket = server
        scope = _graph_admission_service.issue(
            parent, graph_id=graph_id, graph_version=graph_version,
            request_digest=request_digest,
            ttl_seconds=min(3600, max(30, math.ceil(max_wall_ms / 1000))),
            deadline_seconds=max_wall_ms / 1000,
            budget_invocations=max_calls,
        )
    return {
        "schema": "ares.graph-admission/v1",
        "reference": scope.reference,
        "graph_id": scope.graph_id,
        "graph_version": scope.graph_version,
        "request_digest": scope.request_digest,
        "execution_route": scope.execution_route,
        "deadline_seconds": scope.deadline_seconds,
        "budget_invocations": scope.budget_invocations,
        "route_generation": scope.route_generation,
    }

GRAPH_OPERATIONS = {
    'graph_approval_get': 'read', 'graph_approval_list': 'read',
    'graph_approval_request': 'approval_request', 'graph_create': 'spec',
    'graph_execute': 'launch', 'graph_inspect': 'read', 'graph_list': 'read',
    'graph_policy_check': 'preflight', 'graph_render': 'read',
    'graph_retention_review': 'read', 'graph_retention_set': 'retention',
    'graph_run_artifact': 'read', 'graph_run_cancel': 'cancel',
    'graph_run_checkpoint': 'read', 'graph_run_events': 'read',
    'graph_run_get': 'read', 'graph_run_receipt': 'read',
    'graph_run_resume': 'launch', 'graph_run_start': 'launch',
    'graph_run_state': 'read', 'graph_run_triage': 'read', 'graph_run_wait': 'read',
    'graph_source_witness_capture': 'witness', 'graph_source_witness_get': 'read',
    'graph_status': 'read', 'graph_template_candidates': 'read',
    'graph_template_instantiate': 'spec', 'graph_template_list': 'read',
    'graph_template_outcomes': 'read',
}


def guard_graph_mcp(server_name, tool_name, arguments):
    """Before MCP trust/connection/queue. No payload session ID is trusted."""
    if server_name != 'agent_graph':
        return
    category = GRAPH_OPERATIONS.get(tool_name)
    if category is None:
        raise OrchestrationError('graph_operation_unqualified')
    if category == 'launch':
        return _issue_graph_host_grant(tool_name, arguments)
    return None
