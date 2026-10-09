"""Accepted replies survive history shrink without transcript novelty guesses."""
import io
import json
import threading
import types

import pytest

from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tui_gateway.host_supervisor import HostSupervisor
from tui_gateway.turn_outcomes import MAX_BYTES, MAX_FINALIZED, MAX_TURNS, TurnOutcomeWindow
from tests.tui_gateway.terminal_settlement_helpers import wait_for_terminal_projection
from tests.tui_gateway.test_prompt_recovery_contract import _session, turn_env

_REAL_THREAD = threading.Thread


@pytest.fixture
def projection_env(monkeypatch):
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_session_info", lambda *a: {})
    monkeypatch.setattr(server, "_fallback_session_info", lambda *a: {})
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a: False)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_pending_clarify_request_payload", lambda sid: None)
    frames = []
    monkeypatch.setattr(server, "_stdio_transport", types.SimpleNamespace(write=frames.append))
    yield frames
    # These sessions use fake host pipes; production teardown would wait for
    # control ACKs that this in-process fixture intentionally cannot produce.
    server._sessions.clear()


def host_owner(tmp_path, monkeypatch, session, *, boot="boot"):
    host = HostSupervisor(registry_path=tmp_path / "host.json", autostart=False)
    host._hello = {"boot_id": boot}
    sent = []
    # This fake transport represents a validated child; no real startup occurs.
    monkeypatch.setattr(host, "is_ready", lambda: True)
    monkeypatch.setattr(host, "start", lambda: None)
    monkeypatch.setattr(host, "_send_frame", lambda frame, **kw: sent.append(frame))
    monkeypatch.setattr(server, "_compute_host_supervisor", host)
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: host)
    server._sessions["s"] = session
    ack = server._submit_prompt_to_compute_host("reused-rpc", "s", session, "prompt")
    host._test_submit_ack = ack
    return host, sent, ack["result"]["accepted_turn"]


def terminal(ref, finals=None, *, state="complete", **extra):
    return {"type": "turn.end", "sid": ref["session_id"], "request_id": ref["request_id"],
            "_host_boot_id": ref["host_boot_id"],
            "turn_outcome": {"accepted_turn": dict(ref), "state": state,
                             "finalized": finals if finals is not None else [{"text": "answer", "status": "complete"}]},
            **extra}


def snapshot(session, *, omit=True):
    return server._live_session_payload("s", session, omit_messages=omit)["turn_outcomes"]


def test_real_dispatch_emit_terminal_resume_survives_194_to_15_shrink(tmp_path, monkeypatch, turn_env, projection_env):
    parent = _session(running=True, history=[{"role": "assistant", "content": "old"}] * 194)
    output = io.StringIO()
    child_host = ComputeHost(stdout=output, heartbeat_secs=0)
    host, sent, ref = host_owner(tmp_path, monkeypatch, parent, boot=child_host._boot_id)
    child = _session(running=False, history=list(parent["history"]))
    calls = []

    def provider(prompt, **kwargs):
        calls.append(prompt)
        child["session_key"] = "compressed-tip"
        return {"final_response": "owned new answer", "messages": [
            *[{"role": "assistant", "content": "protected old"}] * 14,
            {"role": "assistant", "content": "owned new answer"}]}

    child["agent"] = types.SimpleNamespace(session_id="compressed-tip", model="fake", provider="fake",
        context_compressor=None, run_conversation=provider, clear_interrupt=lambda: None)
    monkeypatch.setattr(child_host, "_ensure_server_session", lambda *a: child)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *a: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda *a: None)
    monkeypatch.setattr(server, "_inside_compute_host_child", lambda: True)
    try:
        server._sessions["s"] = child
        child_host._run_real_turn(sent[0])
        assert len(child["history"]) == 15
        complete = [f["params"]["payload"] for f in projection_env
                    if f.get("params", {}).get("type") == "message.complete"]
        assert complete[0]["accepted_turn"] == ref
        assert complete[0]["chain_pending"] is True
        frames = [json.loads(line) for line in output.getvalue().splitlines()]
        done = next(f for f in frames if f["type"] == "turn.end")
        server._sessions["s"] = parent
        host._complete_turn(done)
        wait_for_terminal_projection(host)
        # Emulate the compacted resume transcript; collection has no count gate.
        parent["history"] = child["history"]
        resume_payload = server._live_session_payload("s", parent, omit_messages=True)
        result = resume_payload["turn_outcomes"]
        assert result["turns"] == [{"accepted_turn": ref, "state": "complete", "finalized": [
            {"text": "owned new answer", "status": "complete"}]}]
        assert parent["session_key"] == "compressed-tip"
        (tmp_path / "wire-contract.json").write_text(json.dumps({
            "submit_ack": host._test_submit_ack,
            "resume_result": resume_payload}, indent=2), encoding="utf-8")
        host._complete_turn(done)
        wait_for_terminal_projection(host)
        assert snapshot(parent) == result
        assert calls == ["prompt"] and len(sent) == 1
    finally:
        child_host.close()


