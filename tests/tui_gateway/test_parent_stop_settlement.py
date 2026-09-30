"""Stop ACK settlement preserves post-cut input and never claims a refused Stop."""
import threading
import types

import pytest

from tui_gateway import server
from tui_gateway.host_supervisor import HostSendNotSent, HostSendUncertain


def state(monkeypatch):
    session = {"history_lock": threading.Lock(), "history": [], "agent": None,
            "session_key": "stored", "running": True, "_compute_host_active_request_id": "A",
            "queued_prompt": {"text": "B", "transport": None}}
    monkeypatch.setitem(server._sessions, "s", session)
    return session


def setup(monkeypatch, host):
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: True)
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: host)
    monkeypatch.setattr(server, "_clear_pending", lambda *a: None)
    monkeypatch.setattr(server, "_clear_inflight_turn", lambda *a: None)
    monkeypatch.setattr(server, "_session_info", lambda *a: {})
    monkeypatch.setattr(server, "_emit", lambda *a: None)


def test_terminal_before_stop_ack_preserves_post_cut_message(monkeypatch):
    session = state(monkeypatch)
    sent = []
    def interrupt(sid, **kwargs):
        assert kwargs["wait"] is True
        assert session.get("_stop_pending")
        assert session.get("queued_prompt") is None
        server._on_compute_host_turn_done("rpc-A", "s", session,
            {"type": "turn.end", "sid": "s", "request_id": "A", "interrupted": True})
        response = server._handle_busy_submit("rpc-C", "s", session, "C", None)
        assert response is not None
        assert response["result"]["status"] == "queued"
        assert server._drain_queued_prompt("rpc-C", "s", session) is False
        assert sent == [], "post-cut message must wait for Stop settlement"
        assert session["queued_prompt"]["text"] == "C"
        return {"type": "interrupt.ack", "sid": sid, "request_id": kwargs["request_id"],
                "target_request_id": "A", "applied": True}
    setup(monkeypatch, types.SimpleNamespace(interrupt=interrupt))
    monkeypatch.setattr(server, "_submit_prompt_to_compute_host",
                        lambda rid, sid, state, text, **kw: sent.append(text) or server._ok(rid, {"status": "streaming"}))
    assert server._interrupt_session_turn("s", session, request_id="stop")
    assert sent == ["C"]
    assert not session.get("_stop_pending")
    assert not session.get("_stop_uncertain")


def test_negative_ack_keeps_owner_unconfirmed(monkeypatch):
    session = state(monkeypatch)
    def interrupt(sid, **kwargs):
        return {"type": "interrupt.ack", "sid": sid, "request_id": kwargs["request_id"],
                "target_request_id": "A", "applied": False}
    setup(monkeypatch, types.SimpleNamespace(interrupt=interrupt))
    with pytest.raises(HostSendUncertain, match="not confirmed"):
        server._interrupt_session_turn("s", session, request_id="stop")
    assert session["_compute_host_active_request_id"] == "A"
    assert session.get("_stop_uncertain")
    assert not session.get("_stop_pending")
    with session["history_lock"]:
        server._enqueue_prompt(session, "C", None)
    assert server._drain_queued_prompt("rpc-C", "s", session) is False
    assert session["queued_prompt"]["text"] == "C"


def test_proven_unsent_stop_can_be_retried(monkeypatch):
    session = state(monkeypatch)
    session["_queued_prompt_generation"] = 4
    session["_last_stop_queue_generation"] = 2
    def interrupt(*args, **kwargs):
        with session["history_lock"]:
            server._enqueue_prompt(session, "C", None)
        raise HostSendNotSent("nothing offered")
    setup(monkeypatch, types.SimpleNamespace(interrupt=interrupt))
    with pytest.raises(HostSendNotSent):
        server._interrupt_session_turn("s", session, request_id="stop")
    assert not session.get("_stop_pending")
    assert not session.get("_stop_uncertain")
    assert session["_compute_host_active_request_id"] == "A"
    assert session["queued_prompt"]["text"] == "B"
    assert [q["text"] for q in session["queued_prompts"]] == ["C"]
    assert session["_queued_prompt_generation"] == 4
    assert session["_last_stop_queue_generation"] == 2
    assert not session.get("_turn_cancel_requested")


