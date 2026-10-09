"""Cold-resume admission through real hydration, dispatch and temporary SQLite."""
import io
import threading
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tests.test_tui_gateway_server import _configure_immediate_prompt_run, _session
from tests.tui_gateway.test_model_intent_admission_order import (  # noqa: F401
    REAL_THREAD, agent, assert_route, gateway, intent_turn, live_turn, routes, select, turn_env,
)
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tui_gateway.turn_marker import read_turn_marker, record_turn_start

REAL_EMIT = server._emit
REAL_DRAIN = server._drain_queued_prompt
REAL_TERMINAL_ERROR = server._emit_terminal_turn_error
REAL_WAIT = server._wait_agent_for_prompt
REAL_RETIRE = server._retire_turn_marker


class HistoryGate(threading.Event):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()

    def wait(self, timeout=None):
        self.entered.set()
        return super().wait(timeout)


class Transport:
    def __init__(self):
        self.frames = []
        self.terminal = threading.Event()

    def write(self, obj):
        self.frames.append(obj)
        if obj.get("params", {}).get("type") == "message.complete":
            self.terminal.set()
        return True


@pytest.fixture
def resume(monkeypatch, tmp_path):
    _configure_immediate_prompt_run(monkeypatch, tmp_path, immediate_threads=False)
    monkeypatch.setattr(server, "_emit", REAL_EMIT)
    monkeypatch.setattr(server, "_sync_bot_capabilities", lambda *a: None)
    monkeypatch.setattr(server, "_sync_agent_compression_with_config", lambda *a: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a: None)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: False)
    monkeypatch.setattr(server, "_voice_mode_enabled", lambda: False)
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *a: None)
    monkeypatch.setattr(server, "_start_usage_ticker", lambda *a: (threading.Event(), SimpleNamespace(join=lambda *a, **kw: None)))
    monkeypatch.setattr(server, "record_turn_start", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_retire_turn_marker", lambda *a: None)
    monkeypatch.setattr(server, "_AGENT_BUILD_WAIT_SLICE", 0.01)
    monkeypatch.setattr(server, "_agent_build_wait_cap", lambda: 5.0)
    db = SessionDB(tmp_path / "history.db")
    db.create_session("durable", source="tui")
    db.append_message("durable", role="user", content="prior durable question")
    db.append_message("durable", role="assistant", content="prior durable answer")
    monkeypatch.setattr(server, "_db", db)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    calls = []
    provider_entered = threading.Event()
    provider_release = threading.Event()
    provider_release.set()

    def conversation(prompt, conversation_history=None, **kwargs):
        calls.append((prompt, list(conversation_history or [])))
        provider_entered.set()
        assert provider_release.wait(5)
        return {"final_response": "offline", "messages": [], "completed": False, "api_calls": 0}

    agent = SimpleNamespace(model="inert-model", provider="inert-provider", session_id="durable",
        _session_db=db, clear_interrupt=lambda: None, interrupt=lambda: None,
        run_conversation=conversation)
    ready = threading.Event()
    ready.set()
    gate = HistoryGate()
    transport = Transport()
    session = _session(agent=agent, session_key="durable", agent_ready=ready,
        resume_history_ready=gate, resume_hydrating=True, transport=transport)
    monkeypatch.setattr(server, "_sessions", {"runtime": session})
    read_entered, read_release = threading.Event(), threading.Event()
    original_read = db.get_resume_conversations
    error = []
    threads = []

    def read(target):
        read_entered.set()
        assert read_release.wait(5)
        if error:
            raise RuntimeError(error[0])
        return original_read(target)

    monkeypatch.setattr(db, "get_resume_conversations", read)
    state = SimpleNamespace(db=db, session=session, gate=gate, transport=transport, calls=calls,
        provider_entered=provider_entered, provider_release=provider_release, read_entered=read_entered,
        read_release=read_release, error=error, threads=threads)
    yield state
    read_release.set()
    provider_release.set()
    for thread in [*threads, session.get("_run_thread")]:
        if thread is not None and thread is not threading.current_thread():
            thread.join(5)
            assert not thread.is_alive()
    assert not read_entered.is_set() or gate.wait(5)
    db.close()


def hydrate(state):
    server._schedule_resume_hydration("runtime", "durable", state.db)
    assert state.read_entered.wait(5)


def launch(state, route):
    if route == "inline":
        response = server.dispatch({"id": "send", "method": "prompt.submit", "params": {
            "session_id": "runtime", "text": "new question"}}, state.transport)
        assert response["result"]["status"] == "streaming", response
        if not response["result"].get("turn_isolation"):
            state.threads.append(state.session["_run_thread"])
        return response
    state.session["running"] = route != "queue"
    if route == "queue":
        state.session["queued_prompt"] = {"text": "new question"}
        target = lambda: REAL_DRAIN("queue", "runtime", state.session)
    else:
        target = lambda: server._run_prompt_submit("direct", "runtime", state.session,
            "new question", display_kind="auto_continue" if route == "continuation" else None)
    thread = threading.Thread(target=target)
    state.threads.append(thread)
    thread.start()
    return None


@pytest.mark.parametrize("route", ["inline", "direct", "queue", "continuation"])
def test_delayed_real_sqlite_hydration_precedes_provider(resume, route):
    hydrate(resume)
    launch(resume, route)
    assert resume.gate.entered.wait(2), f"history gate bypassed: {resume.calls!r}"
    assert not resume.provider_entered.is_set()
    resume.read_release.set()
    assert resume.provider_entered.wait(5)
    assert [row["content"] for row in resume.calls[0][1]] == [
        "prior durable question", "prior durable answer"]


@pytest.mark.parametrize("route", ["inline", "direct", "queue", "continuation"])
def test_failed_hydration_delivers_correlated_terminal_to_actual_transport(resume, route):
    resume.error.append("injected SQLite read failure")
    hydrate(resume)
    response = launch(resume, route)
    assert resume.gate.entered.wait(2), f"history gate bypassed: {resume.calls!r}"
    resume.read_release.set()
    assert resume.transport.terminal.wait(5), resume.transport.frames
    terminal = next(x["params"]["payload"] for x in resume.transport.frames
        if x.get("params", {}).get("type") == "message.complete")
    assert terminal["status"] == "error"
    assert "injected SQLite read failure" in terminal["error"]
    if response is not None:
        assert terminal["accepted_turn"] == response["result"]["accepted_turn"]
    else:
        assert "accepted_turn" not in terminal
    assert resume.calls == []


@pytest.mark.parametrize("outcome", ["success", "cancel", "deadline"])
def test_unused_once_keeps_original_restore_through_history_wait(intent_turn, monkeypatch, outcome):
    session, _ = intent_turn
    monkeypatch.setattr(server.threading, "Thread", REAL_THREAD)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a: None)
    monkeypatch.setattr(server, "_AGENT_BUILD_WAIT_SLICE", 0.01)
    monkeypatch.setattr(server, "_agent_build_wait_cap", lambda: 0.05 if outcome == "deadline" else 5.0)
    value = session["agent"]
    assert not server.handle_request(select("model-b", "endpoint-b", once=True)).get("error")
    assert not server.handle_request(select("model-c", "endpoint-c", once=True)).get("error")
    original = session["one_turn_model_restore"]
    assert original["model"] == "model-a"
    db = value._session_db
    db.append_message(session["session_key"], role="user", content="prior durable question")
    db.append_message(session["session_key"], role="assistant", content="prior durable answer")
    read_entered, release_read = threading.Event(), threading.Event()
    original_read = db.get_resume_conversations
    def read(target):
        read_entered.set()
        assert release_read.wait(5)
        return original_read(target)
    monkeypatch.setattr(db, "get_resume_conversations", read)
    gate = HistoryGate()
    session.update(resume_history_ready=gate, resume_hydrating=True, running=True)
    calls = []
    def conversation(prompt, **kwargs):
        calls.append((value.model, value.provider))
        return {"final_response": "offline", "completed": False, "api_calls": 0}
    monkeypatch.setattr(value, "run_conversation", conversation)
    server._schedule_resume_hydration("selected", session["session_key"], db)
    assert read_entered.wait(5)
    waiter = REAL_THREAD(target=lambda: server._run_prompt_submit("once", "selected", session, "new question"))
    waiter.start()
    try:
        assert gate.entered.wait(2)
        assert calls == [] and session["one_turn_model_restore"] is original
        if outcome == "cancel":
            with session["history_lock"]:
                session["_turn_cancel_requested"] = True
            waiter.join(5)
        elif outcome == "deadline":
            waiter.join(5)
        else:
            release_read.set()
            waiter.join(5)
            worker = session.get("_run_thread")
            if worker is not None:
                worker.join(5)
                assert not worker.is_alive()
        assert not waiter.is_alive()
        if outcome == "success":
            assert calls == [("model-c", "endpoint-c")]
            assert_route(value, "http://a.invalid/v1", "synthetic-key-a")
            assert "one_turn_model_restore" not in session
        else:
            assert calls == []
            assert session["one_turn_model_restore"] is original
            assert_route(value, "http://c.invalid/v1", "synthetic-key-c")
    finally:
        release_read.set()
        waiter.join(5)
        assert gate.wait(5)


