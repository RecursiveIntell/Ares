"""AUTH card: Ares MCP admission issues a real opaque owner reference and the
peer-credentialed service grants one launch + per-provider claims. No model
supplied reference, route, or endpoint is accepted.
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from agent.orchestration_graph import guard_graph_mcp, reset_graph_admission_for_tests
from agent.orchestration_policy import OrchestrationPolicy, SecondaryRoute
from agent.subagent_lifecycle import bind_subagent_parent
from hermes_state import SessionDB
from agent.graph_admission import call_graph_admission, SCHEMA

VERSION = "sha256:" + "a" * 64
ARGS = {
    "graph_id": "auth-fixture",
    "graph_version": VERSION,
    "input": {"q": "fixture"},
    "budgets": {"max_llm_calls": 2, "max_wall_clock_ms": 60000},
    "checkpoint": False,
    "idempotency_key": "auth-op-1",
}


@pytest.fixture
def parent(tmp_path, monkeypatch):
    with tempfile.TemporaryDirectory(prefix="auth-", dir="/tmp") as d:
        endpoint = Path(d) / "authority.sock"
        monkeypatch.setenv("AGENT_GRAPH_ADMISSION_SOCKET", str(endpoint))
        home = tmp_path / "profile"
        home.mkdir()
        (home / "config.yaml").write_text("delegation:\n  provider: ''\n  model: ''\n")
        db = SessionDB(home / "state.db")
        db.create_session("auth-root", "cli", model="main-unchanged")
        db.set_orchestration_policy(
            "auth-root",
            OrchestrationPolicy(secondary_route=SecondaryRoute(
                "openai-codex", "gpt-6-luna", "low")).to_dict(),
            expected_revision=0,
            origin="operator-command",
        )
        p = SimpleNamespace(_session_db=db, session_id="auth-root", _delegate_depth=0)
        try:
            yield p, endpoint
        finally:
            db.close()
            reset_graph_admission_for_tests()


def _launch_request(grant, run_id="run-auth-1"):
    return {
        "schema": SCHEMA,
        "reference": grant["reference"],
        "stage": "launch",
        "graph_id": grant["graph_id"],
        "graph_version": grant["graph_version"],
        "request_digest": grant["request_digest"],
        "execution_route": grant["execution_route"],
        "run_id": run_id,
    }


def _provider_request(grant, run_id="run-auth-1", node_id="rec", invocation_ref="rec:1"):
    return {
        "schema": SCHEMA,
        "reference": grant["reference"],
        "stage": "provider",
        "graph_id": grant["graph_id"],
        "graph_version": grant["graph_version"],
        "request_digest": grant["request_digest"],
        "execution_route": grant["execution_route"],
        "run_id": run_id,
        "node_id": node_id,
        "invocation_ref": invocation_ref,
        "requested_effort": grant["execution_route"]["reasoning_effort"],
    }


def test_guard_overwrites_model_grant_and_real_owner_accepts_launch_and_provider(parent):
    p, endpoint = parent
    with bind_subagent_parent(p):
        grant = guard_graph_mcp("agent_graph", "graph_run_start", {
            **ARGS,
            "host_grant": {"reference": "fixture-reference", "request_digest": "sha256:fixture"},
        })
    assert grant["reference"] != "fixture-reference"
    assert grant["request_digest"].startswith("sha256:")
    assert grant["graph_version"] == VERSION
    assert grant["execution_route"] == {
        "provider": "openai-codex", "model": "gpt-6-luna", "reasoning_effort": "low"}
    assert call_graph_admission(endpoint, _launch_request(grant))["ok"] is True
    assert call_graph_admission(endpoint, _provider_request(grant))["ok"] is True
    replay = call_graph_admission(endpoint, _provider_request(grant))
    assert replay["ok"] is False and replay["code"] == "graph_admission_replay"


def test_default_admission_socket_is_scoped_to_hermes_home(parent, tmp_path, monkeypatch):
    p, _fixture_endpoint = parent
    with tempfile.TemporaryDirectory(prefix="ph-") as profile_home_raw:
        profile_home = Path(profile_home_raw)
        monkeypatch.delenv("AGENT_GRAPH_ADMISSION_SOCKET", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        expected = profile_home / "run" / "graph-admission" / "authority.sock"

        with bind_subagent_parent(p):
            guard_graph_mcp("agent_graph", "graph_run_start", ARGS)

        assert expected.exists()
        assert expected.is_socket()
        reset_graph_admission_for_tests()


def test_missing_version_or_budget_is_refused_before_transport(parent):
    p, _ = parent
    with bind_subagent_parent(p), pytest.raises(Exception, match="graph_version_pin_required"):
        guard_graph_mcp("agent_graph", "graph_run_start", {
            "graph_id": "auth-fixture", "input": {},
            "budgets": ARGS["budgets"],
        })
    with bind_subagent_parent(p), pytest.raises(Exception, match="graph_budget_required"):
        guard_graph_mcp("agent_graph", "graph_run_start", {
            "graph_id": "auth-fixture", "graph_version": VERSION, "input": {},
        })


def test_wrong_owner_revision_and_forged_reference_denied(parent):
    p, endpoint = parent
    with bind_subagent_parent(p):
        grant = guard_graph_mcp("agent_graph", "graph_run_start", ARGS)
    assert call_graph_admission(endpoint, _launch_request(grant))["ok"] is True
    # Operator revokes graph between launch and provider call.
    p._session_db.set_orchestration_policy(
        "auth-root", OrchestrationPolicy(allow_graph=False).to_dict(),
        expected_revision=1, origin="operator-command")
    denied = call_graph_admission(endpoint, _provider_request(grant, invocation_ref="rec:2"))
    assert denied["ok"] is False
    assert denied["code"] == "orchestration_revision_changed"
    forged = _provider_request(grant, invocation_ref="rec:3")
    forged["reference"] = "fixture-reference"
    assert call_graph_admission(endpoint, forged)["ok"] is False


def test_graph_execute_and_resume_are_typed_unqualified(parent):
    p, _ = parent
    with bind_subagent_parent(p), pytest.raises(Exception, match="graph_operation_not_managed"):
        guard_graph_mcp("agent_graph", "graph_execute", {"graph_id": "auth-fixture"})
    with bind_subagent_parent(p), pytest.raises(Exception, match="graph_operation_not_managed"):
        guard_graph_mcp("agent_graph", "graph_run_resume", {"run_id": "run-old"})


def test_mcp_handler_replaces_caller_grant_with_owner_issued_grant(parent):
    p, endpoint = parent
    sent = {}

    class FakeSession:
        async def call_tool(self, name, *, arguments):
            if name == "graph_status":
                return SimpleNamespace(
                    structured_content={"data": {"capabilities": {
                        "host_admission": {
                            "protocol": "ares.graph-admission/v1",
                            "launch_owner_claim": True,
                            "provider_owner_claim": True,
                        }}}},
                    content=[],
                    is_error=False,
                )
            sent["name"] = name
            sent["arguments"] = arguments
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text='{"result":"ok"}')],
                is_error=False,
                structured_content=None,
                _meta=None,
            )

    server = SimpleNamespace(
        session=FakeSession(),
        _rpc_lock=asyncio.Lock(),
        _config={},
        _pending_call_context=None,
        _stdio_children_dead=None,
        _watch_stdio_children=None,
        _mark_session_proven=None,
    )

    @asynccontextmanager
    async def no_track(*args, **kwargs):
        yield

    from tools.mcp_tool import _make_tool_handler
    args = {
        **ARGS,
        "host_grant": {"reference": "fixture-reference", "request_digest": "sha256:fixture"},
    }
    with bind_subagent_parent(p), \
         patch("tools.mcp_tool._trust_gate_check", return_value=None), \
         patch("tools.mcp_tool._get_connected_server_for_call", return_value=server), \
         patch("tools.mcp_tool._run_on_mcp_loop", side_effect=lambda coro, timeout: asyncio.run(coro() if callable(coro) else coro)), \
         patch("tools.mcp_tool._track_inflight_rpc", no_track), \
         patch("tools.mcp_tool._mark_server_call_started", return_value=None):
        result = _make_tool_handler("agent_graph", "graph_run_start", 10)(args)
    assert json.loads(result)["result"] == '{"result":"ok"}'
    assert sent["name"] == "graph_run_start"
    assert sent["arguments"]["host_grant"]["reference"] != "fixture-reference"
    assert sent["arguments"]["host_grant"]["request_digest"].startswith("sha256:")
    # Model text cannot override the generated authority endpoint or route.
    assert "endpoint" not in sent["arguments"]["host_grant"]
    assert sent["arguments"]["host_grant"]["graph_id"] == ARGS["graph_id"]
    reset_graph_admission_for_tests()


def test_old_daemon_without_host_admission_capability_is_blocked_prelaunch():
    calls = []

    class OldSession:
        async def call_tool(self, name, *, arguments):
            calls.append(name)
            return SimpleNamespace(
                structured_content={"data": {"capabilities": {"runtime": "agent_graph"}}},
                content=[], is_error=False)

    server = SimpleNamespace(session=OldSession(), _rpc_lock=asyncio.Lock(), _config={})
    from tools.mcp_tool import _make_tool_handler
    with patch("agent.orchestration_graph.guard_graph_mcp", return_value={"reference":"opaque"}), \
         patch("tools.mcp_tool._trust_gate_check", return_value=None), \
         patch("tools.mcp_tool._get_connected_server_for_call", return_value=server), \
         patch("tools.mcp_tool._run_on_mcp_loop", side_effect=lambda coro, timeout: asyncio.run(coro() if callable(coro) else coro)), \
         patch("tools.mcp_tool._mark_server_call_started", return_value=None):
        result = json.loads(_make_tool_handler("agent_graph", "graph_run_start", 10)({"graph_id":"g"}))
    assert result["status"] == "blocked"
    assert result["code"] == "graph_peer_unqualified"
    assert calls == ["graph_status"]  # launch was never sent to an old peer
