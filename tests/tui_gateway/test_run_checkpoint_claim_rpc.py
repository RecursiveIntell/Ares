"""Real RPC dispatch and native checkpoint admission; no model/provider call."""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import queue
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from hermes_state_runs import RunCheckpoint
from tui_gateway import server


METHOD = "session.run_checkpoint.claim"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class ReplyTransport:
    def __init__(self):
        self.responses = queue.Queue()

    def write(self, response):
        self.responses.put(response)
        return True


def dispatch(f, params=None, transport=None):
    transport = transport or f.transport
    result = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": METHOD,
                              "params": f.params if params is None else params}, transport)
    return result if result is not None else transport.responses.get(timeout=10)


def rows(db):
    with sqlite3.connect(f"file:{db.db_path}?mode=ro", uri=True) as conn:
        return conn.execute("SELECT key,value FROM state_meta ORDER BY key").fetchall()


def assert_first_claim_delta(before, after):
    """Preserve all prior metadata; admit only the first custody generation."""
    before, after = dict(before), dict(after)
    assert set(after) - set(before) == {
        "run-custody:rpc-fixture:head", "run-custody:rpc-fixture:generation:1",
    }
    assert all(key in after and after[key] == value for key, value in before.items())


@pytest.fixture
def live(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="current", source="desktop")
    assert db.try_acquire_session_turn_lease("current", "active-holder", ttl_seconds=120)
    goal = '{"status":"cleared","outcome":"CANCELLED"}'
    db.set_meta("goal:old", goal)
    source = tmp_path / "source.bin"
    source.write_bytes(b"source bytes")
    inventory = json.dumps({str(source): sha(source.read_bytes())}).encode()
    files = {}
    for name, raw in {"plan": b"plan", "contract": b"contract", "source": inventory}.items():
        path = tmp_path / name
        path.write_bytes(raw)
        files[name] = str(path)
    members = (("historical-goal-key", "goal:old"), ("authority", "No downstream effects"))
    files["members"] = {}
    for name, raw in members:
        path = tmp_path / (name + ".txt")
        path.write_text(raw)
        files["members"][name] = str(path)
    cp = RunCheckpoint(sha(b"plan"), sha(b"contract"), sha(inventory), "Reconcile only",
                       members, ("push:unknown",), ("FTS:open",), ("no activation",))
    request = {"checkpoint": dataclasses.asdict(cp), "files": files,
               "session_id": "current", "lease_holder": "active-holder"}
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    params = dict(session_id="runtime-sid", run_id="rpc-fixture", expected_generation=0,
                  request_path=str(request_path), expected_request_digest=sha(request_path.read_bytes()),
                  origin_session_id="old", historical_goal_digest=sha(goal.encode()), ttl_seconds=120)
    agent = SimpleNamespace(_session_db=db, session_id="current",
                            _active_session_turn_lease_holder="active-holder")
    from agent.run_checkpoint_custody import TurnRunCustody
    agent._run_checkpoint_custody = TurnRunCustody(db)
    agent._run_checkpoint_custody.begin_turn("active-holder")
    ready = threading.Event()
    ready.set()
    transport = ReplyTransport()
    session = dict(agent=agent, agent_ready=ready, agent_error=None, running=True,
                   session_key="current", profile_home=str(tmp_path), transport=transport)
    monkeypatch.setattr(server, "_sessions", {"runtime-sid": session})
    def forbidden(*a, **kw):
        pytest.fail("claim RPC must not open, build, acquire or substitute an owner")
    for name in ("_get_db", "_db_for_profile", "_session_db", "_make_agent", "_sess_building"):
        monkeypatch.setattr(server, name, forbidden)
    yield SimpleNamespace(db=db, agent=agent, session=session, cp=cp, request=request,
                          params=params, transport=transport, goal=goal, source=source)
    # This fixture owns the temporary record; do not feed it to the global
    # gateway teardown, which intentionally opens stores to finalize sessions.
    server._sessions.clear()
    db.close()


def rpc_method(f, name, params):
    result = server.dispatch({"jsonrpc": "2.0", "id": 9, "method": name, "params": params}, f.transport)
    return result if result is not None else f.transport.responses.get(timeout=10)


def test_rpc_manages_refresh_and_release_without_exposing_handle(live):
    assert "result" in dispatch(live)
    params = {"session_id": "runtime-sid", "run_id": "rpc-fixture", "expected_generation": 1, "ttl_seconds": 120}
    refreshed = rpc_method(live, "session.run_checkpoint.refresh", params)
    assert refreshed["result"]["generation"] == 2
    del params["ttl_seconds"]
    params["expected_generation"] = 2
    released = rpc_method(live, "session.run_checkpoint.release", params)
    assert released["result"]["status"] == "release_observed"
    value = live.db.read_run_custody("rpc-fixture")
    assert value.disposition == "released"
    assert value.owner_token not in json.dumps([refreshed, released])
    assert "active-holder" not in json.dumps([refreshed, released])


