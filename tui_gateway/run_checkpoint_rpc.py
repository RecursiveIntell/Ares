"""Checkpoint RPC routing and live-owner checks on the existing gateway."""
from pathlib import Path

from agent.run_checkpoint_custody import TurnRunCustody
from hermes_state import SessionDB
from hermes_state_runs import RunCustodyError, RunTaskBinding, _digest, _integer, _run_id, _ttl
from scripts.run_checkpoint_claim import ClaimOutcomeUnknown, ClaimRefusal


def handle(server, rid, params, operation):
    def refuse(code, rpc_code=-32040):
        return server._err(rid, rpc_code, code, {"status": "refused", "custody_changed": False,
            "automatic_retry": False, "resume_authorized": False, "downstream_effects_executed": False})

    def unknown(code):
        return server._err(rid, -32041, code, {"status": "unknown", "custody_changed": None,
            "automatic_retry": False, "resume_authorized": False, "downstream_effects_executed": False})

    fields = {"session_id", "run_id", "expected_generation"}
    if operation == "claim":
        fields |= {"request_path", "expected_request_digest", "origin_session_id", "ttl_seconds"}
        v2 = type(params) is dict and "task_binding" in params
        fields |= {"task_binding", "expected_control_digest"} if v2 else {"historical_goal_digest"}
    elif operation == "refresh":
        fields.add("ttl_seconds")
    elif operation != "release":
        return refuse("INVALID_PARAMS", -32602)
    if type(params) is not dict or set(params) != fields:
        return refuse("INVALID_PARAMS", -32602)
    try:
        _run_id(params["run_id"])
        _integer(params["expected_generation"], 0 if operation == "claim" else 1)
        if operation != "release":
            _ttl(params["ttl_seconds"])
        for name in ("session_id", "origin_session_id") if operation == "claim" else ("session_id",):
            value = params[name]
            if type(value) is not str or not value.strip() or len(value) > 256 or "\x00" in value:
                return refuse("INVALID_PARAMS", -32602)
        if operation == "claim":
            _digest(params["expected_request_digest"])
            if v2:
                RunTaskBinding.from_dict(params["task_binding"])
                _digest(params["expected_control_digest"])
            else:
                _digest(params["historical_goal_digest"])
            path = params["request_path"]
            if type(path) is not str or not path or len(path) > 4096 or "\x00" in path or not Path(path).is_absolute():
                return refuse("INVALID_PARAMS", -32602)
    except RunCustodyError:
        return refuse("INVALID_PARAMS", -32602)

    session, error = server._sess_nowait(params, rid)
    if error:
        return error
    transport, selected = server._current_session_steer_authority(params["session_id"])
    if transport is None or selected is not session:
        return refuse("LIVE_SESSION_MISMATCH")
    if session.get("_finalized") or session.get("_turn_cancel_requested") or session.get("running") is not True:
        return refuse("ACTIVE_AGENT_REQUIRED")

    if server._session_uses_compute_host(session):
        supervisor = server._compute_host_supervisor
        if supervisor is None or not session.get("_compute_host_active"):
            return refuse("ACTIVE_COMPUTE_HOST_REQUIRED")
        route = "session.run_checkpoint." + operation
        try:
            ack = supervisor.control(params["session_id"], route_name=route,
                payload={"type": "control", "params": dict(params)}, wait=True, timeout=30.0)
        except Exception:
            return unknown("COMPUTE_HOST_OUTCOME_UNKNOWN")
        if (type(ack) is not dict or ack.get("type") != "control.ack" or
                ack.get("sid") != params["session_id"] or ack.get("route_name") != route or
                type(ack.get("response")) is not dict):
            return unknown("COMPUTE_HOST_READBACK_UNKNOWN")
        response = ack["response"]
        if set(response) not in ({"jsonrpc", "id", "result"}, {"jsonrpc", "id", "error"}):
            return unknown("COMPUTE_HOST_READBACK_UNKNOWN")
        return {**response, "id": rid}

    agent = session.get("agent")
    ready = session.get("agent_ready")
    # Directly initialized host sessions have no deferred-build Event. The
    # actual turn-owned lifecycle and native lease, not an absent flag, bind them.
    if agent is None or (ready is not None and not ready.is_set()) or session.get("agent_error"):
        return refuse("ACTIVE_AGENT_REQUIRED")
    sid = getattr(agent, "session_id", None)
    holder = getattr(agent, "_active_session_turn_lease_holder", None)
    if type(sid) is not str or not sid or type(holder) is not str or not holder:
        return refuse("ACTIVE_AGENT_REQUIRED")
    db = getattr(agent, "_session_db", None)
    if not isinstance(db, SessionDB) or db.read_only or db._conn is None or db._read_conns_closed:
        return refuse("WRITABLE_OWNER_REQUIRED")
    try:
        if Path(db.db_path).resolve(strict=True) != (server._session_home(session) / "state.db").resolve(strict=True):
            return refuse("OWNER_STORE_MISMATCH")
    except (OSError, ValueError, RuntimeError):
        return refuse("OWNER_STORE_MISMATCH")
    owner = getattr(agent, "_run_checkpoint_custody", None)
    if not isinstance(owner, TurnRunCustody) or owner.db is not db:
        return refuse("TURN_CUSTODY_OWNER_REQUIRED")
    args = {key: value for key, value in params.items() if key != "session_id"}
    try:
        if operation == "claim":
            result = owner.claim(holder, session_id=sid, **args)
        elif operation == "refresh":
            result = owner.refresh(holder, **args)
        else:
            result = owner.release(holder, **args)
    except ClaimRefusal as exc:
        return refuse(str(exc))
    except ClaimOutcomeUnknown as exc:
        return unknown(str(exc))
    return server._ok(rid, result)
