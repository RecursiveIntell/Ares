"""Host admission belongs to an exact request, not a transient running flag."""
import io
import json
import threading
import types

from tui_gateway import server

import pytest

from tui_gateway.compute_host import ComputeHost


def frames(output):
    return [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]


def test_second_request_refused_while_first_worker_owns_session(monkeypatch):
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    entered = threading.Event()
    release = threading.Event()
    starts = []

    def run(frame):
        starts.append(frame["request_id"])
        entered.set()
        release.wait(3)

    monkeypatch.setattr(host, "_run_real_turn", run)
    try:
        host._handle_turn_start({"sid": "s", "request_id": "A"})
        assert entered.wait(2)
        host._handle_turn_start({"sid": "s", "request_id": "B"})
        assert starts == ["A"]
        refused = [f for f in frames(output) if f.get("request_id") == "B"]
        assert len(refused) == 1
        assert refused[0]["reason"] == "admission_refused_busy"
    finally:
        release.set()
        host.close()


def test_terminal_allows_successor_before_prior_worker_retires(monkeypatch):
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    a_terminal = threading.Event()
    release_a = threading.Event()
    b_started = threading.Event()
    release_b = threading.Event()

    def run(frame):
        if frame["request_id"] == "A":
            host._emit_real_turn_terminal({"type": "turn.end", **frame})
            a_terminal.set()
            release_a.wait(3)
        else:
            b_started.set()
            release_b.wait(3)
            host._emit_real_turn_terminal({"type": "turn.end", **frame})

    monkeypatch.setattr(host, "_run_real_turn", run)
    try:
        host._handle_turn_start({"sid": "s", "request_id": "A"})
        assert a_terminal.wait(2)
        with host._turn_futures_lock:
            a_future = next(iter(host._turn_futures))
        host._handle_turn_start({"sid": "s", "request_id": "B"})
        assert b_started.wait(2)
        release_a.set()
        a_future.result(timeout=2)
        assert host._active_request_ids["s"] == "B"
        host._handle_turn_start({"sid": "s", "request_id": "C"})
        assert any(f.get("request_id") == "C" and f.get("reason") == "admission_refused_busy"
                   for f in frames(output))
    finally:
        release_a.set()
        release_b.set()
        host.close()


def test_terminal_output_does_not_hold_admission_lock(monkeypatch):
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    entered = threading.Event()
    release = threading.Event()
    def blocked_emit(frame):
        entered.set()
        release.wait(3)
    monkeypatch.setattr(host, "emit", blocked_emit)
    host._active_request_ids["s"] = "A"
    worker = threading.Thread(target=host._emit_real_turn_terminal,
                              args=({"type": "turn.end", "sid": "s", "request_id": "A"},))
    worker.start()
    try:
        assert entered.wait(2)
        acquired = host._turn_futures_lock.acquire(timeout=0.5)
        assert acquired, "terminal I/O must not monopolize host admission bookkeeping"
        if acquired:
            host._turn_futures_lock.release()
    finally:
        release.set()
        worker.join(3)
        host.close()
    assert not worker.is_alive()


def test_executor_refusal_does_not_leave_phantom_owner(monkeypatch):
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    def fail(*args, **kwargs):
        raise RuntimeError("executor unavailable")
    monkeypatch.setattr(host._executor, "submit", fail)
    try:
        with pytest.raises(RuntimeError, match="executor unavailable"):
            host._handle_turn_start({"sid": "s", "request_id": "A"})
        assert not host._active_request_ids
    finally:
        host.close()


def test_terminal_publish_failure_cannot_clear_replacement_owner(monkeypatch):
    state = {"history_lock": threading.Lock(), "history": [], "session_key": "stored",
             "agent": types.SimpleNamespace(session_id="stored"), "running": False}
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    monkeypatch.setattr(server, "_sessions", {"s": state})
    monkeypatch.setattr(host, "_ensure_server_session", lambda *a: state)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *a: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda *a: None)
    monkeypatch.setattr(server, "_start_inflight_turn", lambda *a: None)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_session_info", lambda *a: {})
    cleared = []
    monkeypatch.setattr(server, "_clear_inflight_turn", cleared.append)

    def emit(frame):
        if frame["type"] == "turn.end":
            # The old computation released admission; a new one owns the SID
            # when publication of the old terminal fails.
            host._active_request_ids["s"] = "B"
            state["running"] = True
            raise OSError("terminal output failed")

    monkeypatch.setattr(host, "emit", emit)
    try:
        host._active_request_ids["s"] = "A"
        host._run_real_turn({"sid": "s", "request_id": "A", "text": "old"})
        assert state["running"] is True
        assert host._active_request_ids["s"] == "B"
        assert cleared == []
    finally:
        host.close()