def test_gateway_routes_isolated_claim_without_using_local_owner(live, monkeypatch):
    calls = []
    def control(sid, **kwargs):
        calls.append((sid, kwargs))
        return {"type": "control.ack", "sid": sid, "route_name": METHOD,
                "response": {"jsonrpc": "2.0", "id": "private", "result": {"status": "claim_observed"}}}
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda s: True)
    monkeypatch.setattr(server, "_compute_host_supervisor", SimpleNamespace(control=control))
    live.session["_compute_host_active"] = True
    live.agent._session_db = None
    before = rows(live.db)
    response = dispatch(live)
    assert response["result"]["status"] == "claim_observed"
    assert len(calls) == 1
    assert calls[0][1]["payload"]["params"] == live.params
    assert rows(live.db) == before


def test_isolated_control_timeout_is_unknown_not_retried(live, monkeypatch):
    calls = []
    def control(*a, **kw):
        calls.append(1)
        raise TimeoutError("lost acknowledgement")
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda s: True)
    monkeypatch.setattr(server, "_compute_host_supervisor", SimpleNamespace(control=control))
    live.session["_compute_host_active"] = True
    response = dispatch(live)
    assert response["error"]["data"]["status"] == "unknown"
    assert response["error"]["data"]["automatic_retry"] is False
    assert len(calls) == 1


def rewrite(f):
    path = Path(f.params["request_path"])
    path.write_text(json.dumps(f.request))
    f.params["expected_request_digest"] = sha(path.read_bytes())


def refused(response, code=None):
    assert "error" in response, response
    assert response["error"]["data"]["status"] == "refused", response
    assert response["error"]["data"]["custody_changed"] is False
    if code:
        assert response["error"]["message"] == code


def test_rpc_dispatch_claims_real_owner_without_opening_or_closing_store(live):
    before = rows(live.db)
    response = dispatch(live)
    assert "result" in response, response
    result = response["result"]
    assert result["status"] == "claim_observed"
    assert result["generation"] == 1
    assert result["resume_authorized"] is False
    assert result["downstream_effects_executed"] is False
    owner = live.db.read_run_custody("rpc-fixture")
    assert owner.checkpoint == live.cp
    assert owner.controller_pid == os.getpid()
    assert owner.current_session_id == live.agent.session_id
    native = rows(live.db)
    assert_first_claim_delta(before, native)
    assert ("goal:old", live.goal) in native
    assert owner.owner_token not in json.dumps(response)
    assert "active-holder" not in json.dumps(response)
    live.db.set_meta("still-borrowed", "usable")
    assert live.db.get_meta("still-borrowed") == "usable"


@pytest.mark.parametrize("fault", [
    "prior-update", "prior-delete", "unexpected-key", "missing-head",
    "missing-generation", "extra-generation",
])
def test_claim_delta_oracle_rejects_unexpected_metadata_changes(live, fault):
    # Scratch-store mutations prove the oracle does not hide unrelated rows.
    live.db.set_meta("unrelated-sentinel", "preserve-exactly")
    before = rows(live.db)
    assert "result" in dispatch(live)
    assert_first_claim_delta(before, rows(live.db))
    key = {
        "prior-update": "unrelated-sentinel", "prior-delete": "unrelated-sentinel",
        "unexpected-key": "unexpected", "missing-head": "run-custody:rpc-fixture:head",
        "missing-generation": "run-custody:rpc-fixture:generation:1",
        "extra-generation": "run-custody:rpc-fixture:generation:2",
    }[fault]
    if fault in {"prior-delete", "missing-head", "missing-generation"}:
        live.db._execute_write(lambda conn: conn.execute(
            "DELETE FROM state_meta WHERE key=?", (key,),
        ))
    else:
        live.db.set_meta(key, "unexpected-value")
    with pytest.raises(AssertionError):
        assert_first_claim_delta(before, rows(live.db))


def test_claim_uses_worker_dispatch_so_file_reads_do_not_block_reader(live, monkeypatch):
    from scripts import run_checkpoint_claim as client
    entered, release = threading.Event(), threading.Event()
    original = client.claim_from_files
    def blocked(*a, **kw):
        entered.set()
        assert release.wait(8)
        return original(*a, **kw)
    monkeypatch.setattr(client, "claim_from_files", blocked)
    try:
        response = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": METHOD,
                                    "params": live.params}, live.transport)
        assert response is None, response
        assert entered.wait(5)
        # An unrelated invalid method can still be answered by the reader.
        ping = server.dispatch({"id": 2, "method": "no.such.method"}, live.transport)
        assert ping["error"]["code"] == -32601
    finally:
        release.set()
    assert live.transport.responses.get(timeout=10)["result"]["generation"] == 1


