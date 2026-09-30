"""New input must be accepted after, not inside, a settling native Stop cut."""
import contextlib
import threading
import time
import types

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.host_supervisor import HostSendUncertain
from tests.test_tui_gateway_server import _session


@pytest.mark.parametrize("with_receipt", [True, False])
def test_new_input_waits_outside_session_lock_until_native_cut_verified(tmp_path, monkeypatch, with_receipt):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("stored", source="desktop")
    before = db.accept_context_input("stored", source="desktop", event_id="B", content="B")
    session = _session(session_key="stored", source="desktop", running=True,
                       agent_ready=threading.Event(), _compute_host_active_request_id="A")
    session["agent"] = None
    session["queued_prompt"] = {"text": "B", "context_input_event_id": "B", "transport": None}
    monkeypatch.setattr(server, "_sessions", {"s": session})
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_session_db", lambda s: contextlib.nullcontext(db))
    monkeypatch.setattr(server, "_load_cfg", lambda: {
        "dashboard": {"turn_isolation": True}, "compression": {"context_rebase_enabled": True}})
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *a: None)
    monkeypatch.setattr(server, "_clear_pending", lambda *a: None)
    monkeypatch.setattr(server, "_emit", lambda *a: None)
    monkeypatch.setattr(server, "_session_info", lambda *a: {})
    waiting = threading.Event()
    release = threading.Event()
    input_started = threading.Event()
    input_done = threading.Event()
    admission_wait_entered = threading.Event()
    original_wait = server._wait_for_stop_input_admission
    def wait_gate(*args, **kwargs):
        admission_wait_entered.set()
        return original_wait(*args, **kwargs)
    monkeypatch.setattr(server, "_wait_for_stop_input_admission", wait_gate)
    results = {}
    errors = []

    def interrupt(sid, **kwargs):
        waiting.set()
        assert release.wait(3)
        ack = {"type": "interrupt.ack", "sid": sid, "request_id": kwargs["request_id"],
               "target_request_id": "A", "applied": True}
        if with_receipt:
            control = db.record_context_stop("stored")
            results["cut"] = control
            ack["stop_receipt"] = {"session_key": "stored", "control": control}
        return ack
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda: types.SimpleNamespace(interrupt=interrupt))

    def stop():
        try:
            results["stop"] = server._interrupt_session_turn("s", session, request_id="stop")
        except Exception as exc:
            errors.append(exc)
    def send():
        try:
            input_started.set()
            results["input"] = server.handle_request({"id": "C", "method": "prompt.submit",
                "params": {"session_id": "s", "text": "C", "input_event_id": "C", "queued": True}})
        except Exception as exc:
            errors.append(exc)
        finally:
            input_done.set()
    stopper = threading.Thread(target=stop)
    sender = threading.Thread(target=send)
    stopper.start()
    try:
        assert waiting.wait(1)
        sender.start()
        assert input_started.wait(1)
        assert admission_wait_entered.wait(1)
        returned_early = input_done.wait(0.2)
        accepted_early = db.read_context_input("stored", source="desktop", event_id="C")
        assert accepted_early is None, f"native input committed before Stop cutoff: {accepted_early}"
        assert not returned_early, results.get("input")
        # The waiting request must not keep Stop from taking the session lock.
        acquired = session["history_lock"].acquire(timeout=0.2)
        assert acquired
        if acquired:
            session["history_lock"].release()
    finally:
        release.set()
        stopper.join(4)
        if sender.ident is not None:
            sender.join(4)
        db.close()
    assert not stopper.is_alive() and not sender.is_alive()
    reopened = SessionDB(tmp_path / "state.db")
    try:
        after = reopened.read_context_input("stored", source="desktop", event_id="C")
        if with_receipt:
            assert errors == []
            assert results["stop"] is True
            assert results["cut"]["input_sequence"] == before.sequence
            assert results["input"]["result"]["status"] == "queued"
            assert after is not None and after.sequence > results["cut"]["input_sequence"]
        else:
            assert len(errors) == 1 and isinstance(errors[0], HostSendUncertain)
            assert results["input"]["error"]["data"]["durable_input_accepted"] is False
            assert after is None
            assert reopened.read_context_stop("stored") is None
    finally:
        reopened.close()


def test_stop_input_wait_has_a_deadline_and_does_not_hold_history_lock():
    session = {"history_lock": threading.Lock(), "_stop_pending": {"input_release": threading.Event()}}
    started = time.monotonic()
    result = server._wait_for_stop_input_admission("input", session, deadline=started + 0.02)
    assert result is not None and result["error"]["code"] == 5032
    assert result["error"]["data"]["durable_input_accepted"] is False
    assert time.monotonic() - started < 0.5
    assert session["history_lock"].acquire(timeout=0.1)
    session["history_lock"].release()
