"""Terminal projection cannot monopolize the host's ACK reader."""
import queue
import threading

from tui_gateway import host_supervisor
from tui_gateway.host_supervisor import HostSupervisor


def test_blocked_terminal_callback_does_not_block_ack_reader(tmp_path):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    entered = threading.Event()
    release = threading.Event()
    reader_returned = threading.Event()
    waiter = queue.Queue(maxsize=1)
    host._pending_controls["stop"] = waiter
    def callback(frame):
        entered.set()
        release.wait(3)
    host._pending_turns["A"] = ("s", callback, host.boot_id)
    def reader():
        host._handle_host_frame({"type": "turn.end", "sid": "s", "request_id": "A"})
        host._handle_host_frame({"type": "interrupt.ack", "sid": "s", "request_id": "stop"})
        reader_returned.set()
    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert entered.wait(1)
        assert reader_returned.wait(0.3), "terminal callback occupied the ACK reader"
        assert waiter.get_nowait()["request_id"] == "stop"
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()


def test_same_session_callbacks_are_ordered_other_session_progresses(tmp_path):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    entered = threading.Event()
    release = threading.Event()
    b_done = threading.Event()
    c_done = threading.Event()
    order = []
    def first(frame):
        order.append("A-enter")
        entered.set()
        release.wait(3)
        order.append("A-exit")
    def second(frame):
        order.append("B")
        b_done.set()
    host._pending_turns.update(A=("s", first, host.boot_id), B=("s", second, host.boot_id), C=("other", lambda f: c_done.set(), host.boot_id))
    try:
        host._complete_turn({"type": "turn.end", "sid": "s", "request_id": "A"})
        assert entered.wait(1)
        host._complete_turn({"type": "turn.end", "sid": "s", "request_id": "B"})
        host._complete_turn({"type": "turn.end", "sid": "other", "request_id": "C"})
        assert c_done.wait(1)
        assert not b_done.is_set()
    finally:
        release.set()
    assert b_done.wait(2)
    assert order == ["A-enter", "A-exit", "B"]


def test_worker_start_failure_retains_terminal_without_inline_callback(tmp_path, monkeypatch):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    observed = []
    host._pending_turns["A"] = ("s", observed.append, host.boot_id)
    class FailedThread:
        def __init__(self, *args, **kwargs):
            pass
        def start(self):
            raise RuntimeError("thread creation refused")
    monkeypatch.setattr(host_supervisor, "_Thread", FailedThread)
    terminal = {"type": "turn.end", "sid": "s", "request_id": "A"}
    host._complete_turn(terminal)
    assert observed == []
    assert host._terminal_queues["s"][0][0] == terminal
    assert not host._terminal_workers


def test_projection_error_does_not_retry_or_block_later_terminal(tmp_path):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()
    attempts = []
    def fail(frame):
        attempts.append(frame["request_id"])
        entered.set()
        release.wait(3)
        raise RuntimeError("projection failed")
    host._pending_turns["A"] = ("s", fail, host.boot_id)
    host._pending_turns["B"] = ("s", lambda frame: done.set(), host.boot_id)
    try:
        host._complete_turn({"type": "turn.end", "sid": "s", "request_id": "A"})
        assert entered.wait(1)
        host._complete_turn({"type": "turn.end", "sid": "s", "request_id": "B"})
    finally:
        release.set()
    assert done.wait(2)
    assert attempts == ["A"]