@pytest.mark.parametrize("field,value", [
    ("profile", "other"), ("db_path", "/other/state.db"), ("controller_pid", 1),
    ("lease_holder", "active-holder"), ("current_session_id", "other"),
    ("expected_generation", True), ("expected_generation", -1),
    ("expected_generation", "0"), ("ttl_seconds", True), ("ttl_seconds", 0),
    ("ttl_seconds", 3601), ("ttl_seconds", 1.5), ("ttl_seconds", float("inf")),
    ("run_id", "../other"), ("run_id", ""), ("session_id", []),
    ("origin_session_id", ""), ("expected_request_digest", "ABC"),
    ("historical_goal_digest", "z" * 64), ("request_path", "relative.json"),
    ("request_path", "\x00bad"), ("request_path", "/" + "x" * 4096),
])
def test_rpc_exact_shape_and_type_refusals_leave_native_state_unchanged(live, field, value):
    before = rows(live.db)
    refused(dispatch(live, {**live.params, field: value}), "INVALID_PARAMS")
    assert rows(live.db) == before


def test_missing_argument_is_not_defaulted(live):
    params = dict(live.params)
    del params["expected_generation"]
    refused(dispatch(live, params), "INVALID_PARAMS")
    assert live.db.read_run_custody("rpc-fixture") is None


@pytest.mark.parametrize("fault", ["db-missing", "db-closed", "db-read-only", "db-foreign",
                                    "agent-missing", "not-ready", "agent-error", "idle",
                                    "no-holder", "finalized", "cancelled", "removed", "transport"])
def test_unavailable_or_foreign_live_owner_is_refused(live, tmp_path, fault):
    before = rows(live.db)
    extra = None
    if fault == "db-missing":
        live.agent._session_db = None
    elif fault == "db-closed":
        live.db.close()
    elif fault == "db-read-only":
        extra = SessionDB(db_path=live.db.db_path, read_only=True)
        live.agent._session_db = extra
    elif fault == "db-foreign":
        extra = SessionDB(db_path=tmp_path / "foreign.db")
        extra.create_session(session_id="current", source="desktop")
        assert extra.try_acquire_session_turn_lease("current", "active-holder", ttl_seconds=120)
        extra.set_meta("goal:old", live.goal)
        live.agent._session_db = extra
    elif fault == "agent-missing":
        live.session["agent"] = None
    elif fault == "not-ready":
        live.session["agent_ready"].clear()
    elif fault == "agent-error":
        live.session["agent_error"] = "private error must not escape"
    elif fault == "idle":
        live.session["running"] = False
    elif fault == "no-holder":
        live.agent._active_session_turn_lease_holder = None
    elif fault == "finalized":
        live.session["_finalized"] = True
    elif fault == "cancelled":
        live.session["_turn_cancel_requested"] = True
    elif fault == "removed":
        server._sessions.clear()
    try:
        response = dispatch(live, transport=ReplyTransport() if fault == "transport" else None)
        assert "error" in response
        if fault != "removed":
            refused(response)
        assert "private error" not in json.dumps(response)
        assert rows(live.db) == before
        if extra:
            assert extra.read_run_custody("rpc-fixture") is None
    finally:
        if extra:
            extra.close()


@pytest.mark.parametrize("fault", ["request-session", "request-holder", "generation", "request-digest",
                                    "request-missing", "source-changed"])
def test_valid_shape_cannot_bypass_selected_agent_or_native_fence(live, fault):
    if fault == "request-session":
        live.db.create_session(session_id="other", source="desktop")
        assert live.db.try_acquire_session_turn_lease("other", "active-holder", ttl_seconds=120)
        live.request["session_id"] = "other"
        rewrite(live)
    elif fault == "request-holder":
        # Native row/request agree, but not with the selected agent's active holder.
        live.db.release_session_turn_lease("current", "active-holder")
        assert live.db.try_acquire_session_turn_lease("current", "other-holder", ttl_seconds=120)
        live.request["lease_holder"] = "other-holder"
        rewrite(live)
    elif fault == "generation":
        live.params["expected_generation"] = 2
    elif fault == "request-digest":
        live.params["expected_request_digest"] = "0" * 64
    elif fault == "request-missing":
        Path(live.params["request_path"]).unlink()
    else:
        live.source.write_bytes(b"changed")
    before = rows(live.db)
    refused(dispatch(live))
    assert rows(live.db) == before


@pytest.mark.parametrize("fault,code", [("ack", "CLAIM_OUTCOME_UNKNOWN"),
    ("readback", "CLAIM_READBACK_UNKNOWN"), ("mismatch", "CLAIM_READBACK_MISMATCH")])
