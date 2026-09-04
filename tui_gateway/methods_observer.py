"""Read-only mobile observer/snapshot RPCs.

The handlers are capability-gated and deliberately do not call session.resume,
replace a session transport, or create a second session/database owner.
"""

from __future__ import annotations

import copy
import types

from .method_ctx import HandlerRegistry

_registry = HandlerRegistry()
method = _registry.method


def _forbidden(rid, message: str, *, data: dict | None = None) -> dict:
    return _err(rid, 4403, message, data)


def _capability_error(rid, capability: str) -> dict:
    return _err(
        rid,
        4401,
        f"mobile capability is not enabled: {capability}",
        {"capability": capability, "retry": "refresh_capabilities"},
    )


def _require_transport(rid):
    transport = current_transport()
    identity = getattr(transport, "auth_identity", None) if transport is not None else None
    if identity:
        return transport, None
    # The local dev/G1 backend is explicitly loopback-bound. When the auth gate
    # is disabled for loopback, retain a typed local observer path without
    # pretending it is a remotely authenticated principal. Remote bind/auth
    # promotion remains blocked until host identity and enrollment exist.
    peer = (
        getattr(transport, "peer", None)
        or getattr(transport, "_peer", "")
        if transport is not None
        else ""
    )
    peer = str(peer or "")
    if peer.startswith(("127.0.0.1:", "[::1]:", "::1:")):
        return transport, None
    return None, _forbidden(rid, "authenticated observer identity required")


def _session_for_request(rid, params: dict):
    session_id = str(params.get("session_id") or "").strip()
    requested_host = str(params.get("host_id") or "").strip()
    if not session_id:
        return None, _err(rid, 4002, "session_id required")
    if not requested_host:
        return None, _err(rid, 4002, "host_id required")
    if requested_host != host_identity_digest():
        return None, _forbidden(
            rid,
            "session host does not match the requested owner scope",
            data={"reason": "wrong_host"},
        )
    with _sessions_lock:
        session = _sessions.get(session_id)
        if session is None:
            return None, _err(rid, 4404, "session is not available for observation")
        profile = str(session.get("profile") or "default")
        requested_profile = str(params.get("profile") or profile).strip()
        if requested_profile != profile:
            return None, _forbidden(
                rid,
                "session profile does not match the requested owner scope",
                data={"reason": "wrong_profile"},
            )
        projection = {
            "history": copy.deepcopy(session.get("history") if isinstance(session.get("history"), list) else []),
            "pending_requests": copy.deepcopy(
                session.get("pending_requests") if isinstance(session.get("pending_requests"), list) else []
            ),
            "controller": copy.deepcopy(
                session.get("controller") if isinstance(session.get("controller"), dict) else {"state": "none"}
            ),
            "profile": profile,
            "connection_id": str(session.get("connection_id") or "local"),
            "lineage_root_id": session.get("lineage_root_id") or session.get("_lineage_root_id"),
            "session_key": session.get("session_key"),
            "running": bool(session.get("running")),
        }
        return (session_id, profile, projection), None


def _scope(session_id: str, profile: str, session: dict) -> dict:
    return {
        "host_id": host_identity_digest(),
        "connection_id": str(session.get("connection_id") or "local"),
        "profile": profile,
        "session_id": session_id,
        "lineage_root_id": str(
            session.get("lineage_root_id")
            or session.get("_lineage_root_id")
            or session.get("session_key")
            or session_id
        ),
        "runtime_id": protocol_runtime_id(),
    }


def _revision(session_id: str, profile: str) -> int:
    with _session_event_lock:
        return int(_session_event_revisions.get((profile, session_id, protocol_runtime_id()), 0))  # type: ignore[reportUndefinedVariable]


@method("session.snapshot")
def _(rid, params: dict) -> dict:
    if not mobile_protocol_capability_enabled("session_snapshot_v1"):
        return _capability_error(rid, "session_snapshot_v1")
    _transport, error = _require_transport(rid)
    if error is not None:
        return error
    selected, error = _session_for_request(rid, params)
    if error is not None:
        return error
    session_id, profile, session = selected
    history = session.get("history")
    if not isinstance(history, list):
        history = []
    pending = session.get("pending_requests")
    if not isinstance(pending, list):
        pending = []
    controller = session.get("controller")
    if not isinstance(controller, dict):
        controller = {"state": "none"}
    status = "running" if session.get("running") else ("waiting_for_user" if pending else "idle")
    snapshot = {
        "scope": _scope(session_id, profile, session),
        "schema_version": 1,
        "revision": _revision(session_id, profile),
        "status": status,
        "transcript_tail": copy.deepcopy(history[-200:]),
        "transcript_cursor": None,
        "pending_requests": copy.deepcopy(pending),
        "controller": copy.deepcopy(controller),
    }
    return _ok(rid, {"snapshot": snapshot})


