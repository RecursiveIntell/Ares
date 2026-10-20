"""Worker settlement, admission and recovery after a host-side failure."""
import io
import json
import threading
from types import SimpleNamespace

import pytest

from tui_gateway import server
from tui_gateway.compute_host import ComputeHost


@pytest.fixture
def runtime(monkeypatch):
    session = {
        "agent": SimpleNamespace(session_id="stored", model="fixture", provider="fixture"),
        "session_key": "stored", "history": [], "history_version": 0,
        "history_lock": threading.Lock(), "running": False,
        "_turn_cancel_requested": False,
    }
    monkeypatch.setattr(server, "_sessions", {"s": session})
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda state: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda state: None)
    monkeypatch.setattr(server, "_begin_turn_outcome", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_session_info", lambda *a, **kw: {"running": session["running"]})
    cleared = []

    def start_inflight(state, text):
        state["inflight_turn"] = {"text": text, "started_at": 1}

    def clear_inflight(state):
        cleared.append(state)
        state.pop("inflight_turn", None)

    monkeypatch.setattr(server, "_start_inflight_turn", start_inflight)
    monkeypatch.setattr(server, "_clear_inflight_turn", clear_inflight)
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    monkeypatch.setattr(host, "_ensure_server_session", lambda *a: session)
    yield host, session, output, cleared
    host.close()


def frames(output):
    return [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]


def test_error_after_dispatch_keeps_busy_and_stop_until_worker_settles(runtime, monkeypatch):
    host, session, output, cleared = runtime
    boundary = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    workers = []
    original_emit = host.emit

    def emit(frame):
        original_emit(frame)
        if frame["type"] == "turn.error":
            boundary.set()

    monkeypatch.setattr(host, "emit", emit)

    class Worker(threading.Thread):
        def join(self, *args, **kwargs):
            boundary.set()
            return super().join(*args, **kwargs)

    def submit(rid, sid, state, text, **kw):
        def work():
            assert release.wait(10)
            with state["history_lock"]:
                state["history_version"] += 1
                state["running"] = False
            exited.set()
        worker = Worker(target=work)
        workers.append(worker)
        with server._sessions_lock:
            state["_run_thread"] = worker
            worker.start()
        raise RuntimeError("submit acknowledgement failed after dispatch")

    monkeypatch.setattr(server, "_run_prompt_submit", submit)

    def interrupt(sid, state, **kw):
        assert host._active_request_ids[sid] == "owner"
        state["_turn_cancel_requested"] = True
        release.set()

    monkeypatch.setattr(server, "_interrupt_session_turn", interrupt)
    try:
        host._handle_turn_start({"sid": "s", "request_id": "owner", "text": "work"})
        assert boundary.wait(5), "host did not reach join or error publication"
        assert host._active_request_ids.get("s") == "owner"
        assert session["running"] is True
        assert not cleared
        assert not any(f["type"] in {"turn.end", "turn.error"} for f in frames(output))
        host._handle_turn_start({"sid": "s", "request_id": "overlap", "text": "must not start"})
        busy = [f for f in frames(output) if f.get("request_id") == "overlap"]
        assert len(busy) == 1 and busy[0]["reason"] == "admission_refused_busy"
        host._handle_interrupt({"sid": "s", "request_id": "stop", "target_request_id": "owner"})
        assert exited.wait(5)
    finally:
        release.set()
        with host._turn_futures_lock:
            futures = list(host._turn_futures)
        for future in futures:
            future.result(timeout=5)
        for worker in workers:
            worker.join(5)
    terminals = [f for f in frames(output) if f.get("request_id") == "owner" and f["type"] in {"turn.end", "turn.error"}]
    assert len(terminals) == 1
    assert terminals[0]["type"] == "turn.error"
    assert terminals[0]["message"] == "submit acknowledgement failed after dispatch"
    assert any(f["type"] == "interrupt.ack" and f["applied"] is True for f in frames(output))
    assert session["running"] is False
    assert session["inflight_turn"]["text"] == "work"
    assert not cleared
    assert "s" not in host._active_request_ids

    def healthy(rid, sid, state, text, **kw):
        worker = threading.Thread(target=lambda: state.update(running=False))
        with server._sessions_lock:
            state["_run_thread"] = worker
            worker.start()
        workers.append(worker)

    monkeypatch.setattr(server, "_run_prompt_submit", healthy)
    host._handle_turn_start({"sid": "s", "request_id": "next", "text": "fresh"})
    with host._turn_futures_lock:
        futures = list(host._turn_futures)
    for future in futures:
        future.result(timeout=5)
    for worker in workers:
        worker.join(5)
    assert [f["type"] for f in frames(output) if f.get("request_id") == "next"] == ["turn.started", "turn.end"]


@pytest.mark.parametrize("known_host_owner", [True, False])
def test_pre_admission_error_cannot_clear_another_live_turn(runtime, monkeypatch, known_host_owner):
    host, session, output, cleared = runtime
    session.update(running=True, _host_turn_request_id="other", inflight_turn={"text": "other work"})
    if known_host_owner:
        host._active_request_ids["s"] = "other"

    def refuse(_state):
        raise OSError("durable row unavailable")

    monkeypatch.setattr(server, "_ensure_session_db_row", refuse)
    host._run_real_turn({"sid": "s", "request_id": "rejected", "text": "not admitted"})
    assert session["running"] is True
    assert session["_host_turn_request_id"] == "other"
    assert session["inflight_turn"] == {"text": "other work"}
    assert host._active_request_ids.get("s") == ("other" if known_host_owner else None)
    assert not cleared
    assert [f["type"] for f in frames(output)] == ["turn.error"]
    host._active_request_ids.clear()


def test_error_on_retired_session_cannot_clear_replacement(runtime, monkeypatch):
    host, session, output, cleared = runtime
    replacement = {
        "agent": SimpleNamespace(session_id="replacement"), "session_key": "replacement",
        "history_lock": threading.Lock(), "running": True,
        "_host_turn_request_id": "replacement-owner", "inflight_turn": {"text": "replacement work"},
    }

    def replaced(*args, **kwargs):
        server._sessions["s"] = replacement
        raise RuntimeError("retired after admission")

    monkeypatch.setattr(server, "_run_prompt_submit", replaced)
    host._run_real_turn({"sid": "s", "request_id": "retired", "text": "old work"})
    assert replacement["running"] is True
    assert replacement["inflight_turn"] == {"text": "replacement work"}
    assert replacement["_host_turn_request_id"] == "replacement-owner"
    assert not cleared
    assert [f["type"] for f in frames(output)] == ["turn.started", "turn.error"]


def test_stop_does_not_redirect_to_replacement_during_error_settlement(runtime, monkeypatch):
    host, session, output, cleared = runtime
    joined = threading.Event()
    release = threading.Event()
    calls = []
    replacement = {"agent": SimpleNamespace(session_id="B"), "session_key": "B",
                   "history_lock": threading.Lock(), "running": True,
                   "_host_turn_request_id": "B", "inflight_turn": {"text": "B"}}

    class Worker(threading.Thread):
        def join(self, *a, **kw):
            joined.set()
            return super().join(*a, **kw)

    def submit(*args, **kw):
        def work():
            assert release.wait(10)
            session["running"] = False
        worker = Worker(target=work)
        with server._sessions_lock:
            session["_run_thread"] = worker
            worker.start()
        server._sessions["s"] = replacement
        raise RuntimeError("detached after dispatch")

    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    monkeypatch.setattr(server, "_interrupt_session_turn", lambda sid, state, **kw: calls.append(state))
    try:
        host._handle_turn_start({"sid": "s", "request_id": "A", "text": "work"})
        assert joined.wait(5)
        host._handle_interrupt({"sid": "s", "request_id": "stop", "target_request_id": "A"})
        ack = [f for f in frames(output) if f["type"] == "interrupt.ack"][-1]
        assert ack["applied"] is False
        assert ack["failure_code"] == "turn_owner_changed"
        assert not calls
    finally:
        release.set()
        with host._turn_futures_lock:
            futures = list(host._turn_futures)
        for future in futures:
            future.result(timeout=5)
        worker = session.get("_run_thread")
        if worker is not None:
            worker.join(5)
    assert replacement["running"] is True
    assert replacement["inflight_turn"] == {"text": "B"}


def test_terminal_cannot_retire_replacement_with_reused_request_id(runtime, monkeypatch):
    host, session, output, cleared = runtime
    replacement = {"agent": SimpleNamespace(session_id="B"), "session_key": "B",
                   "history_lock": threading.Lock(), "running": True,
                   "_host_turn_request_id": "same", "inflight_turn": {"text": "B"}}

    def submit(*args, **kw):
        session["running"] = False
        server._sessions["s"] = replacement

    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    host._run_real_turn({"sid": "s", "request_id": "same", "text": "old"})
    assert replacement["_host_turn_request_id"] == "same"
    assert replacement["running"] is True
    assert replacement["inflight_turn"] == {"text": "B"}


def test_terminal_output_failure_is_not_reissued_as_execution_error(runtime, monkeypatch):
    host, session, output, cleared = runtime
    attempts = []

    def submit(*args, **kw):
        session["running"] = False

    def emit(frame):
        if frame["type"] in {"turn.end", "turn.error"}:
            attempts.append(frame["type"])
            raise OSError("terminal output unavailable")

    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    monkeypatch.setattr(host, "emit", emit)
    host._active_request_ids["s"] = "A"
    host._run_real_turn({"sid": "s", "request_id": "A", "text": "work"})
    assert attempts == ["turn.end"]
    assert "s" not in host._active_request_ids


def test_unadmitted_same_id_error_cannot_release_current_admission(runtime, monkeypatch):
    host, session, output, cleared = runtime
    session.update(running=True, _host_turn_request_id="same", inflight_turn={"text": "live"})
    host._active_request_ids["s"] = "same"
    host._admitted_request_sessions["s"] = ("same", session)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _: (_ for _ in ()).throw(OSError("storage unavailable")))
    host._run_real_turn({"sid": "s", "request_id": "same", "text": "not admitted"})
    assert session["running"] is True
    assert session["_host_turn_request_id"] == "same"
    assert host._active_request_ids["s"] == "same"
    assert host._admitted_request_sessions["s"][1] is session
    assert not cleared
    host._active_request_ids.clear()
    host._admitted_request_sessions.clear()