def test_fresh_executing_host_uses_actual_owner_db_context(resume, monkeypatch):
    monkeypatch.setattr(server, "_make_agent", lambda *a, **kw: resume.session["agent"])
    monkeypatch.setattr(server, "_persist_session_cwd_and_schedule_git_meta", lambda *a, **kw: None)
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0, max_workers=1)
    try:
        record = host._ensure_server_session(server, {"sid": "executing-host", "session_key": "durable",
            "history": [{"role": "user", "content": "stale parent mirror"}], "cwd": ".", "source": "desktop"})
        assert "resume_history_ready" not in record
        record["running"] = True
        assert server._run_prompt_submit("host", "executing-host", record, "host input")
        record["_run_thread"].join(5)
        assert not record["_run_thread"].is_alive()
        assert [row["content"] for row in resume.calls[0][1]] == [
            "prior durable question", "prior durable answer"]
    finally:
        host.close()


def test_adopted_parent_history_error_does_not_gate_host_send_or_stop(resume, monkeypatch):
    submitted, stopped = [], []
    class Host:
        boot_id = "offline-host"
        def submit_turn(self, frame, **kwargs):
            frame["_admitted_host_boot_id"] = self.boot_id
            submitted.append(frame)
        def interrupt(self, sid, **kwargs):
            stopped.append((sid, kwargs))
            return {"type": "interrupt.ack", "sid": sid, "request_id": kwargs["request_id"],
                "target_request_id": kwargs["target_request_id"], "applied": True}
    host = Host()
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: host)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: True)
    resume.session.update(agent=None, _compute_host_active=True, resume_history_error="display mirror unavailable")
    response = launch(resume, "inline")
    assert response["result"]["turn_isolation"] is True
    stop = server.dispatch({"id": "stop", "method": "session.interrupt", "params": {
        "session_id": "runtime"}}, resume.transport)
    assert "error" not in stop, stop
    assert submitted[0]["request_id"] == response["result"]["accepted_turn"]["request_id"]
    assert stopped[0][1]["target_request_id"] == submitted[0]["request_id"]
    assert not resume.gate.entered.is_set()
    assert resume.session["agent"] is None
    assert resume.calls == []


