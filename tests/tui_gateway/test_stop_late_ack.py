"""A late Stop ACK is retained evidence, not permission to target a successor."""
import threading

import pytest

from tui_gateway.host_supervisor import HostSupervisor, HostSendUncertain


def test_late_matching_interrupt_ack_reaches_original_observer(tmp_path, monkeypatch):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    host._hello = {"boot_id": "boot"}
    monkeypatch.setattr(host, "_ensure_started_by", lambda _deadline: None)
    monkeypatch.setattr(host, "_send_owner_control_bounded", lambda *_args, **_kwargs: None)
    observed = []
    delivered = threading.Event()

    def late(ack):
        observed.append(ack)
        delivered.set()

    with pytest.raises(HostSendUncertain, match="timed out"):
        host.interrupt("s", request_id="stop", target_request_id="A", wait=True,
                       timeout=0.01, expected_boot_id="boot", on_late_ack=late)
    host._handle_host_frame({"type": "interrupt.ack", "sid": "s", "request_id": "stop",
                             "target_request_id": "A", "applied": True, "_host_boot_id": "boot"})
    assert delivered.wait(2), "matching late acknowledgment was discarded"
    assert len(observed) == 1 and observed[0]["target_request_id"] == "A"
    assert "stop" not in host._pending_controls


def test_late_wrong_scope_cannot_consume_original_registration(tmp_path, monkeypatch):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    host._hello = {"boot_id": "boot"}
    monkeypatch.setattr(host, "_ensure_started_by", lambda _deadline: None)
    monkeypatch.setattr(host, "_send_owner_control_bounded", lambda *_args, **_kwargs: None)
    observed = []
    delivered = threading.Event()

    def late(ack):
        observed.append(ack)
        delivered.set()

    with pytest.raises(HostSendUncertain):
        host.interrupt("s", request_id="stop", target_request_id="A", wait=True,
                       timeout=0.01, expected_boot_id="boot", on_late_ack=late)
    for wrong in ({"sid": "other"}, {"target_request_id": "B"}, {"_host_boot_id": "retired"}, {"applied": "true"}):
        host._handle_host_frame({"type": "interrupt.ack", "sid": "s", "request_id": "stop",
                                 "target_request_id": "A", "applied": True, "_host_boot_id": "boot", **wrong})
        assert "stop" in host._pending_controls, "foreign ACK consumed the admitted Stop observer"
    host._handle_host_frame({"type": "interrupt.ack", "sid": "s", "request_id": "stop",
                             "target_request_id": "A", "applied": True, "_host_boot_id": "boot"})
    assert delivered.wait(2)
    assert len(observed) == 1


def test_late_ack_callback_does_not_run_on_reader(tmp_path, monkeypatch):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    host._hello = {"boot_id": "boot"}
    monkeypatch.setattr(host, "_ensure_started_by", lambda _deadline: None)
    monkeypatch.setattr(host, "_send_owner_control_bounded", lambda *_args, **_kwargs: None)
    entered, release, returned, completed = (threading.Event() for _ in range(4))

    def late(_ack):
        entered.set()
        assert release.wait(10)
        completed.set()

    with pytest.raises(HostSendUncertain):
        host.interrupt("s", request_id="stop", target_request_id="A", wait=True,
                       timeout=0.01, expected_boot_id="boot", on_late_ack=late)

    def reader():
        host._handle_host_frame({"type": "interrupt.ack", "sid": "s", "request_id": "stop",
                                 "target_request_id": "A", "applied": True, "_host_boot_id": "boot"})
        returned.set()

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert entered.wait(2)
        assert returned.wait(2), "late receipt readback occupied the ACK reader"
    finally:
        release.set()
        thread.join(5)
        completed.wait(5)
    assert not thread.is_alive()


def test_retained_stop_observers_are_bounded_before_sending(tmp_path, monkeypatch):
    from tui_gateway import host_supervisor
    from tui_gateway.host_supervisor import HostSendNotSent

    monkeypatch.setattr(host_supervisor, "_MAX_RETAINED_INTERRUPT_ACKS", 2)
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    host._hello = {"boot_id": "boot"}
    monkeypatch.setattr(host, "_ensure_started_by", lambda _deadline: None)
    sent = []
    monkeypatch.setattr(host, "_send_owner_control_bounded", lambda frame, **kw: sent.append(frame["request_id"]))
    for request in ("first", "second"):
        with pytest.raises(HostSendUncertain):
            host.interrupt("s", request_id=request, target_request_id="A", wait=True,
                           timeout=0, on_late_ack=lambda _ack: None)
    with pytest.raises(HostSendNotSent, match="capacity"):
        host.interrupt("s", request_id="third", target_request_id="A", wait=True,
                       timeout=0, on_late_ack=lambda _ack: None)
    assert sent == ["first", "second"]
    assert set(host._pending_controls) == {"first", "second"}


def test_expired_late_observer_neither_confirms_nor_blocks_new_control(tmp_path, monkeypatch):
    from tui_gateway import host_supervisor

    clock = [100.0]
    monkeypatch.setattr(host_supervisor.time, "monotonic", lambda: clock[0])
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    host._hello = {"boot_id": "boot"}
    monkeypatch.setattr(host, "_ensure_started_by", lambda _deadline: None)
    monkeypatch.setattr(host, "_send_owner_control_bounded", lambda *args, **kw: None)
    observed = []
    new_done = threading.Event()
    with pytest.raises(HostSendUncertain):
        host.interrupt("s", request_id="old", target_request_id="A", wait=True,
                       timeout=0, on_late_ack=lambda ack: observed.append(ack["request_id"]))
    clock[0] += 600
    with pytest.raises(HostSendUncertain):
        host.interrupt("s", request_id="new", target_request_id="B", wait=True,
                       timeout=0, on_late_ack=lambda ack: (observed.append(ack["request_id"]), new_done.set()))
    assert "old" not in host._pending_controls
    for request, target in (("old", "A"), ("new", "B")):
        host._handle_host_frame({"type": "interrupt.ack", "sid": "s", "request_id": request,
                                 "target_request_id": target, "applied": True, "_host_boot_id": "boot"})
    assert new_done.wait(2)
    assert observed == ["new"]
