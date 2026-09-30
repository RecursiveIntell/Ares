"""A host Stop receipt must match the canonical DB scope and control value."""
import contextlib
import io
import json
import threading
import types

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tui_gateway.host_supervisor import HostSendUncertain
from tests.tui_gateway.test_parent_stop_settlement import state, setup
from tests.ares_runtime.test_continuity_dispatch import durable_agent  # noqa: F401
from tests.run_agent.test_run_agent import agent  # noqa: F401


@pytest.mark.parametrize("phase,required,expected", [
    (None, False, False), ("answered", False, False), ("answered", True, True),
    ("cancelled", False, False), ("cancelled", True, True),
    ("active", False, True), ("idle", False, True), ("uncertain", False, True),
])
def test_stop_requirement_distinguishes_history_from_live_work(monkeypatch, phase, required, expected):
    owner = types.SimpleNamespace(
        get_session=lambda key: {"id": key},
        read_context_input_work=lambda key: {"receipts": (), "phase": None if phase is None else {"state": phase}},
        context_dispatch_required_for_session=lambda key: required,
    )
    monkeypatch.setattr(server, "_session_db", lambda scope: contextlib.nullcontext(owner))
    assert server._native_stop_requires_receipt({"session_key": "stored"}) is expected


def test_readback_rejects_wrong_scope_and_changed_value(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("stored", source="desktop")
        db.create_session("other", source="desktop")
        control = db.record_context_stop("stored")
        foreign = db.record_context_stop("other")
        monkeypatch.setattr(server, "_session_db", lambda session: contextlib.nullcontext(db))
        valid = {"session_key": "stored", "control": control}
        assert server._verify_host_stop_receipt(state(monkeypatch), valid) == db.read_context_stop("stored")
        with pytest.raises(RuntimeError, match="scope or value"):
            server._verify_host_stop_receipt(state(monkeypatch), {"session_key": "other", "control": foreign})
        with pytest.raises(RuntimeError, match="scope or value"):
            server._verify_host_stop_receipt(state(monkeypatch), {"session_key": "stored", "control": dict(control, revision=control["revision"] + 1)})
    finally:
        db.close()


def test_real_agent_stop_receipt_crosses_host_ack_and_db_readback(durable_agent, monkeypatch):
    agent, db = durable_agent
    session = {"agent": agent, "history_lock": threading.Lock(), "session_key": agent.session_id,
               "running": True, "_run_thread": types.SimpleNamespace(is_alive=lambda: True)}
    monkeypatch.setattr(server, "_sessions", {"s": session})
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: False)
    monkeypatch.setattr(server, "_clear_pending", lambda *a: None)
    monkeypatch.setattr(server, "_session_db", lambda session: contextlib.nullcontext(db))
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    host._active_request_ids["s"] = "A"
    try:
        host._handle_interrupt({"sid": "s", "request_id": "stop", "target_request_id": "A"})
        ack = json.loads(output.getvalue().strip())
        assert ack["applied"] is True
        assert ack["stop_receipt"] == agent._context_stop_receipt
        assert server._verify_host_stop_receipt(session, ack["stop_receipt"]) == db.read_context_stop(agent.session_id)
    finally:
        host.close()


def test_host_does_not_relabel_old_cached_receipt_as_new(monkeypatch):
    cached = {"session_key": "stored", "control": {"schema": "SessionDBContextControlV2"}}
    agent = types.SimpleNamespace(_context_stop_receipt=cached)
    session = {"agent": agent, "running": True}
    monkeypatch.setattr(server, "_sessions", {"s": session})
    monkeypatch.setattr(server, "_interrupt_session_turn", lambda *a, **kw: None)
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    host._active_request_ids["s"] = "A"
    try:
        host._handle_interrupt({"sid": "s", "request_id": "stop", "target_request_id": "A"})
        assert "stop_receipt" not in json.loads(output.getvalue().strip())
    finally:
        host.close()


def test_terminal_does_not_promote_failed_stop_persistence(monkeypatch):
    session = state(monkeypatch)
    def interrupt(sid, **kwargs):
        server._on_compute_host_turn_done("rpc", "s", session,
            {"type": "turn.end", "sid": "s", "request_id": "A", "interrupted": True})
        return {"type": "interrupt.ack", "sid": sid, "request_id": kwargs["request_id"],
                "target_request_id": "A", "applied": True, "durable_stop_unconfirmed": True}
    setup(monkeypatch, types.SimpleNamespace(interrupt=interrupt))
    with pytest.raises(HostSendUncertain, match="persistence"):
        server._interrupt_session_turn("s", session, request_id="stop")
    assert session["_stop_uncertain"]["durable_stop_unconfirmed"] is True
    with session["history_lock"]:
        server._enqueue_prompt(session, "C", None)
    assert server._drain_queued_prompt("next", "s", session) is False
    assert session["queued_prompt"]["text"] == "C"
