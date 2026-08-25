"""Capability-gated server-owned session controller RPCs."""

from __future__ import annotations

import types

from .method_ctx import HandlerRegistry
from .session_control import ControllerLeaseError

_registry = HandlerRegistry()
method = _registry.method


def _control_capability_error(rid) -> dict:
    return _err(
        rid,
        4401,
        "mobile capability is not enabled: session_control_lease_v1",
        {"capability": "session_control_lease_v1", "retry": "refresh_capabilities"},
    )


def _control_identity(rid):
    transport = current_transport()
    identity = getattr(transport, "auth_identity", None) if transport is not None else None
    principal = str((identity or {}).get("user_id") or "").strip()
    if principal:
        return principal, None
    peer = (
        getattr(transport, "peer", None)
        or getattr(transport, "_peer", "")
        if transport is not None
        else ""
    )
    peer = str(peer or "")
    if peer.startswith(("127.0.0.1:", "[::1]:", "::1:")):
        return "loopback", None
    return None, _err(rid, 4403, "authenticated controller identity required")


def _control_scope(rid, params: dict):
    host_id = str(params.get("host_id") or "").strip()
    profile = str(params.get("profile") or "").strip()
    session_id = str(params.get("session_id") or "").strip()
    runtime_id = str(params.get("runtime_id") or protocol_runtime_id()).strip()
    if not host_id or not profile or not session_id or not runtime_id:
        return None, _err(rid, 4002, "host_id, profile, session_id, and runtime_id are required")
    if host_id != host_identity_digest():
        return None, _err(rid, 4403, "controller host identity mismatch", {"reason": "wrong_host"})
    with _sessions_lock:
        session = _sessions.get(session_id)
        if session is None:
            return None, _err(rid, 4404, "session is not available for control")
        actual_profile = str(session.get("profile") or "default")
    if actual_profile != profile:
        return None, _err(rid, 4403, "controller profile mismatch", {"reason": "wrong_profile"})
    return (host_id, profile, session_id, runtime_id), None


def _control_lease_dict(lease) -> dict:
    return {
        "host_id": lease.host_id,
        "profile": lease.profile,
        "session_id": lease.session_id,
        "runtime_id": lease.runtime_id,
        "principal_id": lease.principal_id,
        "controller_instance_id": lease.controller_instance_id,
        "generation": lease.generation,
        "fencing_token": lease.fencing_token,
        "expires_at": lease.expires_at,
    }


@method("session.control.acquire")
def _(rid, params: dict) -> dict:
    if not mobile_protocol_capability_enabled("session_control_lease_v1"):
        return _control_capability_error(rid)
    principal, error = _control_identity(rid)
    if error is not None:
        return error
    scope, error = _control_scope(rid, params)
    if error is not None:
        return error
    instance = str(params.get("controller_instance_id") or "").strip()
    if not instance:
        return _err(rid, 4002, "controller_instance_id required")
    try:
        ttl = float(params.get("ttl_seconds", 60))
        lease = _session_controller.acquire(
            host_id=scope[0],
            profile=scope[1],
            session_id=scope[2],
            runtime_id=scope[3],
            principal_id=principal,
            controller_instance_id=instance,
            ttl_seconds=ttl,
        )
    except (TypeError, ValueError):
        return _err(rid, 4002, "ttl_seconds must be numeric")
    except ControllerLeaseError as exc:
        return _err(rid, 4409, str(exc), {"reason": "lease_held"})
    return _ok(rid, {"lease": _control_lease_dict(lease)})


@method("session.control.status")
def _(rid, params: dict) -> dict:
    if not mobile_protocol_capability_enabled("session_control_lease_v1"):
        return _control_capability_error(rid)
    _principal, error = _control_identity(rid)
    if error is not None:
        return error
    scope, error = _control_scope(rid, params)
    if error is not None:
        return error
    lease = _session_controller.status(
        host_id=scope[0], profile=scope[1], session_id=scope[2], runtime_id=scope[3]
    )
    return _ok(rid, {"lease": _control_lease_dict(lease) if lease is not None else None})


@method("session.control.renew")
def _(rid, params: dict) -> dict:
    if not mobile_protocol_capability_enabled("session_control_lease_v1"):
        return _control_capability_error(rid)
    principal, error = _control_identity(rid)
    if error is not None:
        return error
    scope, error = _control_scope(rid, params)
    if error is not None:
        return error
    try:
        ttl = float(params.get("ttl_seconds", 60))
        lease = _session_controller.renew(
            host_id=scope[0],
            profile=scope[1],
            session_id=scope[2],
            runtime_id=scope[3],
            principal_id=principal,
            controller_instance_id=str(params.get("controller_instance_id") or ""),
            generation=int(params.get("generation")),
            fencing_token=str(params.get("fencing_token") or ""),
            ttl_seconds=ttl,
        )
    except (TypeError, ValueError):
        return _err(rid, 4002, "renewal lease fields and ttl_seconds must be valid")
    except ControllerLeaseError as exc:
        return _err(rid, 4410, str(exc), {"reason": "stale_or_missing_lease"})
    return _ok(rid, {"lease": _control_lease_dict(lease)})


@method("session.control.release")
def _(rid, params: dict) -> dict:
    if not mobile_protocol_capability_enabled("session_control_lease_v1"):
        return _control_capability_error(rid)
    principal, error = _control_identity(rid)
    if error is not None:
        return error
    scope, error = _control_scope(rid, params)
    if error is not None:
        return error
    try:
        lease = _session_controller.validate(
            host_id=scope[0], profile=scope[1], session_id=scope[2], runtime_id=scope[3],
            principal_id=principal,
            controller_instance_id=str(params.get("controller_instance_id") or ""),
            generation=int(params.get("generation")),
            fencing_token=str(params.get("fencing_token") or ""),
        )
    except (TypeError, ValueError, ControllerLeaseError) as exc:
        return _err(rid, 4410, str(exc), {"reason": "stale_or_missing_lease"})
    return _ok(rid, {"released": _session_controller.release(lease)})


def register(server) -> None:
    for helper in (_control_capability_error, _control_identity, _control_scope, _control_lease_dict):
        real = types.FunctionType(
            helper.__code__, vars(server), helper.__name__, helper.__defaults__, helper.__closure__
        )
        real.__kwdefaults__ = helper.__kwdefaults__
        real.__doc__ = helper.__doc__
        setattr(server, helper.__name__, real)
    _registry.install(server)