@pytest.mark.parametrize("action", ["cancel", "replace", "queue_generation"])
def test_obsolete_history_waiter_cannot_run_or_clear_successor(resume, action):
    hydrate(resume)
    resume.session["running"] = True
    generation = 0 if action == "queue_generation" else None
    thread = threading.Thread(target=lambda: server._run_prompt_submit("old", "runtime",
        resume.session, "obsolete input", queued_prompt_generation=generation))
    resume.threads.append(thread)
    thread.start()
    assert resume.gate.entered.wait(2), f"history gate bypassed: {resume.calls!r}"
    with resume.session["history_lock"]:
        if action == "cancel":
            resume.session["_turn_cancel_requested"] = True
        elif action == "replace":
            server._sessions["runtime"] = _session(running=True, inflight_turn={"user": "successor"})
        else:
            resume.session["_queued_prompt_generation"] = 1
            server._start_inflight_turn(resume.session, "successor")
    resume.read_release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert resume.calls == []
    if action != "cancel":
        assert server._sessions["runtime"]["running"] is True
        assert server._sessions["runtime"]["inflight_turn"]["user"] == "successor"


def test_history_only_wait_has_finite_cap(resume, monkeypatch):
    hydrate(resume)
    resume.session["running"] = True
    monkeypatch.setattr(server, "_agent_build_wait_cap", lambda: 0.03)
    result = server._wait_agent_for_prompt(resume.session, "bounded", "runtime")
    assert result["error"]["code"] == 5032
    assert "was not sent" in result["error"]["message"]
    assert not resume.gate.is_set()
    assert resume.calls == []