@pytest.mark.parametrize("bad", ["request", "sid", "boot", "ref"])
def test_wrong_terminal_identity_cannot_expose_reply(tmp_path, monkeypatch, projection_env, bad):
    session = _session(running=True)
    host, sent, ref = host_owner(tmp_path, monkeypatch, session)
    frame = terminal(ref)
    if bad == "ref":
        frame["turn_outcome"]["accepted_turn"]["request_id"] = "other"
    else:
        frame[{"request": "request_id", "sid": "sid", "boot": "_host_boot_id"}[bad]] = "other"
    host._complete_turn(frame)
    wait_for_terminal_projection(host)
    projected = snapshot(session)["turns"][0]
    assert projected["state"] != "complete" and projected["finalized"] == []
    if bad != "ref":
        assert session["running"] is True
        host._complete_turn(terminal(ref))
        wait_for_terminal_projection(host)
        assert snapshot(session)["turns"][0]["state"] == "complete"
    assert len(sent) == 1


def test_chain_pending_keeps_order_and_does_not_settle(monkeypatch, projection_env):
    session = _session(running=True, _host_turn_request_id="A")
    server._sessions["s"] = session
    with session["history_lock"]:
        ref = server._begin_turn_outcome(session, "s", "A", "compute_host", "boot")
    monkeypatch.setattr(server, "_inside_compute_host_child", lambda: True)
    token = server._turn_outcome_execution.set((session, "s", "A"))
    try:
        server._emit("message.complete", "s", {"text": "substantive answer", "status": "complete"})
        server._emit("message.complete", "s", {"text": "pass", "status": "complete"})
    finally:
        server._turn_outcome_execution.reset(token)
    assert snapshot(session)["turns"][0]["state"] == "running"
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    try:
        host._emit_real_turn_terminal({"type": "turn.end", "sid": "s", "request_id": "A"})
    finally:
        host.close()
    outcome = json.loads(output.getvalue())["turn_outcome"]
    assert outcome["accepted_turn"] == ref and outcome["state"] == "complete"
    assert [p["text"] for p in outcome["finalized"]] == ["substantive answer", "pass"]


@pytest.mark.parametrize("state", ["error", "interrupted", "waiting"])
def test_failed_interrupted_waiting_terminals_are_not_success(tmp_path, monkeypatch, projection_env, state):
    session = _session(running=True)
    host, _, ref = host_owner(tmp_path, monkeypatch, session)
    status = "complete" if state == "waiting" else state
    final = {"text": "partial", "status": status, "warning": "keep warning"}
    if state == "error":
        final["error"] = "provider failed"
    host._complete_turn(terminal(ref, [final], state=state))
    wait_for_terminal_projection(host)
    assert snapshot(session)["turns"][0]["state"] == state
    assert snapshot(session)["turns"][0]["finalized"] == [final]


