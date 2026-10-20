"""Real RPC resolution of accepted turns whose runtime and stored IDs differ."""
import json
import types
import threading
import time
import queue
import itertools
from pathlib import Path
import subprocess
import shutil

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from tests.tui_gateway.test_prompt_recovery_contract import _session, turn_env

_REAL_THREAD = threading.Thread
_REAL_PENDING_CLARIFY = server._pending_clarify_request_payload
_REQUEST_IDS = itertools.count()


class RecordingTransport:
    def __init__(self):
        self.frames = queue.Queue()

    def write(self, frame):
        self.frames.put(frame)
        return True

    def close(self):
        pass


def rpc(method, **params):
    rid = f"poll-test-{next(_REQUEST_IDS)}"
    transport = RecordingTransport()
    result = server.dispatch({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, transport)
    if result is not None:
        return result
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        frame = transport.frames.get(timeout=max(0.01, deadline - time.monotonic()))
        if frame.get("id") == rid:
            return frame
    raise AssertionError("RPC worker did not return a response")


@pytest.fixture()
def owned_turn(tmp_path, monkeypatch, turn_env):
    # Preserve real SQLite background-writer threads; the imported helper's
    # inline threads are only suitable for projection tests without a real DB.
    monkeypatch.setattr(server.threading, "Thread", _REAL_THREAD)
    home = tmp_path / ".hermes"
    profile = home / "profiles" / "alpha"
    profile.mkdir(parents=True)
    db = SessionDB(db_path=profile / "state.db")
    key = "20261002_142439_canonical"
    runtime = "dce84102"
    db.create_session(key, "desktop")
    db.set_session_title(key, "Group: test-room")
    calls, frames = [], []

    def provider(prompt, **kwargs):
        calls.append(prompt)
        return {"final_response": "owned answer", "messages": [
            {"role": "user", "content": prompt}, {"role": "assistant", "content": "owned answer"}]}

    agent = types.SimpleNamespace(session_id=key, model="fake", provider="fake",
        context_compressor=None, run_conversation=provider, clear_interrupt=lambda: None)
    session = _session(agent=agent, session_key=key, profile_home=str(profile),
        source="desktop", cwd=str(tmp_path), last_active=123,
        transport=object(), viewers={})
    monkeypatch.setattr(server, "_sessions", {runtime: session})
    monkeypatch.setattr(server, "_hermes_home", home)
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda *a: None)
    monkeypatch.setattr(server, "_accept_tui_context_input", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *a: None)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_plan_goal_compression_recovery", lambda *a, **kw: (None, None))
    monkeypatch.setattr(server, "write_json", frames.append)
    monkeypatch.setattr(server, "_pending_approval_request_payload", lambda *a: None)
    monkeypatch.setattr(server, "_pending_clarify_request_payload", lambda *a, **kw: None)
    yield types.SimpleNamespace(runtime=runtime, key=key, home=home, profile=profile,
        session=session, db=db, calls=calls, frames=frames)
    db.close()