def test_actual_stop_then_new_send_keeps_successor_admission(resume):
    hydrate(resume)
    resume.session["running"] = True
    old = threading.Thread(target=lambda: server._run_prompt_submit("old", "runtime",
        resume.session, "obsolete input", queued_prompt_generation=0))
    resume.threads.append(old)
    old.start()
    assert resume.gate.entered.wait(2)
    stop = server.dispatch({"id": "stop", "method": "session.interrupt", "params": {
        "session_id": "runtime"}}, resume.transport)
    assert "error" not in stop, stop
    assert not resume.session["running"]
    resume.provider_release.clear()
    response = launch(resume, "inline")
    successor = resume.session["inflight_turn"]
    resume.read_release.set()
    assert resume.provider_entered.wait(5)
    old.join(5)
    assert not old.is_alive()
    assert [prompt for prompt, _ in resume.calls] == ["new question"]
    assert resume.session["running"]
    assert resume.session["inflight_turn"]["started_at"] is successor["started_at"]
    assert resume.session["_turn_outcomes"].turns[-1]["accepted_turn"] == response["result"]["accepted_turn"]


def test_actual_inline_stop_during_hydration_does_not_replay_input(resume):
    hydrate(resume)
    launch(resume, "inline")
    assert resume.gate.entered.wait(2)
    stop = server.dispatch({"id": "stop", "method": "session.interrupt", "params": {
        "session_id": "runtime"}}, resume.transport)
    assert "error" not in stop, stop
    resume.threads[0].join(5)
    assert not resume.threads[0].is_alive()
    assert not resume.session["running"]
    assert any(frame["params"].get("payload", {}).get("message") == "Turn cancelled before the agent was ready"
        for frame in resume.transport.frames if frame.get("method") == "event")
    resume.read_release.set()
    assert resume.gate.wait(5)
    assert resume.calls == []


def test_direct_history_deadline_delivers_terminal_without_spending_model_choice(resume, monkeypatch):
    hydrate(resume)
    pending = {"raw": "unused --session"}
    once = {"model": "original"}
    resume.session.update(pending_model_switch=pending, one_turn_model_restore=once)
    monkeypatch.setattr(server, "_agent_build_wait_cap", lambda: 0.03)
    launch(resume, "direct")
    assert resume.transport.terminal.wait(5)
    terminal = next(frame["params"]["payload"] for frame in resume.transport.frames
        if frame.get("params", {}).get("type") == "message.complete")
    assert terminal["status"] == "error" and "was not sent" in terminal["error"]
    assert resume.session["inflight_turn"]["user"] == "new question"
    assert resume.session["pending_model_switch"] is pending
    assert resume.session["one_turn_model_restore"] is once
    assert resume.calls == []


def test_old_history_error_cannot_fail_new_send_after_stop(resume, monkeypatch, tmp_path):
    resume.session["profile_home"] = tmp_path / "markers"
    monkeypatch.setattr(server, "_retire_turn_marker", REAL_RETIRE)
    hydrate(resume)
    entered_error, release_error = threading.Event(), threading.Event()
    def paused_error(*args, **kwargs):
        entered_error.set()
        assert release_error.wait(5)
        return REAL_TERMINAL_ERROR(*args, **kwargs)
    monkeypatch.setattr(server, "_emit_terminal_turn_error", paused_error)
    monkeypatch.setattr(server, "_agent_build_wait_cap", lambda: 0.03)
    launch(resume, "direct")
    try:
        assert entered_error.wait(5)
        stop = server.dispatch({"id": "stop", "method": "session.interrupt", "params": {
            "session_id": "runtime"}}, resume.transport)
        assert "error" not in stop, stop
        monkeypatch.setattr(server, "_agent_build_wait_cap", lambda: 5.0)
        resume.provider_release.clear()
        response = launch(resume, "inline")
        successor = resume.session["inflight_turn"]
        resume.read_release.set()
        assert resume.provider_entered.wait(5)
        marker = ("successor-provider", "successor-model")
        resume.session["model_verified_for"] = marker
        record_turn_start(resume.session["profile_home"], "durable", "successor")
        release_error.set()
        resume.threads[0].join(5)
        assert not resume.threads[0].is_alive()
        assert resume.session["inflight_turn"]["started_at"] is successor["started_at"]
        assert resume.session["inflight_turn"].get("status") != "error"
        assert resume.session["model_verified_for"] == marker
        assert resume.session["running"]
        assert read_turn_marker(resume.session["profile_home"], "durable")["prompt"] == "successor"
        assert resume.session["_turn_outcomes"].turns[-1]["accepted_turn"] == response["result"]["accepted_turn"]
    finally:
        release_error.set()