def test_post_commit_uncertainty_is_not_retry_or_refusal(live, monkeypatch, fault, code):
    native_claim = live.db.claim_run_custody_checked
    native_read = live.db.read_run_custody
    calls = []
    def uncertain(*a, **kw):
        calls.append(1)
        value = native_claim(*a, **kw)
        if fault == "ack":
            raise OSError("private lost acknowledgement")
        return value
    monkeypatch.setattr(live.db, "claim_run_custody_checked", uncertain)
    if fault == "readback":
        def lost_read(*a):
            raise OSError("private readback error")
        monkeypatch.setattr(live.db, "read_run_custody", lost_read)
    elif fault == "mismatch":
        monkeypatch.setattr(live.db, "read_run_custody", lambda *a: None)
    response = dispatch(live)
    assert response["error"]["message"] == code
    assert response["error"]["data"]["status"] == "unknown"
    assert response["error"]["data"]["custody_changed"] is None
    assert response["error"]["data"]["automatic_retry"] is False
    assert "private" not in json.dumps(response)
    assert len(calls) == 1
    owner = native_read("rpc-fixture")
    assert owner.generation == 1
    assert owner.owner_token not in json.dumps(response)
    before = rows(live.db)
    # The new private handle preserves quarantine even on an explicit repeat;
    # do not perform another native mutation to rediscover the unknown outcome.
    repeated = dispatch(live)
    assert repeated["error"]["data"]["status"] == "unknown"
    assert len(calls) == 1
    assert rows(live.db) == before


def test_concurrent_rpc_claims_admit_exactly_one_generation(live, monkeypatch):
    before = rows(live.db)
    original = server._methods[METHOD]
    barrier = threading.Barrier(2)
    def race(*a, **kw):
        barrier.wait(timeout=8)
        return original(*a, **kw)
    # Meet at the RPC boundary, before the per-owner serialization lock.
    monkeypatch.setitem(server._methods, METHOD, race)
    req = {"jsonrpc": "2.0", "id": 1, "method": METHOD, "params": live.params}
    assert server.dispatch(req, live.transport) is None
    assert server.dispatch({**req, "id": 2}, live.transport) is None
    replies = [live.transport.responses.get(timeout=10) for _ in range(2)]
    assert sum("result" in r for r in replies) == 1
    refused(next(r for r in replies if "error" in r), "FENCE_MISMATCH")
    assert live.db.read_run_custody("rpc-fixture").generation == 1
    assert_first_claim_delta(before, rows(live.db))


@pytest.mark.parametrize("fault", ["session", "holder"])
def test_selected_agent_and_native_binding_change_before_admission_is_refused(live, monkeypatch, fault):
    before = rows(live.db)
    original = live.db.claim_run_custody_checked
    def changed(*a, **kw):
        if fault == "session":
            live.agent.session_id = "next"
            live.db.create_session(session_id="next", source="desktop", parent_session_id="current")
            live.db.end_session("current", "compression")
        else:
            live.agent._active_session_turn_lease_holder = "next-holder"
            live.db.release_session_turn_lease("current", "active-holder")
            assert live.db.try_acquire_session_turn_lease("current", "next-holder", ttl_seconds=120)
        return original(*a, **kw)
    monkeypatch.setattr(live.db, "claim_run_custody_checked", changed)
    refused(dispatch(live))
    assert live.db.read_run_custody("rpc-fixture") is None
    assert rows(live.db) == before


def test_launch_profile_uses_existing_launch_home_not_ambient_profile(live, monkeypatch):
    live.session["profile_home"] = None
    monkeypatch.setattr(server, "_hermes_home", str(Path(live.db.db_path).parent))
    monkeypatch.setenv("HERMES_HOME", "/unrelated/ambient/profile")
    assert dispatch(live)["result"]["generation"] == 1


def test_rpc_observes_request_bytes_once(live, monkeypatch):
    from scripts import run_checkpoint_resume as observer
    original = observer.read_file_bytes
    calls = []
    def observed(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(observer, "read_file_bytes", observed)
    assert dispatch(live)["result"]["generation"] == 1
    assert calls.count(live.params["request_path"]) == 1


@pytest.mark.parametrize("fault", ["lease", "goal", "session"])
def test_rpc_native_transaction_rechecks_after_file_observation(live, monkeypatch, fault):
    original = live.db._execute_write
    def interpose(write):
        # Separate connection commits a native binding change just before admission.
        with sqlite3.connect(live.db.db_path) as conn:
            if fault == "lease":
                conn.execute("DELETE FROM session_turn_leases")
            elif fault == "goal":
                conn.execute("UPDATE state_meta SET value='changed' WHERE key='goal:old'")
            else:
                conn.execute("UPDATE sessions SET ended_at=1,end_reason='compression' WHERE id='current'")
        return original(write)
    monkeypatch.setattr(live.db, "_execute_write", interpose)
    refused(dispatch(live))
    assert live.db.read_run_custody("rpc-fixture") is None