@method("session.mobile.list")
def _(rid, params: dict) -> dict:
    """List canonical server sessions as route-qualified mobile projections."""
    if not mobile_protocol_capability_enabled("mobile_surface_v1"):
        return _capability_error(rid, "mobile_surface_v1")
    _transport, error = _require_transport(rid)
    if error is not None:
        return error
    requested_host = str(params.get("host_id") or "").strip()
    if requested_host != host_identity_digest():
        return _forbidden(rid, "session list host does not match the requested owner scope", data={"reason": "wrong_host"})
    profile = str(params.get("profile") or "default").strip() or "default"
    try:
        limit = max(1, min(int(params.get("limit", 200)), 200))
    except (TypeError, ValueError):
        return _err(rid, 4002, "limit must be an integer")
    with _profile_db({"profile": profile}) as db:
        if db is None:
            return _err(rid, 5006, "session database unavailable")
        try:
            rows = db.list_sessions_rich(
                source=None,
                limit=limit,
                order_by_last_active=True,
                compact_rows=True,
                include_hidden=False,
            )
        except Exception as exc:
            return _err(rid, 5006, str(exc))
    sessions = []
    for row in rows:
        session_id = str(row.get("id") or "").strip()
        if not session_id:
            continue
        live = _sessions.get(session_id)
        sessions.append(
            {
                "scope": {
                    "host_id": requested_host,
                    "connection_id": str((live or {}).get("connection_id") or "local"),
                    "profile": profile,
                    "session_id": session_id,
                    "lineage_root_id": str(
                        row.get("lineage_root_id")
                        or row.get("session_key")
                        or session_id
                    ),
                    "runtime_id": protocol_runtime_id(),
                },
                "title": str(row.get("title") or ""),
                "preview": str(row.get("preview") or ""),
                "message_count": max(0, int(row.get("message_count") or 0)),
                "started_at": float(row.get("started_at") or 0),
                "status": "running" if (live or {}).get("running") else "idle",
            }
        )
    return _ok(rid, {"sessions": sessions, "runtime_id": protocol_runtime_id()})


@method("session.observe")
def _(rid, params: dict) -> dict:
    if not mobile_protocol_capability_enabled("session_observe_v1"):
        return _capability_error(rid, "session_observe_v1")
    transport, error = _require_transport(rid)
    if error is not None:
        return error
    selected, error = _session_for_request(rid, params)
    if error is not None:
        return error
    session_id, profile, session = selected
    runtime_id = protocol_runtime_id()
    requested_runtime_id = str(params.get("runtime_id") or "").strip()
    try:
        raw_last_seen = params.get("last_seen_revision", 0)
        if isinstance(raw_last_seen, bool):
            raise ValueError
        last_seen_revision = int(raw_last_seen)
        if last_seen_revision < 0:
            raise ValueError
        current_revision = _revision(session_id, profile)
        snapshot_required = requested_runtime_id != "" and requested_runtime_id != runtime_id
        snapshot_required = snapshot_required or last_seen_revision != current_revision
        record = observer_registry.register(
            session_id=session_id,
            profile=profile,
            runtime_id=runtime_id,
            transport=transport,
            last_seen_revision=last_seen_revision,
        )
    except (TypeError, ValueError):
        return _err(rid, 4002, "last_seen_revision must be a non-negative integer")
    except RuntimeError:
        return _err(rid, 4409, "observer capacity exceeded", {"retry": "snapshot_then_retry"})
    return _ok(
        rid,
        {
            "observing": True,
            "session_id": session_id,
            "profile": profile,
            "runtime_id": runtime_id,
            "current_revision": current_revision,
            "last_seen_revision": record.last_seen_revision,
            "snapshot_required": snapshot_required,
            "transport_rebound": False,
        },
    )


@method("session.unobserve")
def _(rid, params: dict) -> dict:
    if not mobile_protocol_capability_enabled("session_observe_v1"):
        return _capability_error(rid, "session_observe_v1")
    transport, error = _require_transport(rid)
    if error is not None:
        return error
    selected, error = _session_for_request(rid, params)
    if error is not None:
        return error
    assert selected is not None
    session_id, profile, _session = selected
    removed = observer_registry.unregister(
        session_id=session_id,
        profile=profile,
        runtime_id=protocol_runtime_id(),
        transport=transport,
    )
    return _ok(rid, {"unobserved": True, "removed": removed, "session_id": session_id})


def register(server) -> None:
    # HandlerRegistry rebinds handler globals to server.py. Rebind the small
    # helper layer as well so helpers see the same canonical server globals.
    for helper in (
        _forbidden,
        _capability_error,
        _require_transport,
        _session_for_request,
        _scope,
        _revision,
    ):
        real = types.FunctionType(
            helper.__code__, vars(server), helper.__name__, helper.__defaults__, helper.__closure__
        )
        real.__kwdefaults__ = helper.__kwdefaults__
        real.__doc__ = helper.__doc__
        setattr(server, helper.__name__, real)
    _registry.install(server)