def test_waiter_starting_after_failed_owner_removal_rejects_replacement(resume):
    removed = threading.Event()
    resume.session["active_session_lease"] = SimpleNamespace(release=removed.set)
    resume.session["running"] = True
    resume.error.append("injected SQLite read failure")
    hydrate(resume)
    resume.read_release.set()
    assert removed.wait(5)
    replacement = _session(running=True, inflight_turn={"user": "successor"})
    server._sessions["runtime"] = replacement
    assert server._wait_agent_for_prompt(resume.session, "obsolete", "runtime") is None
    assert server._sessions["runtime"] is replacement
    assert replacement["running"] and replacement["inflight_turn"] == {"user": "successor"}


@pytest.mark.parametrize("route", ["inline", "direct", "queue", "continuation"])
def test_completed_failed_owner_removal_still_delivers_admitted_error(resume, monkeypatch, route):
    removed = threading.Event()
    resume.session["active_session_lease"] = SimpleNamespace(release=removed.set)
    def wait_after_removal(*args):
        error = REAL_WAIT(*args)
        assert error is not None
        assert removed.wait(5)
        return error
    monkeypatch.setattr(server, "_wait_agent_for_prompt", wait_after_removal)
    resume.error.append("injected SQLite read failure")
    hydrate(resume)
    response = launch(resume, route)
    assert resume.gate.entered.wait(2)
    resume.read_release.set()
    assert resume.transport.terminal.wait(5)
    assert removed.wait(5) and server._sessions.get("runtime") is None
    terminal = next(frame["params"]["payload"] for frame in resume.transport.frames
        if frame.get("params", {}).get("type") == "message.complete")
    assert terminal["status"] == "error" and "injected SQLite read failure" in terminal["error"]
    if response is not None:
        assert terminal["accepted_turn"] == response["result"]["accepted_turn"]
    else:
        assert "accepted_turn" not in terminal
    assert resume.calls == []


def test_inline_cancel_does_not_emit_to_replacement_transport(resume, monkeypatch):
    cancelled, release_waiter = threading.Event(), threading.Event()
    def paused_wait(*args):
        result = REAL_WAIT(*args)
        cancelled.set()
        assert release_waiter.wait(5)
        return result
    monkeypatch.setattr(server, "_wait_agent_for_prompt", paused_wait)
    hydrate(resume)
    launch(resume, "inline")
    assert resume.gate.entered.wait(2)
    try:
        response = server.dispatch({"id": "stop", "method": "session.interrupt", "params": {
            "session_id": "runtime"}}, resume.transport)
        assert "error" not in response
        assert cancelled.wait(5)
        transport = Transport()
        replacement = _session(running=True, transport=transport, inflight_turn={"user": "successor"})
        server._sessions["runtime"] = replacement
        release_waiter.set()
        resume.threads[0].join(5)
        assert not resume.threads[0].is_alive()
        assert transport.frames == []
        assert replacement["running"] and replacement["inflight_turn"] == {"user": "successor"}
        assert resume.calls == []
    finally:
        release_waiter.set()