def test_pending_clarify_only_marks_current_outcome_waiting(monkeypatch, projection_env):
    session = _session(running=True)
    server._sessions["s"] = session
    window = TurnOutcomeWindow(session, "s")
    session["_turn_outcomes"] = window
    window.begin("old", "inline", None)
    window.append("old", {"text": "old answer", "status": "complete"})
    window.finish("old")
    window.begin("new", "inline", None)
    monkeypatch.setattr(server, "_pending_clarify_request_payload", lambda sid: {"request_id": "clarify"})
    assert [t["state"] for t in snapshot(session)["turns"]] == ["complete", "waiting"]
    assert window.find("new")["state"] == "running"


def test_identical_text_belongs_to_distinct_accepted_turns(monkeypatch, projection_env):
    session = _session(running=True)
    server._sessions["s"] = session
    refs = []
    for request in ("A", "B"):
        ref = server._begin_turn_outcome(session, "s", request, "inline")
        refs.append(ref)
        token = server._turn_outcome_execution.set((session, "s", request))
        try:
            server._emit("message.complete", "s", {"text": "same answer", "status": "complete"})
        finally:
            server._turn_outcome_execution.reset(token)
        session["_turn_outcomes"].finish(request)
    result = snapshot(session)["turns"]
    assert [t["accepted_turn"] for t in result] == refs
    assert [t["finalized"][0]["text"] for t in result] == ["same answer", "same answer"]


def test_late_old_execution_cannot_relabel_as_new_request(monkeypatch, projection_env):
    session = _session(running=True)
    server._sessions["s"] = session
    old_ref = server._begin_turn_outcome(session, "s", "old", "inline")
    token = server._turn_outcome_execution.set((session, "s", "old"))
    try:
        server._emit("message.complete", "s", {"text": "old answer", "status": "complete"})
        assert projection_env[-1]["params"]["payload"]["accepted_turn"] == old_ref
        published = list(projection_env)
        session["_turn_outcomes"].finish("old")
        server._begin_turn_outcome(session, "s", "new", "inline")
        session["_compute_host_active_request_id"] = "new"
        session["_host_turn_request_id"] = "new"
        server._emit("message.complete", "s", {"text": "late old text", "status": "complete"})
    finally:
        server._turn_outcome_execution.reset(token)
    assert snapshot(session)["turns"][-1]["finalized"] == []
    assert snapshot(session)["turns"][-1]["state"] == "running"
    assert projection_env == published
    assert session["_turn_outcomes"].find("old")["finalized"] == [
        {"text": "old answer", "status": "complete"},
    ]


def test_transcript_cache_overwrite_restart_and_cross_owner_are_unavailable(monkeypatch, projection_env):
    session = _session(history=[{"role": "assistant", "content": "old protected reply", "_row_id": 999}])
    server._sessions["s"] = session
    assert snapshot(session)["availability"] == "unavailable"
    server._begin_turn_outcome(session, "s", "A", "inline")
    old_cache = session["_turn_outcomes"]
    replacement = _session(session_key="other-root", profile_home="other-profile")
    replacement["_turn_outcomes"] = old_cache
    server._sessions["s"] = replacement
    assert snapshot(session)["availability"] == "unavailable"
    assert snapshot(replacement)["availability"] == "unavailable"
    server._sessions["s"] = session
    session["profile_home"] = "changed-profile"
    assert snapshot(session)["availability"] == "unavailable"
    session.pop("_turn_outcomes")
    assert snapshot(session)["turns"] == []


