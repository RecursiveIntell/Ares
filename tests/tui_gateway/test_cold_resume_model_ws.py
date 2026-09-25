"""Cold Desktop resume/model selection through authenticated WebSocket and real host."""

import json
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient


def test_cold_goal_resume_picks_model_before_host_has_child(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_state import SessionDB
    from hermes_cli import goals, web_server
    from tui_gateway import server
    from tui_gateway.host_supervisor import HostSupervisor

    goals._DB_CACHE.clear()
    db = SessionDB(db_path=home / "state.db")
    key = "disposable-cold-goal"
    db.create_session(key, source="desktop")
    db.set_session_title(key, "Cold model selection")
    goal = goals.GoalManager(key).set("Continue bounded work")
    assert goal.status == "active"
    monkeypatch.setattr(server, "_db", db)
    monkeypatch.setattr(server, "_hermes_home", home)
    monkeypatch.setattr(server, "_sessions", {})
    # Hold hydration/auto-continuation so the pick is deterministically BEFORE
    # the first host turn, just as in the reported restart window.
    monkeypatch.setattr(server, "_schedule_resume_hydration", lambda *_a, **_kw: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda *_a: None)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config",
                        lambda *_a: {"turn_isolation": True, "compute_host_heartbeat_secs": 15,
                                     "compute_host_respawn_max": 0})
    monkeypatch.setattr(web_server, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    monkeypatch.setattr(server, "resolve_skin", lambda: {})
    monkeypatch.setattr(server, "_ensure_skin_watcher", lambda: None)
    monkeypatch.setattr(server, "_schedule_startup_orphan_sweep", lambda: None)
    repo = Path(__file__).resolve().parents[2]
    supervisor = HostSupervisor(registry_path=home / "state" / "cold-host.json",
        env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", ""),
             "PYTHONPATH": str(repo), "HERMES_HOME": str(home),
             "HERMES_ISO_CERTIFY_SYNTH_TURN": "1",
             "HERMES_ISO_CERTIFY_DURATION_S": "0.1",
             "HERMES_COMPUTE_HOST_HEARTBEAT_SECS": "0"},
        heartbeat_secs=0, respawn_max=0, autostart=False)
    host_frames = []
    original_handle_frame = supervisor._handle_host_frame
    def capture_host_frame(frame):
        if frame.get("type") in {"turn.started", "turn.end", "turn.error"}:
            host_frames.append(dict(frame))
        return original_handle_frame(frame)
    monkeypatch.setattr(supervisor, "_handle_host_frame", capture_host_frame)
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *_a: supervisor)
    def stage_model(_sid, staging, raw, **_kwargs):
        assert raw == "gpt-6-sol --provider openai-codex --session"
        assert staging.get("agent") is None
        staging["model_override"] = {"model": "gpt-6-sol", "provider": "openai-codex"}
        return {"value": "gpt-6-sol", "scope": "session", "warning": ""}
    monkeypatch.setattr(server, "_apply_model_switch", stage_model)

    def rpc(ws, id_, method, **params):
        ws.send_text(json.dumps({"jsonrpc": "2.0", "id": id_, "method": method, "params": params}))
        for _ in range(40):
            frame = json.loads(ws.receive_text())
            if frame.get("id") == id_:
                return frame
        raise AssertionError(f"missing RPC reply: {method}")

    try:
        with TestClient(web_server.app).websocket_connect(
            f"/api/ws?token={web_server._SESSION_TOKEN}"
        ) as ws:
            assert json.loads(ws.receive_text())["params"]["type"] == "gateway.ready"
            resumed = rpc(ws, "resume", "session.resume", session_id=key,
                          defer_history=True, omit_messages=True, source="desktop")
            assert resumed.get("result"), resumed.get("error")
            sid = resumed["result"]["session_id"]
            session = server._sessions[sid]
            assert not session.get("_compute_host_active")
            assert supervisor.lookup_session_key(key) is None
            picked = rpc(ws, "model", "config.set", session_id=sid, key="model",
                         value="gpt-6-sol --provider openai-codex --session")
            assert picked.get("result", {}).get("value") == "gpt-6-sol", picked
            assert session["model_override"]["model"] == "gpt-6-sol"
            persisted_goal = goals.GoalManager(key).state
            assert persisted_goal is not None
            assert persisted_goal.goal_id == goal.goal_id
            assert persisted_goal.status == "active"
            assert supervisor.lookup_session_key(key) is None
            first_turn = server._compute_host_turn_frame("first", sid, session, "first user turn")
            assert first_turn["model_override"]["model"] == "gpt-6-sol"
            session["resume_hydrating"] = False
            session["resume_history_ready"].set()
            dispatched = rpc(ws, "first-turn", "prompt.submit", session_id=sid,
                             text=json.dumps({"duration_s": 0.1, "chunk": 1000}))
            assert dispatched.get("result", {}).get("status") == "streaming", dispatched
            deadline = time.monotonic() + 8
            while not any(f.get("type") == "turn.end" and f.get("request_id") == "first-turn"
                          for f in host_frames) and time.monotonic() < deadline:
                time.sleep(0.02)
            completed = next((f for f in host_frames if f.get("type") == "turn.end"
                              and f.get("request_id") == "first-turn"), None)
            assert completed is not None, host_frames
            assert completed["session_info"]["model"] == "gpt-6-sol"
            assert not any(f.get("type") == "turn.error" for f in host_frames), host_frames
    finally:
        supervisor.shutdown()
        db.close()
        goals._DB_CACHE.clear()