@pytest.mark.parametrize("change", ["generation", "owner", "session_key"])
def test_unsent_stop_does_not_overwrite_newer_parent_state(monkeypatch, change):
    session = state(monkeypatch)
    def interrupt(*args, **kwargs):
        with session["history_lock"]:
            server._enqueue_prompt(session, "C", None)
            if change == "generation":
                session["_queued_prompt_generation"] += 1
            elif change == "owner":
                session["_compute_host_active_request_id"] = "new-owner"
            else:
                session["session_key"] = "changed-session"
        raise HostSendNotSent("nothing offered")
    setup(monkeypatch, types.SimpleNamespace(interrupt=interrupt))
    with pytest.raises(HostSendNotSent, match="reconciliation"):
        server._interrupt_session_turn("s", session, request_id="stop")
    token = session["_stop_uncertain"]
    assert token["local_state_unconfirmed"] is True
    assert token["pre_cut_queue"][0]["text"] == "B"
    assert session["queued_prompt"]["text"] == "C"
    server._on_compute_host_turn_done("rpc-A", "s", session,
        {"type": "turn.end", "sid": "s", "request_id": "A"})
    assert session["_stop_uncertain"] is token
    assert session["queued_prompt"]["text"] == "C"


@pytest.mark.parametrize("duplicate_text", ["B", "changed"])
def test_unsent_stop_restoration_preserves_receipt_identity(monkeypatch, duplicate_text):
    session = state(monkeypatch)
    session["queued_prompt"]["context_input_event_id"] = "receipt-B"
    def interrupt(*args, **kwargs):
        with session["history_lock"]:
            server._enqueue_prompt(session, duplicate_text, None, context_input_event_id="receipt-B")
        raise HostSendNotSent("nothing offered")
    setup(monkeypatch, types.SimpleNamespace(interrupt=interrupt))
    with pytest.raises(HostSendNotSent):
        server._interrupt_session_turn("s", session, request_id="stop")
    if duplicate_text == "B":
        assert not session.get("_stop_uncertain")
        assert session["queued_prompt"]["context_input_event_id"] == "receipt-B"
        assert not session.get("queued_prompts")
    else:
        assert session["_stop_uncertain"]["local_state_unconfirmed"] is True
        assert session["_stop_uncertain"]["pre_cut_queue"][0]["text"] == "B"
        assert session["queued_prompt"]["text"] == "changed"


def test_unsent_stop_after_terminal_publishes_idle_state(monkeypatch):
    session = state(monkeypatch)
    session["queued_prompt"] = None
    emitted = []
    def interrupt(*args, **kwargs):
        server._on_compute_host_turn_done("rpc-A", "s", session,
            {"type": "turn.end", "sid": "s", "request_id": "A"})
        raise HostSendNotSent("nothing offered")
    setup(monkeypatch, types.SimpleNamespace(interrupt=interrupt))
    monkeypatch.setattr(server, "_session_info", lambda agent, s: {
        "running": bool(s.get("running") or s.get("_stop_pending") or s.get("_stop_uncertain"))})
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload: emitted.append(payload))
    with pytest.raises(HostSendNotSent):
        server._interrupt_session_turn("s", session, request_id="stop")
    assert emitted[-1]["running"] is False
    assert not session.get("_stop_uncertain")


def test_matching_late_terminal_releases_uncertain_stop_without_retry(monkeypatch):
    session = state(monkeypatch)
    attempts = []
    sent = []
    def interrupt(*args, **kwargs):
        attempts.append(kwargs)
        raise HostSendUncertain("ACK lost")
    setup(monkeypatch, types.SimpleNamespace(interrupt=interrupt))
    monkeypatch.setattr(server, "_submit_prompt_to_compute_host",
                        lambda rid, sid, state, text, **kw: sent.append(text) or server._ok(rid, {"status": "streaming"}))
    with pytest.raises(HostSendUncertain):
        server._interrupt_session_turn("s", session, request_id="stop")
    assert session.get("_stop_uncertain")
    with session["history_lock"]:
        server._enqueue_prompt(session, "C", None)
    server._on_compute_host_turn_done("rpc-A", "s", session,
        {"type": "turn.end", "sid": "s", "request_id": "A", "interrupted": True})
    assert sent == ["C"]
    assert len(attempts) == 1
    assert not session.get("_stop_uncertain")


def test_stop_cancelled_claim_is_not_restored(monkeypatch):
    session = state(monkeypatch)
    session.pop("_compute_host_active_request_id")
    session["running"] = False
    once = []
    setup(monkeypatch, types.SimpleNamespace(interrupt=lambda *a, **kw: None))
    def route(state):
        if not once:
            once.append(True)
            server._interrupt_session_turn("s", state, request_id="stop")
            with state["history_lock"]:
                server._enqueue_prompt(state, "C", None)
        return True
    monkeypatch.setattr(server, "_session_uses_compute_host", route)
    assert server._drain_queued_prompt("drain", "s", session)
    assert session["queued_prompt"]["text"] == "C", "Stop-cancelled B was resurrected"