def test_session_replacement_rejects_terminal_and_old_emitter(tmp_path, monkeypatch, projection_env):
    session = _session(running=True)
    host, _, ref = host_owner(tmp_path, monkeypatch, session)
    published = list(projection_env)
    replacement = _session(session_key="new-root")
    server._sessions["s"] = replacement
    host._complete_turn(terminal(ref))
    wait_for_terminal_projection(host)
    token = server._turn_outcome_execution.set((session, "s", ref["request_id"]))
    try:
        server._emit("message.complete", "s", {"text": "old execution", "status": "complete"})
    finally:
        server._turn_outcome_execution.reset(token)
    assert snapshot(replacement)["turns"] == [] and session["running"] is True
    assert projection_env == published
    assert session["_turn_outcomes"].find(ref["request_id"])["finalized"] == [
        {"text": "old execution", "status": "complete"},
    ]


def test_host_replacement_and_crash_revoke_projection(tmp_path, monkeypatch, projection_env):
    session = _session(running=True)
    host, _, ref = host_owner(tmp_path, monkeypatch, session)
    host._complete_turn(terminal(ref))
    wait_for_terminal_projection(host)
    assert snapshot(session)["turns"][0]["state"] == "complete"
    host._hello = {"boot_id": "replacement"}
    assert snapshot(session)["turns"][0]["state"] == "unavailable"
    monkeypatch.setattr(server, "_compute_host_supervisor", object())
    assert snapshot(session)["availability"] == "unavailable"


def test_already_validated_terminal_after_boot_replacement_still_settles(tmp_path, monkeypatch, projection_env):
    session = _session(running=True)
    host, _, ref = host_owner(tmp_path, monkeypatch, session)
    # The supervisor already matched this terminal; its callback can lag a
    # replacement handshake. Losing projection is not lost executor settlement.
    host._hello = {"boot_id": "replacement"}
    server._on_compute_host_turn_done("rpc", "s", session, terminal(ref))
    assert session["running"] is False
    assert not session.get("_compute_host_active_request_id")
    assert snapshot(session)["turns"][0]["state"] == "unavailable"


def test_bounded_window_evicts_and_overflow_is_unavailable(monkeypatch, projection_env):
    session = _session()
    server._sessions["s"] = session
    for n in range(MAX_TURNS + 1):
        server._begin_turn_outcome(session, "s", str(n), "inline")
    window = session["_turn_outcomes"]
    assert len(window.turns) == MAX_TURNS and window.find("0") is None
    request = str(MAX_TURNS)
    window.append(request, {"text": "x" * MAX_BYTES, "status": "complete"})
    window.finish(request)
    assert window.find(request)["state"] == "unavailable"
    assert window.find(request)["finalized"] == []
    assert window.find(request)["reason"] == "payload_limit"
    server._begin_turn_outcome(session, "s", "too-many", "inline")
    for _ in range(MAX_FINALIZED + 1):
        window.append("too-many", {"text": "same", "status": "complete"})
    window.finish("too-many")
    assert window.find("too-many")["state"] == "unavailable"
    assert len(json.dumps(snapshot(session)).encode()) < MAX_BYTES


def test_inline_ack_nonce_and_queued_input_never_borrow_active_owner(monkeypatch, turn_env, projection_env):
    session = _session()
    server._sessions["s"] = session
    monkeypatch.setattr(server, "_sess_nowait", lambda *a: (session, None))
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a: None)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *a: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda *a: None)
    monkeypatch.setattr(server, "_accept_tui_context_input", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *a: None)
    runs = []

    def run(rid, sid, state, text, **kwargs):
        runs.append(text)
        server._emit("message.complete", sid, {"text": "same", "status": "complete"})
        state["running"] = False

    monkeypatch.setattr(server, "_run_prompt_submit", run)
    acknowledgements = [server._methods["prompt.submit"]("repeated-rpc", {"session_id": "s", "text": text})
                        for text in ("first", "second")]
    refs = [a["result"]["accepted_turn"] for a in acknowledgements]
    assert refs[0] != refs[1] and all(ref["request_id"] != "repeated-rpc" for ref in refs)
    assert all(ref["session_id"] == "s" and ref["route"] == "inline" and ref["host_boot_id"] is None for ref in refs)
    session["running"] = True
    session["_compute_host_active_request_id"] = "OTHER"
    queued = server._methods["prompt.submit"]("queue-rpc", {"session_id": "s", "text": "queued", "queued": True})
    assert queued["result"]["status"] == "queued"
    assert "accepted_turn" not in queued["result"]
    assert runs == ["first", "second"]


