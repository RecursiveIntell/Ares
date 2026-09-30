"""Accepted host turns retain ownership through goal follow-up threads."""
import io
import json
import threading
import types

from tui_gateway import server
from tui_gateway.compute_host import ComputeHost


def _json_lines(output):
    return [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]


def test_real_host_turn_waits_for_a_chained_successor_before_terminal(monkeypatch):
    """A goal successor belongs to the accepted host turn, not a new idle gap."""
    session = {
        "agent": types.SimpleNamespace(session_id="goal-stored"),
        "session_key": "goal-stored",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
    }
    monkeypatch.setattr(server, "_sessions", {"goal-runtime": session})
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _session: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda _session: None)
    monkeypatch.setattr(server, "_start_inflight_turn", lambda *_args: None)
    monkeypatch.setattr(server, "_session_info", lambda _agent, state: {"running": state["running"]})

    joined_predecessor = threading.Event()
    allow_successor = threading.Event()
    successor_started = threading.Event()
    release_successor = threading.Event()
    host_done = threading.Event()
    threads = []

    class Predecessor(threading.Thread):
        def join(self, *args, **kwargs):
            joined_predecessor.set()
            return super().join(*args, **kwargs)

    def submit(_rid, _sid, state, _text, **_kwargs):
        def successor():
            successor_started.set()
            release_successor.wait(timeout=5)
            with state["history_lock"]:
                state["running"] = False

        def predecessor():
            allow_successor.wait(timeout=5)
            next_thread = threading.Thread(target=successor)
            with state["history_lock"]:
                state["running"] = True
                state["_run_thread"] = next_thread
            next_thread.start()
            threads.append(next_thread)

        first = Predecessor(target=predecessor)
        state["_run_thread"] = first
        first.start()
        threads.append(first)

    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    monkeypatch.setattr(host, "_ensure_server_session", lambda _server, _frame: session)
    runner = threading.Thread(target=lambda: (host._run_real_turn({"sid": "goal-runtime", "request_id": "turn-1", "text": "goal"}), host_done.set()))
    runner.start()
    try:
        assert joined_predecessor.wait(timeout=2)
        allow_successor.set()
        assert successor_started.wait(timeout=2)
        assert not host_done.wait(timeout=0.1), "host must not mark the accepted chain terminal while successor runs"
    finally:
        release_successor.set()
        runner.join(timeout=3)
        for thread in threads:
            thread.join(timeout=3)
        host.close()
    assert not runner.is_alive()
    assert [frame["type"] for frame in _json_lines(output)].count("turn.end") == 1


def test_terminal_uses_final_chain_state_and_original_receipt(monkeypatch):
    session = {"agent": types.SimpleNamespace(session_id="stored"),
               "session_key": "stored", "history": [], "history_version": 0,
               "history_lock": threading.Lock(), "running": False}
    monkeypatch.setattr(server, "_sessions", {"runtime": session})
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda _: None)
    monkeypatch.setattr(server, "_start_inflight_turn", lambda *a: None)
    monkeypatch.setattr(server, "_session_info", lambda *a: {"running": False})
    joins = []
    submitted = []

    class Successor:
        def __init__(self, index):
            self.index = index

        def join(self):
            joins.append(self.index)
            if self.index < 3:
                session["_run_thread"] = Successor(self.index + 1)
            else:
                session["running"] = False
                session["_turn_cancel_requested"] = True
                session["history_version"] = 3

    def submit(rid, sid, state, text, **kwargs):
        submitted.append((rid, sid, kwargs))
        state["_run_thread"] = Successor(1)

    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    monkeypatch.setattr(host, "_ensure_server_session", lambda *a: session)
    try:
        host._run_real_turn({"sid": "runtime", "request_id": "host-owner",
                             "text": "goal", "context_input_event_id": "receipt-owner"})
    finally:
        host.close()
    assert joins == [1, 2, 3]
    assert submitted == [("host-owner", "runtime", {"display_kind": None,
                                                   "context_input_event_id": "receipt-owner"})]
    terminals = [f for f in _json_lines(output) if f["type"] in {"turn.end", "turn.error"}]
    assert len(terminals) == 1
    assert terminals[0]["type"] == "turn.end"
    assert terminals[0]["request_id"] == "host-owner"
    assert terminals[0]["interrupted"] is True
    assert terminals[0]["history_version"] == 3