def test_unknown_worker_settlement_retains_admission_without_terminal(runtime, monkeypatch):
    host, session, output, cleared = runtime
    class UnknownWorker:
        _turn_outcome_execution = (session, "s", "different-owner")
        def join(self):
            pytest.fail("must not join another execution's worker")
    def submit(*args, **kw):
        session["_run_thread"] = UnknownWorker()
        raise RuntimeError("dispatch acknowledgement lost")
    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    host._active_request_ids["s"] = "A"
    host._run_real_turn({"sid": "s", "request_id": "A", "text": "work"})
    assert session["running"] is True
    assert host._active_request_ids["s"] == "A"
    assert session["inflight_turn"]["text"] == "work"
    assert not cleared
    assert [f["type"] for f in frames(output)] == ["turn.started"]
    host._active_request_ids.clear()
    host._admitted_request_sessions.clear()


def test_native_start_failure_after_completed_turn_releases_only_failed_admission(runtime, monkeypatch):
    host, session, output, cleared = runtime
    workers = []
    hook = "_start_joinable_thread" if hasattr(threading, "_start_joinable_thread") else "_start_new_thread"

    def refuse_native(*args, **kwargs):
        raise RuntimeError("native thread creation refused")

    def submit(rid, sid, state, text, **kwargs):
        worker = threading.Thread(target=lambda: state.update(running=False))
        worker._turn_outcome_execution = (state, sid, rid)
        workers.append(worker)
        if rid == "B":
            with monkeypatch.context() as fault:
                fault.setattr(threading, hook, refuse_native)
                server._start_run_thread(state, worker)
        else:
            server._start_run_thread(state, worker)

    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    try:
        for rid in ["A", "B", "C"]:
            host._active_request_ids["s"] = rid
            host._run_real_turn({"sid": "s", "request_id": rid, "text": rid})
            if rid == "B":
                assert session["inflight_turn"]["text"] == "B"
                assert session["_run_thread"] is workers[0]
                assert workers[1].ident is None
            assert "s" not in host._active_request_ids
            assert "s" not in host._admitted_request_sessions
            assert "_host_turn_request_id" not in session
            assert session["running"] is False
    finally:
        for worker in workers:
            if worker.ident is not None:
                worker.join(5)
        host._active_request_ids.clear()
        host._admitted_request_sessions.clear()
    assert [(f["request_id"], f["type"]) for f in frames(output) if f["type"] in {"turn.end", "turn.error"}] == [
        ("A", "turn.end"), ("B", "turn.error"), ("C", "turn.end")]