def inline_admission(monkeypatch, session):
    server._sessions["s"] = session
    monkeypatch.setattr(server, "_sess_nowait", lambda *a: (session, None))
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a: None)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *a: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda *a: None)
    monkeypatch.setattr(server, "_accept_tui_context_input", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *a: None)


def test_real_inline_chain_preserves_all_finalized_under_one_admission(monkeypatch, turn_env, projection_env):
    session = _session()
    inline_admission(monkeypatch, session)
    calls = []
    def provider(prompt, **kwargs):
        calls.append(prompt)
        if len(calls) == 2:
            assert snapshot(session)["turns"][0]["state"] == "running"
        text = "substantive" if len(calls) == 1 else "pass"
        return {"final_response": text, "messages": [*(kwargs.get("conversation_history") or []),
            {"role": "user", "content": prompt}, {"role": "assistant", "content": text}]}
    session["agent"] = types.SimpleNamespace(session_id="inline-key", model="fake", provider="fake",
        context_compressor=None, run_conversation=provider, clear_interrupt=lambda: None)
    monkeypatch.setattr(server, "_plan_goal_compression_recovery", lambda *a, **kw: (
        "continue" if len(calls) == 1 else None, None))
    ack = server._methods["prompt.submit"]("rpc", {"session_id": "s", "text": "prompt"})
    turn = snapshot(session)["turns"][0]
    assert turn["accepted_turn"] == ack["result"]["accepted_turn"]
    assert turn["state"] == "complete"
    assert [f["text"] for f in turn["finalized"]] == ["substantive", "pass"]
    assert calls == ["prompt", "continue"]


@pytest.mark.parametrize("retirement", ["closing", "replacement", "cancel"])
def test_inline_observer_detaches_with_bounded_join(monkeypatch, turn_env, projection_env, retirement):
    session = _session()
    inline_admission(monkeypatch, session)
    entered, release = threading.Event(), threading.Event()
    observers, workers = [], []
    def make_observer(*args, **kwargs):
        thread = _REAL_THREAD(*args, **kwargs)
        observers.append(thread)
        return thread
    monkeypatch.setattr(server.threading, "Thread", make_observer)
    def submit(rid, sid, state, text, **kwargs):
        captured = server._turn_outcome_execution.get()
        def blocked_worker():
            token = server._turn_outcome_execution.set(captured)
            try:
                server._emit("message.complete", sid, {"text": "partial", "status": "complete"})
                entered.set()
                release.wait(3)
            finally:
                server._turn_outcome_execution.reset(token)
        worker = _REAL_THREAD(target=blocked_worker, daemon=True)
        worker._turn_outcome_execution = captured
        state["_run_thread"] = worker
        workers.append(worker)
        worker.start()
    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    try:
        ack = server._methods["prompt.submit"]("rpc", {"session_id": "s", "text": "prompt"})
        assert ack["result"]["accepted_turn"]["route"] == "inline"
        assert entered.wait(2)
        assert snapshot(session)["turns"][0]["state"] == "running"
        if retirement == "replacement":
            server._sessions["s"] = _session()
        elif retirement == "closing":
            session["_closing"] = True
        else:
            session["_turn_cancel_requested"] = True
        observers[0].join(2)
        assert not observers[0].is_alive()
        assert workers[0].is_alive(), "observer must not wait indefinitely for provider exit"
        assert session["_turn_outcomes"].turns[0]["state"] == "unavailable"
    finally:
        release.set()
        for thread in [*workers, *observers]:
            thread.join(2)


