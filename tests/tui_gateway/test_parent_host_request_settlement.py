"""Only the matching host request may settle a parent session."""
import threading
from types import SimpleNamespace

from tests.tui_gateway.terminal_settlement_helpers import wait_for_terminal_projection

import pytest

from tui_gateway import server
from tui_gateway.host_supervisor import HostSendNotSent, HostSupervisor


def state(monkeypatch):
    session = {"history_lock": threading.Lock(), "history": [], "agent": None,
            "running": True, "session_key": "stored", "_compute_host_active_request_id": "A"}
    monkeypatch.setitem(server._sessions, "s", session)
    return session


def test_wrong_or_duplicate_terminal_cannot_settle_parent(monkeypatch):
    session = state(monkeypatch)
    effects = []
    monkeypatch.setattr(server, "_clear_inflight_turn", lambda s: effects.append("clear"))
    monkeypatch.setattr(server, "_emit", lambda *a: effects.append("emit"))
    monkeypatch.setattr(server, "_session_info", lambda *a: {})
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a: effects.append("drain"))
    for sid, rid in (("s", "B"), ("other", "A")):
        server._on_compute_host_turn_done("rpc", "s", session,
            {"type": "turn.error", "sid": sid, "request_id": rid, "message": "session busy"})
    assert session["running"] is True
    assert effects == []
    terminal = {"type": "turn.end", "sid": "s", "request_id": "A"}
    server._on_compute_host_turn_done("rpc", "s", session, terminal)
    assert session["running"] is False
    assert not session.get("_compute_host_active_request_id")
    once = list(effects)
    server._on_compute_host_turn_done("rpc", "s", session, terminal)
    assert effects == once
    assert effects.count("drain") == 1


def test_second_submission_cannot_replace_parent_owner(monkeypatch):
    session = state(monkeypatch)
    sent = []
    class Host:
        def submit_turn(self, frame, **kw):
            sent.append(frame)
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: Host())
    result = server._submit_prompt_to_compute_host("rpc-B", "s", session, "B")
    assert result["error"]["code"] == 4009
    assert sent == []
    assert session["_compute_host_active_request_id"] == "A"


def test_proven_zero_byte_refusal_does_not_quarantine_parent(monkeypatch):
    session = state(monkeypatch)
    session.pop("_compute_host_active_request_id")
    class Host:
        def submit_turn(self, frame, **kw):
            raise HostSendNotSent("no bytes offered")
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: Host())
    response = server._submit_prompt_to_compute_host("rpc", "s", session, "A")
    assert response["error"]["data"]["delivery"] == "not_sent"
    assert not session.get("_compute_host_active_request_id")
    assert not session.get("_host_delivery_uncertain")


def test_send_exception_retains_uncertain_owner(monkeypatch):
    session = state(monkeypatch)
    session.pop("_compute_host_active_request_id")
    sent = []
    class Host:
        def submit_turn(self, frame, **kw):
            sent.append(frame)
            raise OSError("write outcome unknown")
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: Host())
    result = server._submit_prompt_to_compute_host("rpc-A", "s", session, "A")
    assert result["error"]["code"] == 5019
    assert session["_compute_host_active_request_id"] == sent[0]["request_id"]
    assert session["_host_delivery_uncertain"] is True
    assert session["running"] is True


