"""One supervisor deadline covers startup, send and acknowledgement."""
import os
import threading
import time

import pytest

from tui_gateway import server
from tui_gateway.host_supervisor import HostSendNotSent, HostSendUncertain, HostSupervisor
from tests.tui_gateway.test_host_pipe_deadline import pipe, drain  # noqa: F401


def test_startup_timeout_never_sends_later(tmp_path, monkeypatch):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    sent = []
    def start():
        entered.set()
        release.wait(2)
        finished.set()
    monkeypatch.setattr(host, "start", start)
    monkeypatch.setattr(host, "_send_frame", lambda frame, **kw: sent.append(frame))
    began = time.monotonic()
    try:
        with pytest.raises(HostSendNotSent):
            host.interrupt("s", target_request_id="A", wait=True, timeout=0.05)
        assert entered.is_set()
        assert time.monotonic() - began < 0.5
        assert not host._pending_controls
    finally:
        release.set()
    assert finished.wait(1)
    assert sent == []


def test_interrupt_ack_matches_exact_session_request_and_target(tmp_path, monkeypatch):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    monkeypatch.setattr(host, "is_running", lambda: True)
    def send(frame, **kwargs):
        host._handle_host_frame({"type": "interrupt.ack", "sid": frame["sid"],
            "request_id": frame["request_id"], "target_request_id": frame["target_request_id"], "applied": True})
    monkeypatch.setattr(host, "_send_frame", send)
    ack = host.interrupt("s", request_id="stop", target_request_id="A", wait=True, timeout=0.1)
    assert ack is not None and ack["applied"] is True
    assert not host._pending_controls


def test_missing_ack_is_uncertain_not_not_sent(tmp_path, monkeypatch):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    monkeypatch.setattr(host, "is_running", lambda: True)
    sent = []
    monkeypatch.setattr(host, "_send_frame", lambda frame, **kw: sent.append(frame))
    with pytest.raises(HostSendUncertain, match="acknowledgement"):
        host.interrupt("s", target_request_id="A", wait=True, timeout=0.04)
    assert len(sent) == 1
    assert not host._pending_controls


@pytest.mark.parametrize("bad_field,bad_value", [("sid", "other"), ("target_request_id", "B")])
def test_mismatched_ack_is_not_success(tmp_path, monkeypatch, bad_field, bad_value):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    monkeypatch.setattr(host, "is_running", lambda: True)
    def send(frame, **kwargs):
        ack = {"type": "interrupt.ack", "sid": "s", "request_id": "stop",
               "target_request_id": "A", "applied": True, bad_field: bad_value}
        host._handle_host_frame(ack)
    monkeypatch.setattr(host, "_send_frame", send)
    with pytest.raises(HostSendUncertain, match="acknowledgement"):
        host.interrupt("s", request_id="stop", target_request_id="A", wait=True, timeout=0.1)
    assert not host._pending_controls


@pytest.mark.parametrize("operation", ["lookup", "control"])
def test_other_controls_cleanup_waiter_on_send_refusal(tmp_path, monkeypatch, operation):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    monkeypatch.setattr(host, "is_running", lambda: True)
    def send(frame, **kwargs):
        raise HostSendNotSent("nothing offered")
    monkeypatch.setattr(host, "_send_frame", send)
    with pytest.raises(HostSendNotSent):
        if operation == "lookup":
            host.lookup_session_key("stored", timeout=0.05)
        else:
            host.control("s", route_name="config.set.model", timeout=0.05)
    assert not host._pending_controls


@pytest.mark.parametrize("operation", ["lookup", "control"])
def test_other_controls_ack_timeout_is_uncertain(tmp_path, monkeypatch, operation):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    monkeypatch.setattr(host, "is_running", lambda: True)
    sent = []
    monkeypatch.setattr(host, "_send_frame", lambda frame, **kw: sent.append(frame))
    with pytest.raises(HostSendUncertain, match="acknowledgement"):
        if operation == "lookup":
            host.lookup_session_key("stored", timeout=0.03)
        else:
            host.control("s", route_name="config.set.model", timeout=0.03)
    assert len(sent) == 1
    assert not host._pending_controls


@pytest.mark.skipif(os.name != "posix", reason="real pipe deadline proof")
@pytest.mark.parametrize("operation", ["interrupt", "control"])
def test_pipe_wait_and_ack_share_one_budget(pipe, operation):
    host, read, write = pipe
    os.set_blocking(write, False)
    filled = 0
    while True:
        try:
            filled += os.write(write, b"x" * 4096)
        except BlockingIOError:
            break
    drained = []
    def free_pipe():
        time.sleep(0.1)
        drained.append(drain(read))
    reader = threading.Thread(target=free_pipe)
    reader.start()
    started = time.monotonic()
    try:
        with pytest.raises(HostSendUncertain, match="acknowledgement"):
            if operation == "interrupt":
                host.interrupt("s", target_request_id="A", wait=True, timeout=0.15)
            else:
                host.control("s", route_name="config.set.model", timeout=0.15)
        assert time.monotonic() - started < 0.23
    finally:
        reader.join(1)
    data = b"".join(drained) + drain(read)
    assert data[:filled] == b"x" * filled
    assert len(data[filled:].splitlines()) == 1
    assert not host._pending_controls


def test_supervisor_lookup_does_not_autostart_under_global_lock(monkeypatch):
    import tui_gateway.host_supervisor as module
    created = []
    def constructor(**kwargs):
        created.append(kwargs)
        return object()
    monkeypatch.setattr(module, "HostSupervisor", constructor)
    monkeypatch.setattr(server, "_compute_host_supervisor", None)
    server._get_compute_host_supervisor({"turn_isolation": True})
    assert created[0]["autostart"] is False