def admit(owned_turn):
    ack = rpc("prompt.submit", session_id=owned_turn.runtime, profile="alpha", text="offline prompt")
    ref = ack["result"]["accepted_turn"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with owned_turn.session["history_lock"]:
            turn = owned_turn.session["_turn_outcomes"].find(ref["request_id"])
            if turn["state"] == "complete":
                break
        time.sleep(0.005)
    assert turn["state"] == "complete"
    return ack


def poll(owner, ref):
    return rpc("session.turn.poll", session_id=owner.runtime, profile="alpha", accepted_turn=ref)


def test_real_resume_resolver_does_not_accept_runtime_ids(owned_turn):
    ack = admit(owned_turn)
    ref = ack["result"]["accepted_turn"]
    assert ref["session_id"] == owned_turn.runtime != owned_turn.key
    assert owned_turn.db.get_session(ref["session_id"]) is None
    assert owned_turn.db.get_session_by_title(ref["session_id"]) is None
    assert rpc("session.resume", session_id=ref["session_id"], profile="alpha")["error"]["code"] == 4007


def test_runtime_poll_returns_actual_accepted_turn(owned_turn, monkeypatch):
    ref = admit(owned_turn)["result"]["accepted_turn"]
    session = owned_turn.session
    lifecycle = {k: session.get(k) for k in ("transport", "viewers", "last_active", "cols", "session_key")}
    with owned_turn.db._lock:
        rows_before = list(owned_turn.db._conn.iterdump())

    def forbidden(*a, **kw):
        raise AssertionError("poll attempted session hydration/lifecycle mutation")

    for name in ("_get_db", "_session_db", "_cancel_ws_orphan_reap", "_start_agent_build", "_init_session"):
        monkeypatch.setattr(server, name, forbidden)
    monkeypatch.setattr("hermes_state.SessionDB", forbidden)
    response = poll(owned_turn, ref)
    assert "result" in response, response
    assert response["result"]["turn_outcomes"]["turns"][0]["accepted_turn"] == ref
    assert response["result"]["turn_outcomes"]["turns"][0]["finalized"] == [{"text": "owned answer", "status": "complete"}]
    assert "messages" not in response["result"]
    assert lifecycle == {k: session.get(k) for k in lifecycle}
    with owned_turn.db._lock:
        assert rows_before == list(owned_turn.db._conn.iterdump())
    assert owned_turn.calls == ["offline prompt"]


@pytest.mark.parametrize("profile", ["default", "beta", "../", "../../", "/tmp", "", [], "UNKNOWN"])
def test_wrong_or_invalid_profile_never_adopts_owner(owned_turn, profile):
    ref = admit(owned_turn)["result"]["accepted_turn"]
    response = rpc("session.turn.poll", session_id=owned_turn.runtime, profile=profile, accepted_turn=ref)
    assert response["error"]["code"] == 4030
    assert server._sessions == {owned_turn.runtime: owned_turn.session}
    assert owned_turn.calls == ["offline prompt"]


@pytest.mark.parametrize("field,value", [("request_id", "another-request"), ("route", "compute_host"),
    ("host_boot_id", "another-boot"), ("session_id", "another-runtime"), ("route", []), ("route", {})])
def test_wrong_or_malformed_tuple_is_never_success(owned_turn, field, value):
    ref = {**admit(owned_turn)["result"]["accepted_turn"], field: value}
    if field == "route" and value == "compute_host":
        ref["host_boot_id"] = "another-boot"
    response = poll(owned_turn, ref)
    if "error" not in response:
        assert response["result"]["turn_outcomes"]["availability"] == "unavailable"
        assert response["result"]["turn_outcomes"]["turns"] == []
    else:
        assert response["error"]["code"] == 4006


@pytest.mark.parametrize("loss", ["reaped", "replacement", "closing", "finalized", "window", "profile"])
def test_owner_loss_never_recovers_from_saved_transcript(owned_turn, loss):
    ref = admit(owned_turn)["result"]["accepted_turn"]
    s = owned_turn.session
    if loss == "reaped":
        server._sessions.pop(owned_turn.runtime)
    elif loss == "replacement":
        server._sessions[owned_turn.runtime] = _session(session_key=owned_turn.key, profile_home=str(owned_turn.profile))
    elif loss == "window":
        s.pop("_turn_outcomes")
    elif loss == "profile":
        s["profile_home"] = str(owned_turn.home)
    else:
        s[f"_{loss}"] = True
    response = poll(owned_turn, ref)
    assert "error" in response or response["result"]["turn_outcomes"]["availability"] == "unavailable"
    assert owned_turn.db.get_session(owned_turn.key) is not None


def test_compaction_rotates_canonical_key_without_rotating_execution(owned_turn):
    ref = admit(owned_turn)["result"]["accepted_turn"]
    owned_turn.db.create_session("compressed-tip", "desktop", parent_session_id=owned_turn.key)
    owned_turn.session["session_key"] = "compressed-tip"
    owned_turn.session["history"] = [{"role": "user", "content": "summary"}]
    for _ in range(2):
        result = poll(owned_turn, ref)["result"]
        assert result["session_id"] == ref["session_id"]
        assert result["turn_outcomes"]["turns"][0]["finalized"][0]["text"] == "owned answer"
    assert owned_turn.calls == ["offline prompt"]


def test_pending_read_cannot_bind_a_replacement_record(owned_turn, monkeypatch):
    ref = admit(owned_turn)["result"]["accepted_turn"]
    s = owned_turn.session
    s["_turn_outcomes"].find(ref["request_id"])["state"] = "running"
    replacement = _session(profile_home=str(owned_turn.profile), _compute_host_active=True)
    controls = []
    monkeypatch.setattr(server, "_call_host_clarify", lambda *a, **kw: controls.append(a))
    monkeypatch.setattr(server, "_pending_clarify_request_payload", _REAL_PENDING_CLARIFY)
    def replace_before_clarify(*a):
        server._sessions[owned_turn.runtime] = replacement
        return None
    monkeypatch.setattr(server, "_pending_approval_request_payload", replace_before_clarify)
    assert poll(owned_turn, ref)["error"]["code"] == 4001
    assert controls == []
    assert "_host_clarify_binding" not in replacement


@pytest.mark.parametrize("replacement", ["boot", "supervisor", "transport"])
def test_pending_read_is_pinned_to_accepted_host(owned_turn, monkeypatch, replacement):
    from tui_gateway.turn_outcomes import TurnOutcomeWindow
    controls = []
    supervisor = types.SimpleNamespace(boot_id="boot-A", is_ready=lambda: True,
        control=lambda *a, **kw: controls.append((a, kw)))
    monkeypatch.setattr(server, "_compute_host_supervisor", supervisor)
    s = owned_turn.session
    window = TurnOutcomeWindow(s, owned_turn.runtime, supervisor=supervisor)
    s["_turn_outcomes"] = window
    s["_compute_host_active"] = True
    ref = window.begin("accepted-host-request", "compute_host", "boot-A")
    monkeypatch.setattr(server, "_pending_clarify_request_payload", _REAL_PENDING_CLARIFY)
    def change_before_clarify(*a):
        if replacement == "boot":
            supervisor.boot_id = "boot-B"
        elif replacement == "supervisor":
            server._compute_host_supervisor = types.SimpleNamespace(boot_id="boot-B")
        else:
            s["transport"] = object()
        return None
    monkeypatch.setattr(server, "_pending_approval_request_payload", change_before_clarify)
    response = poll(owned_turn, ref)
    assert "error" in response or response["result"]["turn_outcomes"]["availability"] == "unavailable" or response["result"]["turn_outcomes"]["turns"][0]["state"] == "unavailable"
    if replacement != "transport":
        assert controls == []
        assert "_host_clarify_binding" not in s


@pytest.mark.parametrize("kind", ["clarify", "approval"])
def test_pending_prompt_remains_read_only_and_same_admission(owned_turn, monkeypatch, kind):
    ref = admit(owned_turn)["result"]["accepted_turn"]
    s = owned_turn.session
    s["_turn_outcomes"].find(ref["request_id"])["state"] = "running"
    s["running"] = True
    event = threading.Event()
    monkeypatch.setattr(server, "_pending", {"question-1": (owned_turn.runtime, event)})
    monkeypatch.setattr(server, "_pending_prompt_payloads", {"question-1": ("clarify.request",
        {"request_id": "question-1", "question": "Keep going?"})})
    monkeypatch.setattr(server, "_batch_clarify", {})
    if kind == "clarify":
        monkeypatch.setattr(server, "_pending_clarify_request_payload", _REAL_PENDING_CLARIFY)
    else:
        monkeypatch.setattr(server, "_pending_approval_request_payload", lambda key: {
            "request_id": "approval-1", "command": "controlled command", "choices": ["once", "deny"]})
    first = poll(owned_turn, ref)["result"]
    second = poll(owned_turn, ref)["result"]
    assert first == second
    assert first[f"pending_{kind}"]["request_id"] == ("question-1" if kind == "clarify" else "approval-1")
    assert first["turn_outcomes"]["turns"][0]["state"] == ("waiting" if kind == "clarify" else "running")
    assert not event.is_set()
    assert owned_turn.calls == ["offline prompt"]
    # A retained terminal from A cannot acquire B's prompt.
    s["_turn_outcomes"].begin("newer-admission", "inline", None)
    historical = poll(owned_turn, ref)["result"]
    assert "pending_approval" not in historical and "pending_clarify" not in historical


@pytest.mark.parametrize("state", ["active", "queued", "unknown-runtime"])
def test_no_admitted_identity_never_borrows_another_turn(owned_turn, state):
    if state != "unknown-runtime":
        ref = admit(owned_turn)["result"]["accepted_turn"]
        owned_turn.session["running"] = True
        if state == "queued":
            ack = rpc("prompt.submit", session_id=owned_turn.runtime, profile="alpha", text="queued", queued=True)
            assert ack["result"]["status"] == "queued" and "accepted_turn" not in ack["result"]
        ref = {**ref, "request_id": "not-admitted"}
    else:
        ref = {"request_id": "not-admitted", "session_id": "dead-runtime", "route": "inline", "host_boot_id": None}
    response = rpc("session.turn.poll", session_id=ref["session_id"], profile="alpha", accepted_turn=ref)
    assert "error" in response or response["result"]["turn_outcomes"]["availability"] == "unavailable"


@pytest.mark.parametrize("state", ["complete", "error", "interrupted", "reaped", "replacement", "queued", "compacted"])
def test_real_rpc_responses_drive_whole_plugin(owned_turn, monkeypatch, state, tmp_path):
    bridge = Path(__file__).resolve().parents[2] / "apps/desktop/src/plugins/hermes-bots/tests/group-turn-rpc-bridge.mjs"
    node = shutil.which("node")
    assert node
    expected = state if state in {"complete", "error", "interrupted"} else "complete" if state == "compacted" else "unavailable"
    if state in {"error", "interrupted"}:
        def provider(prompt, **kwargs):
            owned_turn.calls.append(prompt)
            return {"final_response": "partial", "messages": [],
                **({"error": "controlled fake provider error", "failed": True} if state == "error" else {"interrupted": True})}
        owned_turn.session["agent"].run_conversation = provider
    if state == "queued":
        owned_turn.session["running"] = True
        owned_turn.session["_compute_host_active_request_id"] = "already-running-owner"
    process = subprocess.Popen([node, str(bridge)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1)
    receipts = []
    try:
        process.stdin.write(json.dumps({"canonical": owned_turn.key, "runtime": owned_turn.runtime,
            "expected": expected, "queued": state == "queued"}) + "\n")
        process.stdin.flush()
        for line in process.stdout:
            request = json.loads(line)
            if request.get("done"):
                receipt = request
                break
            response = rpc(request["method"], **request["params"])
            receipts.append({"request": request, "response": response})
            if request["method"] == "prompt.submit" and response.get("result", {}).get("accepted_turn"):
                ref = response["result"]["accepted_turn"]
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    with owned_turn.session["history_lock"]:
                        turn = owned_turn.session["_turn_outcomes"].find(ref["request_id"])
                        if turn["state"] != "running":
                            break
                    time.sleep(0.005)
                assert turn["state"] != "running"
                if state == "reaped":
                    server._sessions.pop(owned_turn.runtime)
                elif state == "replacement":
                    server._sessions[owned_turn.runtime] = _session(session_key=owned_turn.key,
                        profile_home=str(owned_turn.profile))
                elif state == "compacted":
                    owned_turn.db.create_session("compressed-tip", "desktop", parent_session_id=owned_turn.key)
                    owned_turn.session["session_key"] = "compressed-tip"
                    owned_turn.session["history"] = [{"role": "user", "content": "summary"}]
            process.stdin.write(json.dumps(response) + "\n")
            process.stdin.flush()
        process.wait(timeout=5)
        (tmp_path / "actual-rpc-trace.json").write_text(json.dumps(receipts), encoding="utf8")
        assert process.returncode == 0, process.stderr.read()
        assert receipt["submits"] == 1
        assert receipt["elapsed"] <= 2000
        assert len(owned_turn.calls) == (0 if state == "queued" else 1)
        (tmp_path / "actual-rpc-wire.json").write_text(json.dumps({"receipt": receipt, "rpc": receipts}), encoding="utf8")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