def test_late_terminal_resolves_uncertain_send_without_replay(tmp_path, monkeypatch):
    session = state(monkeypatch)
    session.pop("_compute_host_active_request_id")
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    # This test starts after the host hello has established readiness. Keep
    # the real admission path so a write exception retains its boot owner.
    proc = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(host, "_proc", proc)
    monkeypatch.setattr(host, "_ready_proc", proc)
    monkeypatch.setattr(host, "_hello", {"boot_id": "fixture-boot"})
    assert host.is_ready()
    sent = []
    def fail_send(frame, **_kwargs):
        sent.append(frame)
        raise OSError("uncertain write")
    monkeypatch.setattr(host, "_send_frame", fail_send)
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: host)
    monkeypatch.setattr(server, "_session_info", lambda *a: {})
    monkeypatch.setattr(server, "_clear_inflight_turn", lambda *a: None)
    emitted = []
    drained = []
    monkeypatch.setattr(server, "_emit", lambda *a: emitted.append(a))
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a: drained.append(a))
    result = server._submit_prompt_to_compute_host("rpc", "s", session, "A")
    assert result["error"]["data"]["delivery"] == "uncertain"
    assert host._pending_turns[sent[0]["request_id"]][2] == "fixture-boot"
    terminal = {"type": "turn.end", "sid": "s", "request_id": sent[0]["request_id"]}
    host._complete_turn(terminal)
    wait_for_terminal_projection(host)
    host._complete_turn(terminal)
    wait_for_terminal_projection(host)
    assert len(sent) == 1
    assert len(drained) == 1
    assert len(emitted) == 1
    assert session["running"] is False
    assert not session.get("_host_delivery_uncertain")
    assert not session.get("_compute_host_active_request_id")


def test_projection_failure_retains_owner_and_does_not_repeat_emit(monkeypatch):
    session = state(monkeypatch)
    calls = []
    monkeypatch.setattr(server, "_clear_inflight_turn", lambda *a: None)
    monkeypatch.setattr(server, "_session_info", lambda *a: {})
    def fail_emit(*args):
        calls.append(args)
        raise OSError("projection outcome unknown")
    monkeypatch.setattr(server, "_emit", fail_emit)
    terminal = {"type": "turn.end", "sid": "s", "request_id": "A"}
    with pytest.raises(OSError, match="projection outcome unknown"):
        server._on_compute_host_turn_done("rpc", "s", session, terminal)
    server._on_compute_host_turn_done("rpc", "s", session, terminal)
    assert len(calls) == 1
    assert session["_compute_host_active_request_id"] == "A"
    assert session["_compute_host_terminal"] == terminal


def test_dequeued_terminal_after_abort_claim_publishes_and_drains_nothing(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    class PausedHistoryLock:
        def __enter__(self):
            entered.set()
            assert release.wait(3), "projection did not resume"
        def __exit__(self, *_exc):
            return False

    session = state(monkeypatch)
    session["history_lock"] = PausedHistoryLock()
    effects = []
    monkeypatch.setattr(server, "_clear_inflight_turn", lambda *a: effects.append("clear"))
    monkeypatch.setattr(server, "_emit", lambda *a: effects.append("emit"))
    monkeypatch.setattr(server, "_session_info", lambda *a: {})
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a: effects.append("drain"))
    terminal = {"type": "turn.end", "sid": "s", "request_id": "A"}
    failures = []

    def project():
        try:
            server._on_compute_host_turn_done("rpc", "s", session, terminal)
        except Exception as exc:
            failures.append(exc)

    worker = threading.Thread(target=project)
    worker.start()
    try:
        assert entered.wait(3), "terminal did not reach projection gate"
        assert server._pop_session_by_id("s") is session
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert not failures
    assert effects == []
    assert session["running"] is True
    assert session["_compute_host_active_request_id"] == "A"


def test_abort_after_terminal_claim_before_emit_publishes_and_drains_nothing(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    session = state(monkeypatch)
    effects = []

    def paused_clear(_session):
        entered.set()
        assert release.wait(3), "terminal projection did not resume"
        effects.append("clear")

    monkeypatch.setattr(server, "_clear_inflight_turn", paused_clear)
    monkeypatch.setattr(server, "_emit", lambda *a: effects.append("emit"))
    monkeypatch.setattr(server, "_session_info", lambda *a: {})
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a: effects.append("drain"))
    failures = []

    def project():
        try:
            server._on_compute_host_turn_done(
                "rpc", "s", session, {"type": "turn.end", "sid": "s", "request_id": "A"})
        except Exception as exc:
            failures.append(exc)

    worker = threading.Thread(target=project)
    worker.start()
    try:
        assert entered.wait(3), "terminal did not claim projection"
        assert server._pop_session_by_id("s") is session
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert not failures
    assert "emit" not in effects
    assert "drain" not in effects
