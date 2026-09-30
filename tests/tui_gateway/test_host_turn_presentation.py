"""Reply completion does not retire the accepted host turn's Stop control."""
import io
import threading
import types

from tui_gateway import server
from tui_gateway.compute_host import ComputeHost


def test_host_owned_bubble_marks_pending_without_mutating_payload(monkeypatch):
    session = {"_host_turn_request_id": "A"}
    monkeypatch.setattr(server, "_sessions", {"s": session})
    monkeypatch.setattr(server, "_inside_compute_host_child", lambda: True)
    sent = []
    monkeypatch.setattr(server, "write_json", sent.append)
    payload = {"text": "one goal step finished"}
    server._emit("message.complete", "s", payload)
    assert sent[0]["params"]["payload"]["chain_pending"] is True
    assert "chain_pending" not in payload
    session.pop("_host_turn_request_id")
    server._emit("message.complete", "s", payload)
    assert "chain_pending" not in sent[1]["params"]["payload"]


def test_session_info_stays_busy_during_host_handoff(tmp_path, monkeypatch):
    import hermes_cli.banner as banner
    monkeypatch.setattr(banner, "get_update_result", lambda **kw: None)
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_get_db", lambda: None)
    session = {"running": False, "_host_turn_request_id": "A", "cwd": str(tmp_path)}
    agent = types.SimpleNamespace(model="test-model", provider="test-provider")
    assert server._session_info(agent, session)["running"] is True
    session.pop("_host_turn_request_id")
    assert server._session_info(agent, session)["running"] is False


def test_only_matching_host_terminal_releases_presentation_owner(monkeypatch):
    session = {"history_lock": threading.Lock(), "_host_turn_request_id": "A"}
    monkeypatch.setattr(server, "_sessions", {"s": session})
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    host._active_request_ids["s"] = "A"
    try:
        host._emit_real_turn_terminal({"type": "turn.end", "sid": "s", "request_id": "old"})
        assert session["_host_turn_request_id"] == "A"
        host._emit_real_turn_terminal({"type": "turn.end", "sid": "s", "request_id": "A"})
        assert "_host_turn_request_id" not in session
    finally:
        host.close()
