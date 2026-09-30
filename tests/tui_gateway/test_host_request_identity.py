"""Host execution identity must not inherit connection-local RPC identity."""
import json
import os
import threading

from tests.tui_gateway.terminal_settlement_helpers import wait_for_terminal_projection
import time
from pathlib import Path

from fastapi.testclient import TestClient

import pytest

from tui_gateway import server
from tui_gateway.host_supervisor import HostSupervisor


def test_reused_rpc_id_gets_distinct_host_identity(monkeypatch):
    frames = []
    callbacks = []
    completed = []

    class Supervisor:
        def submit_turn(self, frame, *, on_complete):
            frames.append(frame)
            callbacks.append(on_complete)

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda _: Supervisor())
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_on_compute_host_turn_done", lambda rid, sid, session, frame: completed.append((rid, sid)))
    for sid in ("first", "second", "first"):
        session = {"history_lock": threading.Lock(), "source": "desktop"}
        reply = server._submit_prompt_to_compute_host(
            "rpc-1", sid, session, "hello", context_input_event_id="canonical-input"
        )
        assert reply["id"] == "rpc-1"
    ids = [frame["request_id"] for frame in frames]
    assert len(set(ids)) == 3
    assert "rpc-1" not in ids
    assert all(frame["context_input_event_id"] == "canonical-input" for frame in frames)
    for callback, frame in zip(callbacks, frames):
        callback({"type": "turn.end", "sid": frame["sid"], "request_id": frame["request_id"]})
    assert completed == [("rpc-1", "first"), ("rpc-1", "second"), ("rpc-1", "first")]


def supervisor_fixture(tmp_path, monkeypatch):
    supervisor = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    frames = []
    monkeypatch.setattr(supervisor, "start", lambda: None)
    monkeypatch.setattr(supervisor, "_send_frame", lambda frame, **_kwargs: frames.append(frame))
    return supervisor, frames


def test_duplicate_host_identity_is_refused_before_write(tmp_path, monkeypatch):
    supervisor, frames = supervisor_fixture(tmp_path, monkeypatch)
    completed = []
    supervisor.submit_turn({"sid": "first", "request_id": "owner"}, on_complete=completed.append)
    with pytest.raises(ValueError, match="duplicate.*request"):
        supervisor.submit_turn({"sid": "second", "request_id": "owner"})
    assert len(frames) == 1
    terminal = {"type": "turn.end", "sid": "first", "request_id": "owner"}
    supervisor._complete_turn(terminal)
    wait_for_terminal_projection(supervisor)
    assert completed == [terminal]


def test_wrong_sid_terminal_does_not_consume_request(tmp_path, monkeypatch):
    supervisor, _ = supervisor_fixture(tmp_path, monkeypatch)
    completed = []
    supervisor.submit_turn({"sid": "first", "request_id": "owner"}, on_complete=completed.append)
    supervisor._complete_turn({"type": "turn.end", "sid": "second", "request_id": "owner"})
    wait_for_terminal_projection(supervisor)
    supervisor._complete_turn({"type": "turn.end", "sid": "first", "request_id": "wrong"})
    wait_for_terminal_projection(supervisor)
    assert completed == []
    terminal = {"type": "turn.end", "sid": "first", "request_id": "owner"}
    supervisor._complete_turn(terminal)
    wait_for_terminal_projection(supervisor)
    supervisor._complete_turn(terminal)
    wait_for_terminal_projection(supervisor)
    assert completed == [terminal]


def test_two_authenticated_sockets_reusing_rpc_id_settle_real_host(tmp_path, monkeypatch):
    from hermes_cli import web_server
    from hermes_state import SessionDB

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = SessionDB(db_path=home / "state.db")
    for key in ("identity-first", "identity-second"):
        db.create_session(key, source="desktop")
    monkeypatch.setattr(server, "_db", db)
    monkeypatch.setattr(server, "_hermes_home", home)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_schedule_resume_hydration", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda *a: None)
    monkeypatch.setattr(server, "_schedule_startup_orphan_sweep", lambda: None)
    monkeypatch.setattr(server, "_ensure_skin_watcher", lambda: None)
    monkeypatch.setattr(server, "resolve_skin", lambda: {})
    monkeypatch.setattr(web_server, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config",
                        lambda *a: {"turn_isolation": True, "compute_host_respawn_max": 0})
    host = HostSupervisor(
        registry_path=home / "host.json", autostart=False, heartbeat_secs=0, respawn_max=0,
        env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", ""),
             "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
             "HERMES_HOME": str(home), "HERMES_ISO_CERTIFY_SYNTH_TURN": "1",
             "HERMES_COMPUTE_HOST_HEARTBEAT_SECS": "0"},
    )
    frames = []
    original = host._handle_host_frame

    def capture(frame):
        frames.append(dict(frame))
        original(frame)

    monkeypatch.setattr(host, "_handle_host_frame", capture)
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: host)

    def rpc(ws, rid, method, **params):
        ws.send_text(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
        for _ in range(100):
            frame = json.loads(ws.receive_text())
            if frame.get("id") == rid:
                assert "error" not in frame, frame
                return frame
        raise AssertionError("missing RPC response")

    try:
        with TestClient(web_server.app) as client:
            url = f"/api/ws?token={web_server._SESSION_TOKEN}"
            with client.websocket_connect(url) as first, client.websocket_connect(url) as second:
                sids = []
                for ws, key in ((first, "identity-first"), (second, "identity-second")):
                    resumed = rpc(ws, "resume", "session.resume", session_id=key,
                                  defer_history=True, omit_messages=True, source="desktop")
                    sid = resumed["result"]["session_id"]
                    sids.append(sid)
                    session = server._sessions[sid]
                    session["resume_hydrating"] = False
                    session["resume_history_ready"].set()
                for ws, sid in zip((first, second), sids):
                    reply = rpc(ws, "same-client-id", "prompt.submit", session_id=sid,
                                text=json.dumps({"duration_s": 0.3, "chunk": 1000}))
                    assert reply["id"] == "same-client-id"
                    assert reply["result"]["status"] == "streaming"
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and any(server._sessions[sid]["running"] for sid in sids):
                    time.sleep(0.02)
                assert all(not server._sessions[sid]["running"] for sid in sids)
                terminals = [f for f in frames if f.get("type") in {"turn.end", "turn.error"}]
                assert len(terminals) == 2, terminals
                assert all(f["type"] == "turn.end" for f in terminals), terminals
                assert {f["sid"] for f in terminals} == set(sids)
                assert len({f["request_id"] for f in terminals}) == 2
                assert all(f["request_id"] != "same-client-id" for f in terminals)
    finally:
        host.shutdown()
        db.close()
