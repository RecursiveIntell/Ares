"""Stop controls must progress independently of slow renderer projections."""
import io
import json
import queue
import threading
import types
import pytest

from tui_gateway.host_supervisor import HostSupervisor


@pytest.mark.parametrize("limit", ["_MAX_PROJECTIONS", "_MAX_PROJECTION_BYTES"])
def test_ordinary_delivery_capacity_refusal_preserves_reserved_owner(tmp_path, monkeypatch, limit):
    from tui_gateway import host_supervisor as module
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    host._pending_turns["A"] = ("s", lambda _: pytest.fail("inline delivery"), host.boot_id)
    monkeypatch.setattr(module, limit, 0)
    with pytest.raises(module.HostProjectionFault):
        host._enqueue_projection('s', {'method': 'event'}, lambda _: pytest.fail('inline delivery'))
    assert "A" in host._pending_turns
    assert host._projection_faults == 1
    assert host._projection_count == host._projection_bytes == 0


def test_blocked_rpc_projection_cannot_starve_stop_ack_reader(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    ack_observed = threading.Event()
    rpc_finished = threading.Event()

    def sink(_message):
        entered.set()
        assert release.wait(10)
        rpc_finished.set()

    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False, rpc_sink=sink)
    waiter = queue.Queue(maxsize=1)
    host._pending_controls["stop"] = waiter
    frames = [
        {"type": "rpc", "sid": "s", "message": {
            "jsonrpc": "2.0", "method": "event",
            "params": {"type": "session.info", "session_id": "s", "payload": {"running": True}},
        }},
        {"type": "interrupt.ack", "sid": "s", "request_id": "stop",
         "target_request_id": "A", "applied": True},
    ]
    proc = types.SimpleNamespace(stdout=io.StringIO("".join(json.dumps(frame) + "\n" for frame in frames)))
    host._proc = proc
    host._hello = {"boot_id": "boot"}

    def reader():
        host._drain_stdout(proc)
        ack_observed.set()

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert entered.wait(2), "renderer projection did not start"
        assert ack_observed.wait(2), "renderer projection occupied the Stop acknowledgment reader"
        ack = waiter.get_nowait()
        assert ack["request_id"] == "stop" and ack["target_request_id"] == "A"
        assert ack["_host_boot_id"] == "boot"
        assert not rpc_finished.is_set(), "the witness did not retain the blocked projection"
    finally:
        release.set()
        thread.join(5)
        rpc_finished.wait(5)
    assert not thread.is_alive()


def test_rpc_and_terminal_projection_keep_same_session_order(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    reader_returned = threading.Event()
    terminal_done = threading.Event()
    other_done = threading.Event()
    rpc_finished = threading.Event()
    order = []

    def sink(_message):
        order.append("rpc-start")
        entered.set()
        assert release.wait(10)
        order.append("rpc-end")
        rpc_finished.set()

    def terminal(_frame):
        order.append("terminal")
        terminal_done.set()

    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False, rpc_sink=sink)
    host._pending_turns["A"] = ("s", terminal, host.boot_id)
    host._pending_turns["B"] = ("other", lambda _frame: other_done.set(), host.boot_id)

    def reader():
        host._handle_host_frame({"type": "rpc", "sid": "s", "message": {
            "method": "event", "params": {"type": "message.complete", "session_id": "s"},
        }})
        reader_returned.set()

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert entered.wait(2)
        host._complete_turn({"type": "turn.end", "sid": "s", "request_id": "A"})
        host._complete_turn({"type": "turn.end", "sid": "other", "request_id": "B"})
        assert other_done.wait(2), "a blocked session prevented its sibling from progressing"
        assert not terminal_done.is_set(), "terminal projection overtook its own earlier RPC"
        assert reader_returned.wait(2), "RPC projection remained on the reader"
    finally:
        release.set()
        thread.join(5)
        rpc_finished.wait(5)
        terminal_done.wait(5)
    assert order == ["rpc-start", "rpc-end", "terminal"]
    assert not thread.is_alive()