def test_inline_error_settlement_does_not_clear_later_send(resume, monkeypatch):
    settled, release_error = threading.Event(), threading.Event()
    def paused_after_settlement(*args, **kwargs):
        result = REAL_TERMINAL_ERROR(*args, **kwargs)
        settled.set()
        assert release_error.wait(5)
        return result
    monkeypatch.setattr(server, "_emit_terminal_turn_error", paused_after_settlement)
    monkeypatch.setattr(server, "_agent_build_wait_cap", lambda: 0.03)
    hydrate(resume)
    response = launch(resume, "inline")
    old_thread = resume.threads[0]
    try:
        assert settled.wait(5)
        assert not resume.session["running"]
        terminal = next(frame["params"]["payload"] for frame in resume.transport.frames
            if frame.get("params", {}).get("type") == "message.complete")
        assert terminal["accepted_turn"] == response["result"]["accepted_turn"]
        monkeypatch.setattr(server, "_agent_build_wait_cap", lambda: 5.0)
        resume.provider_release.clear()
        successor_ack = launch(resume, "inline")
        assert successor_ack["result"]["accepted_turn"] != response["result"]["accepted_turn"]
        successor = resume.session["inflight_turn"]
        resume.read_release.set()
        assert resume.provider_entered.wait(5)
        release_error.set()
        old_thread.join(5)
        assert not old_thread.is_alive()
        assert resume.session["running"]
        assert resume.session["inflight_turn"]["started_at"] is successor["started_at"]
        assert resume.session["inflight_turn"].get("status") != "error"
        assert resume.session["_turn_outcomes"].turns[-1]["accepted_turn"] == successor_ack["result"]["accepted_turn"]
        assert [text for text, _ in resume.calls] == ["new question"]
    finally:
        release_error.set()


@pytest.mark.parametrize("original_sink", ["transport", "stdio"])
def test_replaced_terminal_is_retained_without_publishing_to_either_sink(resume, monkeypatch, original_sink):
    ref = server._begin_turn_outcome(resume.session, "runtime", "old-admission", "inline")
    original = resume.transport
    if original_sink == "stdio":
        resume.session["transport"] = None
        monkeypatch.setattr(server, "_stdio_transport", original)
    replacement_transport = Transport()
    replacement = _session(running=True, transport=replacement_transport, inflight_turn={"user": "successor"})
    server._sessions["runtime"] = replacement
    successor_ref = server._begin_turn_outcome(replacement, "runtime", "successor-admission", "inline")
    execution_token = server._turn_outcome_execution.set((resume.session, "runtime", ref["request_id"]))
    transport_token = server.bind_transport(None)
    try:
        server._emit("message.complete", "runtime", {"text": "old terminal", "status": "error"})
    finally:
        server.reset_transport(transport_token)
        server._turn_outcome_execution.reset(execution_token)
    assert original.frames == []
    old_turn = resume.session["_turn_outcomes"].find(ref["request_id"])
    assert old_turn["accepted_turn"] == ref
    assert old_turn["finalized"] == [{"text": "old terminal", "status": "error"}]
    assert replacement_transport.frames == []
    assert replacement["_turn_outcomes"].turns[-1]["accepted_turn"] == successor_ref
    assert replacement["_turn_outcomes"].turns[-1]["finalized"] == []
    assert replacement["running"] and replacement["inflight_turn"] == {"user": "successor"}