@pytest.mark.parametrize("new_owner", [False, True])
def test_exited_inline_worker_stop_is_bound_to_its_admission(monkeypatch, turn_env, projection_env, new_owner):
    session = _session()
    inline_admission(monkeypatch, session)
    def submit(rid, sid, state, text, **kwargs):
        captured = server._turn_outcome_execution.get()
        server._emit("message.complete", sid, {"text": "complete bubble", "status": "complete"})
        if new_owner:
            server._begin_turn_outcome(state, sid, "NEW", "inline")
            captured = (state, sid, "NEW")
        state["_run_thread"] = types.SimpleNamespace(_turn_outcome_execution=captured,
            is_alive=lambda: False, join=lambda **kw: None)
        state["_turn_cancel_requested"] = True
        state["running"] = False
    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    ack = server._methods["prompt.submit"]("rpc", {"session_id": "s", "text": "prompt"})
    turn = session["_turn_outcomes"].find(ack["result"]["accepted_turn"]["request_id"])
    assert turn["state"] == ("complete" if new_owner else "interrupted")


def test_live_inline_observer_cannot_borrow_new_owner_cancel(monkeypatch, turn_env, projection_env):
    session = _session()
    inline_admission(monkeypatch, session)
    entered, release = threading.Event(), threading.Event()
    first_poll, owner_moved, observed_new_owner = threading.Event(), threading.Event(), threading.Event()
    observers, workers = [], []
    def make_observer(*args, **kwargs):
        observer = _REAL_THREAD(*args, **kwargs)
        observers.append(observer)
        return observer
    monkeypatch.setattr(server.threading, "Thread", make_observer)
    class Worker(_REAL_THREAD):
        def join(self, timeout=None):
            first_poll.set()
            if owner_moved.is_set():
                observed_new_owner.set()
            return super().join(timeout)
    def submit(rid, sid, state, text, **kwargs):
        captured = server._turn_outcome_execution.get()
        def blocked_worker():
            token = server._turn_outcome_execution.set(captured)
            try:
                server._emit("message.complete", sid, {"text": "old answer", "status": "complete"})
                entered.set()
                release.wait(4)
            finally:
                server._turn_outcome_execution.reset(token)
        worker = Worker(target=blocked_worker, daemon=True)
        worker._turn_outcome_execution = captured
        with server._sessions_lock:
            state["_run_thread"] = worker
            workers.append(worker)
            worker.start()
    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    try:
        ack = server._methods["prompt.submit"]("rpc", {"session_id": "s", "text": "prompt"})
        assert entered.wait(2)
        assert first_poll.wait(2)
        request_id = ack["result"]["accepted_turn"]["request_id"]
        with session["history_lock"]:
            server._begin_turn_outcome(session, "s", "NEW", "inline")
            session["_run_thread"] = types.SimpleNamespace(
                _turn_outcome_execution=(session, "s", "NEW"), is_alive=lambda: False,
                join=lambda **kwargs: None)
            session["_turn_cancel_requested"] = True
            owner_moved.set()
        assert observed_new_owner.wait(2)
        assert session["_turn_outcomes"].find(request_id)["state"] == "running"
        release.set()
        observers[0].join(2)
        assert not observers[0].is_alive()
        assert session["_turn_outcomes"].find(request_id)["state"] == "complete"
        assert session["_turn_outcomes"].find("NEW")["state"] == "running"
    finally:
        release.set()
        for worker in [*workers, *observers]:
            worker.join(2)


