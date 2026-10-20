"""Late control evidence is reconciled only against its captured owner."""
import types
import threading
import pytest
from tui_gateway import server
from tui_gateway.host_supervisor import HostSendUncertain

def state(monkeypatch):
    session = {"history_lock": threading.Lock(), "history": [], "agent": None,
               "session_key": "stored", "running": True, "_compute_host_active_request_id": "A"}
    monkeypatch.setitem(server._sessions, "s", session)
    return session


def setup(monkeypatch, host):
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: True)
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: host)
    monkeypatch.setattr(server, "_clear_pending", lambda *a: None)
    monkeypatch.setattr(server, "_session_info", lambda *a, lock_held=False: {})
    monkeypatch.setattr(server, "_emit", lambda *a: None)


def test_known_owner_refusal_restores_local_cut_without_uncertainty(monkeypatch):
    session = state(monkeypatch)
    session["queued_prompt"] = {"text": "kept"}
    def interrupt(sid, **kw):
        return {"type": "interrupt.ack", "sid": sid, "request_id": kw["request_id"],
                "target_request_id": "A", "applied": False,
                "failure_code": "turn_owner_changed", "effect_disposition": "not_applied"}
    setup(monkeypatch, types.SimpleNamespace(interrupt=interrupt))
    with pytest.raises(Exception):
        server._interrupt_session_turn("s", session, request_id="stop")
    assert not session.get("_stop_uncertain")
    assert session["queued_prompt"]["text"] == "kept"


@pytest.mark.parametrize("race", [False, True])
def test_parent_observes_late_ack_even_during_uncertain_transition(monkeypatch, race):
    session = state(monkeypatch)
    callbacks = []
    def interrupt(sid, **kw):
        callbacks.append(kw.get("on_late_ack"))
        ack = {"type": "interrupt.ack", "sid": sid, "request_id": kw["request_id"],
               "target_request_id": "A", "applied": True, "_host_boot_id": "boot"}
        if race and callbacks[-1]:
            callbacks[-1](ack)
        raise HostSendUncertain("timeout")
    setup(monkeypatch, types.SimpleNamespace(interrupt=interrupt, boot_id="boot"))
    monkeypatch.setattr(server, "_native_stop_requires_receipt", lambda _: False)
    with pytest.raises(HostSendUncertain):
        server._interrupt_session_turn("s", session, request_id="stop")
    assert callable(callbacks[0]), "parent discarded late ACK evidence"
    if not race:
        callbacks[0]({"type": "interrupt.ack", "sid": "s", "request_id": "stop",
                      "target_request_id": "A", "applied": True, "_host_boot_id": "boot"})
    assert not session.get("_stop_uncertain")
    assert session["running"] is True, "ACK is not execution retirement"


def test_late_ack_does_not_clear_successor_or_changed_scope(monkeypatch):
    session = state(monkeypatch)
    callbacks = []
    def interrupt(sid, **kw):
        callbacks.append(kw.get("on_late_ack"))
        raise HostSendUncertain("timeout")
    setup(monkeypatch, types.SimpleNamespace(interrupt=interrupt, boot_id="boot"))
    monkeypatch.setattr(server, "_native_stop_requires_receipt", lambda _: False)
    with pytest.raises(HostSendUncertain):
        server._interrupt_session_turn("s", session, request_id="stop")
    assert callable(callbacks[0])
    token = session["_stop_uncertain"]
    session["session_key"] = "other"
    callbacks[0]({"type": "interrupt.ack", "sid": "s", "request_id": "stop",
                  "target_request_id": "A", "applied": True, "_host_boot_id": "boot"})
    assert session["_stop_uncertain"] is token


def test_explicit_repeat_after_retirement_records_canonical_missing_receipt(tmp_path, monkeypatch):
    import contextlib
    from hermes_state import SessionDB
    session = state(monkeypatch)
    db = SessionDB(tmp_path / "state.db")
    db.create_session("stored", source="desktop")
    monkeypatch.setattr(server, '_db', db)
    attempts = []
    def interrupt(sid, **kw):
        attempts.append(kw)
        token = session["_stop_pending"]
        token["terminal"] = {"type": "turn.end", "sid": sid, "request_id": "A", "_host_boot_id": "boot"}
        session.pop("_compute_host_active_request_id")
        return {"type": "interrupt.ack", "sid": sid, "request_id": kw["request_id"],
                "target_request_id": "A", "applied": True, "_host_boot_id": "boot"}
    setup(monkeypatch, types.SimpleNamespace(interrupt=interrupt, boot_id="boot"))
    monkeypatch.setattr(server, "_session_db", lambda _: contextlib.nullcontext(db))
    monkeypatch.setattr(server, "_native_stop_requires_receipt", lambda _: True)
    try:
        with pytest.raises(HostSendUncertain):
            server._interrupt_session_turn("s", session, request_id="stop")
        assert db.read_context_stop("stored") is None
        assert server._interrupt_session_turn("s", session, request_id="repeat")
        assert db.read_context_stop("stored") is not None
        assert len(attempts) == 1, "repeat must not retarget or resend execution cancellation"
        assert not session.get("_stop_uncertain")
    finally:
        db.close()
