"""Real POSIX-pipe write deadlines; no background sender or late replay."""
import io
import json
import os
import queue
import threading

from tests.tui_gateway.terminal_settlement_helpers import wait_for_terminal_projection
import time
from types import SimpleNamespace

import pytest

from tui_gateway.host_supervisor import HostBootMismatch, HostSendNotSent, HostSendUncertain, HostSupervisor

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX pipe execution proof")


@pytest.fixture
def pipe(tmp_path):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    read, write = os.pipe()
    os.set_blocking(read, False)
    stream = os.fdopen(write, "wb", buffering=0)
    host._proc = SimpleNamespace(stdin=stream, poll=lambda: None)
    host._ready_proc = host._proc
    host._hello = {"boot_id": "A"}
    yield host, read, write
    stream.close()
    os.close(read)


def drain(read):
    data = bytearray()
    while True:
        try:
            part = os.read(read, 65536)
        except BlockingIOError:
            return bytes(data)
        if not part:
            return bytes(data)
        data.extend(part)


def test_full_pipe_is_not_sent_and_never_replays(pipe):
    host, read, write = pipe
    os.set_blocking(write, False)
    filled = 0
    while True:
        try:
            filled += os.write(write, b"x" * 4096)
        except BlockingIOError:
            break
    before = {t.ident for t in threading.enumerate()}
    started = time.monotonic()
    with pytest.raises(HostSendNotSent):
        host._send_frame({"request_id": "A"}, deadline=started + 0.06)
    assert time.monotonic() - started < 0.5
    assert drain(read) == b"x" * filled
    time.sleep(0.07)
    assert drain(read) == b""
    assert {t.ident for t in threading.enumerate()} <= before


def test_partial_write_poison_prevents_appending_another_frame(pipe):
    host, read, _ = pipe
    with pytest.raises(HostSendUncertain):
        host._send_frame({"text": "z" * 1000000}, deadline=time.monotonic() + 0.06)
    partial = drain(read)
    assert partial and not partial.endswith(b"\n")
    with pytest.raises(HostSendNotSent, match="incomplete"):
        host._send_frame({"request_id": "B"})
    assert drain(read) == b""


def test_boot_fence_is_checked_after_writer_lock(pipe):
    host, read, _ = pipe
    errors = []
    def send():
        try:
            host._send_frame({"request_id": "A"}, deadline=time.monotonic() + 0.5, expected_boot_id="A")
        except Exception as exc:
            errors.append(exc)
    with host._write_lock:
        worker = threading.Thread(target=send)
        worker.start()
        host._hello = {"boot_id": "B"}
    worker.join(1)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], HostBootMismatch)
    assert drain(read) == b""


def test_large_concurrent_frames_remain_whole(pipe):
    host, read, _ = pipe
    payloads = [{"id": n, "text": str(n) * 100000} for n in range(3)]
    data = bytearray()
    stop = threading.Event()
    errors = []
    def receive():
        while not stop.is_set():
            data.extend(drain(read))
            time.sleep(0.001)
        data.extend(drain(read))
    def send(frame):
        try:
            host._send_frame(frame, deadline=time.monotonic() + 2)
        except Exception as exc:
            errors.append(exc)
    reader = threading.Thread(target=receive)
    writers = [threading.Thread(target=send, args=(frame,)) for frame in payloads]
    reader.start()
    try:
        for worker in writers:
            worker.start()
        for worker in writers:
            worker.join(3)
        assert not any(worker.is_alive() for worker in writers)
    finally:
        stop.set()
        reader.join(1)
    assert not errors
    assert sorted((json.loads(line) for line in data.splitlines()), key=lambda v: v["id"]) == payloads


def test_control_and_terminal_registries_progress_while_process_lock_is_busy(pipe):
    host, _, _ = pipe
    held = threading.Event()
    release = threading.Event()
    settled = threading.Event()
    def hold():
        with host._lock:
            held.set()
            release.wait(3)
    worker = threading.Thread(target=hold)
    worker.start()
    assert held.wait(1)
    try:
        started = time.monotonic()
        host.observe_session("s", lambda frame: settled.set(), request_id="A")
        waiter = queue.Queue(maxsize=1)
        with host._control_lock:
            host._pending_controls["ack"] = waiter
        host._complete_turn({"type": "turn.end", "sid": "s", "request_id": "A"})
        wait_for_terminal_projection(host)
        host._handle_host_frame({"type": "interrupt.ack", "request_id": "ack"})
        assert settled.is_set()
        assert waiter.get_nowait()["type"] == "interrupt.ack"
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        worker.join(1)
    assert not worker.is_alive()


def test_unsupported_stream_does_not_use_blocking_fallback(pipe):
    host, _, _ = pipe
    class Stream(io.StringIO):
        def write(self, data):
            pytest.fail("blocking fallback called")
    host._proc = SimpleNamespace(stdin=Stream(), poll=lambda: None)
    with pytest.raises(HostSendNotSent):
        host._send_frame({"request_id": "A"})


@pytest.mark.parametrize("error,retained", [(HostSendNotSent, False), (HostSendUncertain, True)])
def test_submit_registry_preserves_only_possible_delivery(pipe, monkeypatch, error, retained):
    host, _, _ = pipe
    monkeypatch.setattr(host, "start", lambda: None)
    def fail(frame, **_kwargs):
        raise error("test transport evidence")
    monkeypatch.setattr(host, "_send_frame", fail)
    outcomes = []
    with pytest.raises(error):
        host.submit_turn({"sid": "s", "request_id": "A"}, on_complete=outcomes.append)
    assert ("A" in host._pending_turns) is retained
    assert outcomes == []