def test_projection_exceptions_do_not_break_emission_or_settlement(tmp_path, monkeypatch, projection_env):
    session = _session(running=True)
    host, _, ref = host_owner(tmp_path, monkeypatch, session)
    window = session["_turn_outcomes"]
    def fail(*a, **kw):
        raise ValueError("projection-only failure")
    monkeypatch.setattr(window, "accept_terminal", fail)
    host._complete_turn(terminal(ref))
    wait_for_terminal_projection(host)
    assert session["running"] is False
    assert not session.get("_compute_host_active_request_id")
    assert snapshot(session)["turns"][0]["reason"] == "projection_failed"
    server._begin_turn_outcome(session, "s", "inline", "inline")
    window = session["_turn_outcomes"]
    monkeypatch.setattr(window, "append", fail)
    token = server._turn_outcome_execution.set((session, "s", "inline"))
    try:
        server._emit("message.complete", "s", {"text": "legacy emission", "status": "complete"})
    finally:
        server._turn_outcome_execution.reset(token)
    assert projection_env[-1]["params"]["payload"]["text"] == "legacy emission"
    assert snapshot(session)["turns"][0]["state"] == "unavailable"


def test_surrogate_json_string_does_not_raise_in_projection(monkeypatch, projection_env):
    session = _session()
    server._sessions["s"] = session
    server._begin_turn_outcome(session, "s", "A", "inline")
    token = server._turn_outcome_execution.set((session, "s", "A"))
    try:
        server._emit("message.complete", "s", {"text": "\ud800", "status": "complete"})
    finally:
        server._turn_outcome_execution.reset(token)
    assert session["_turn_outcomes"].find("A")["finalized"][0]["text"] == "\ud800"


def test_replacement_while_emitter_waits_for_lock_cannot_attach_ref(monkeypatch, projection_env):
    entered, release = threading.Event(), threading.Event()
    session = _session()
    server._sessions["s"] = session
    server._begin_turn_outcome(session, "s", "A", "inline")
    class Gate:
        def __enter__(self):
            entered.set()
            assert release.wait(2)
        def __exit__(self, *args):
            return False
    session["history_lock"] = Gate()
    def emit():
        token = server._turn_outcome_execution.set((session, "s", "A"))
        try:
            server._emit("message.complete", "s", {"text": "late", "status": "complete"})
        finally:
            server._turn_outcome_execution.reset(token)
    worker = _REAL_THREAD(target=emit)
    worker.start()
    try:
        assert entered.wait(2)
        replacement = _session()
        server._sessions["s"] = replacement
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert projection_env == []
    assert "_turn_outcomes" not in replacement
    assert session["_turn_outcomes"].find("A")["finalized"] == [
        {"text": "late", "status": "complete"},
    ]


def test_next_admission_retains_previous_terminal_for_collector(tmp_path, monkeypatch, projection_env):
    session = _session(running=True)
    host, sent, first = host_owner(tmp_path, monkeypatch, session)
    host._complete_turn(terminal(first))
    wait_for_terminal_projection(host)
    session["running"] = True
    second = server._submit_prompt_to_compute_host("reused-rpc", "s", session, "next")["result"]["accepted_turn"]
    turns = snapshot(session)["turns"]
    assert turns[0]["accepted_turn"] == first and turns[0]["state"] == "complete"
    assert turns[0]["finalized"][0]["text"] == "answer"
    assert turns[1]["accepted_turn"] == second and turns[1]["state"] == "running"
    assert len(sent) == 2


def test_ack_ref_survives_projection_eviction_during_dispatch(monkeypatch, projection_env):
    session = _session(running=True)
    server._sessions["s"] = session
    class FastHost:
        boot_id = "boot"
        def submit_turn(self, frame, **kwargs):
            frame["_admitted_host_boot_id"] = self.boot_id
            for n in range(MAX_TURNS + 1):
                server._begin_turn_outcome(session, "s", f"successor-{n}", "compute_host", self.boot_id, self)
    host = FastHost()
    monkeypatch.setattr(server, "_compute_host_supervisor", host)
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: host)
    ack = server._submit_prompt_to_compute_host("rpc", "s", session, "prompt")
    ref = ack["result"]["accepted_turn"]
    assert ref["request_id"].startswith("host-turn-") and ref["host_boot_id"] == "boot"
    assert session["_turn_outcomes"].find(ref["request_id"]) is None