def test_replacement_during_terminal_projection_cannot_capture_old_stdio_reply(resume, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    resume.session["transport"] = None
    monkeypatch.setattr(server, "_stdio_transport", resume.transport)
    ref = server._begin_turn_outcome(resume.session, "runtime", "old-admission", "inline")
    original_lock = resume.session["history_lock"]
    class LockGate:
        def __enter__(self):
            original_lock.acquire()
            entered.set()
            assert release.wait(5)
        def __exit__(self, *args):
            original_lock.release()
    resume.session["history_lock"] = LockGate()
    def emit():
        token = server._turn_outcome_execution.set((resume.session, "runtime", ref["request_id"]))
        transport_token = server.bind_transport(None)
        try:
            server._emit("message.complete", "runtime", {"text": "old terminal", "status": "error"})
        finally:
            server.reset_transport(transport_token)
            server._turn_outcome_execution.reset(token)
    thread = threading.Thread(target=emit)
    resume.threads.append(thread)
    thread.start()
    try:
        assert entered.wait(5)
        transport = Transport()
        replacement = _session(running=True, transport=transport, inflight_turn={"user": "successor"})
        server._sessions["runtime"] = replacement
        successor_ref = server._begin_turn_outcome(replacement, "runtime", "successor-admission", "inline")
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert transport.frames == []
        assert resume.transport.frames == []
        old_turn = resume.session["_turn_outcomes"].find(ref["request_id"])
        assert old_turn["accepted_turn"] == ref
        assert old_turn["finalized"] == [{"text": "old terminal", "status": "error"}]
        assert replacement["_turn_outcomes"].turns[-1]["accepted_turn"] == successor_ref
        assert replacement["_turn_outcomes"].turns[-1]["finalized"] == []
        assert replacement["running"] and replacement["inflight_turn"] == {"user": "successor"}
    finally:
        release.set()


@pytest.mark.parametrize("route", ["inline", "direct"])
def test_removed_history_owner_cannot_retire_new_runtime_marker(resume, monkeypatch, tmp_path, route):
    from hermes_cli.active_sessions import try_acquire_active_session

    home = tmp_path / "markers"
    resume.session["profile_home"] = home
    lease, error = try_acquire_active_session(session_id="durable", surface="tui",
        config={"max_concurrent_sessions": 1}, metadata={"live_session_id": "runtime"})
    assert lease is not None and error is None and lease.enabled
    removed, entered_error, release_error = threading.Event(), threading.Event(), threading.Event()
    real_release = lease.release
    def release_lease():
        real_release()
        removed.set()
    monkeypatch.setattr(lease, "release", release_lease)
    resume.session["active_session_lease"] = lease
    def paused_error(*args, **kwargs):
        assert removed.wait(5) and lease.released
        entered_error.set()
        assert release_error.wait(5)
        return REAL_TERMINAL_ERROR(*args, **kwargs)
    monkeypatch.setattr(server, "_emit_terminal_turn_error", paused_error)
    monkeypatch.setattr(server, "_retire_turn_marker", REAL_RETIRE)
    monkeypatch.setattr(server, "record_turn_start", record_turn_start)
    resume.error.append("injected SQLite read failure")
    hydrate(resume)
    launch(resume, route)
    assert resume.gate.entered.wait(2)
    new_lease = None
    owned_threads = []
    try:
        resume.read_release.set()
        assert entered_error.wait(5) and server._sessions.get("runtime") is None
        new_lease, error = try_acquire_active_session(session_id="durable", surface="tui",
            config={"max_concurrent_sessions": 1}, metadata={"live_session_id": "replacement-runtime"})
        assert new_lease is not None and error is None and not new_lease.released
        fresh_agent = SimpleNamespace(**vars(resume.session["agent"]))
        transport = Transport()
        successor = _session(agent=fresh_agent, session_key="durable", transport=transport,
            profile_home=home, active_session_lease=new_lease,
            history=resume.db.get_messages_as_conversation("durable"))
        server._sessions["replacement-runtime"] = successor
        resume.provider_release.clear()
        ack = server.dispatch({"id": "successor", "method": "prompt.submit", "params": {
            "session_id": "replacement-runtime", "text": "successor question"}}, transport)
        assert ack["result"]["status"] == "streaming", ack
        assert resume.provider_entered.wait(5)
        owned_threads = [t for t in threading.enumerate()
            if getattr(t, "_turn_outcome_execution", (None,))[0] is successor]
        resume.threads.extend(owned_threads)
        turn = successor["inflight_turn"]
        assert read_turn_marker(home, "durable")["prompt"] == "successor question"
        release_error.set()
        resume.threads[0].join(5)
        assert not resume.threads[0].is_alive()
        marker = read_turn_marker(home, "durable")
        assert marker and marker["prompt"] == "successor question"
        assert successor["running"] and successor["inflight_turn"]["started_at"] is turn["started_at"]
        assert successor["_turn_outcomes"].turns[-1]["accepted_turn"] == ack["result"]["accepted_turn"]
        assert not new_lease.released
    finally:
        release_error.set()
        resume.provider_release.set()
        for thread in owned_threads:
            thread.join(5)
            assert not thread.is_alive()
        if new_lease is not None:
            new_lease.release()
        lease.release()
