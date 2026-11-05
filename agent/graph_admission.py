"""Private Graph admission transport backed by the real session owner.

Opaque references are short-lived capabilities to ASK the owner, never cached
permission. This module is not enabled by the MCP adapter until the daemon has
qualified launch and provider consumers. Same-UID arbitrary shell/SDK execution
is outside this boundary; the private socket is not an OS sandbox.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import struct
import threading
import time

from agent.orchestration_policy import OrchestrationError
from agent.orchestration_runtime import SpawnAdmission, admit_worker

SCHEMA = "ares.graph-admission/v1"
_MAX_FRAME = 65536
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _identifier(value):
    return (type(value) is str and 0 < len(value) <= 1024
            and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value))


def _error(code):
    return {"schema": SCHEMA, "ok": False, "code": code}


@dataclass(frozen=True)
class GraphAdmissionScope:
    reference: str = field(repr=False)
    graph_id: str
    graph_version: str
    request_digest: str
    _route: tuple[str, str, str] = field(repr=False)

    @property
    def execution_route(self):
        return dict(zip(("provider", "model", "reasoning_effort"), self._route))

    def metadata(self):
        return {"schema": SCHEMA, "reference": self.reference,
                "graph_id": self.graph_id, "graph_version": self.graph_version,
                "request_digest": self.request_digest,
                "execution_route": self.execution_route}


@dataclass
class _Entry:
    scope: GraphAdmissionScope
    admission: SpawnAdmission
    expires_at: float
    run_id: str | None = None
    invocations: set[str] = field(default_factory=set)


class GraphAdmissionService:
    """Bounded ephemeral reference lookup; SessionDB alone owns effect claims."""

    def __init__(self, *, clock=time.monotonic, max_scopes=256, max_invocations=1024):
        if (type(max_scopes) is not int or not 1 <= max_scopes <= 4096
                or type(max_invocations) is not int or not 1 <= max_invocations <= 65536):
            raise OrchestrationError("graph_admission_limits_invalid")
        self._clock = clock
        self._max_scopes = max_scopes
        self._max_invocations = max_invocations
        self._entries = {}
        self._lock = threading.Lock()

    def issue(self, parent, *, graph_id, graph_version, request_digest, ttl_seconds=300):
        if (not _identifier(graph_id) or type(graph_version) is not str
                or not _DIGEST.fullmatch(graph_version) or type(request_digest) is not str
                or not _DIGEST.fullmatch(request_digest)
                or type(ttl_seconds) not in (int, float) or not math.isfinite(ttl_seconds)
                or not 0 < ttl_seconds <= 3600):
            raise OrchestrationError("graph_admission_request_invalid")
        if (type(getattr(parent, "_delegate_depth", None)) is not int
                or parent._delegate_depth != 0
                or getattr(parent, "_orchestration_admission", None) is not None):
            raise OrchestrationError("graph_nested_worker_unqualified")
        admission = admit_worker(parent, effect="graph")
        route = admission.resolved_optional_route
        if (route is None or route.provider != "openai-codex"
                or not _identifier(route.model) or route.service_tier is not None
                or route.reasoning_effort not in {"minimal", "low", "medium", "high", "xhigh"}):
            raise OrchestrationError("graph_route_unqualified")
        # Issuance is not an attempt and grants no permission to start work.
        scope = GraphAdmissionScope(secrets.token_urlsafe(32), graph_id, graph_version,
                                    request_digest, (route.provider, route.model, route.reasoning_effort))
        with self._lock:
            now = self._clock()
            self._entries = {k: v for k, v in self._entries.items() if now < v.expires_at}
            if len(self._entries) >= self._max_scopes:
                raise OrchestrationError("graph_admission_capacity")
            self._entries[scope.reference] = _Entry(scope, admission, now + ttl_seconds)
        return scope

    def retire(self, scope):
        with self._lock:
            entry = self._entries.get(scope.reference)
            if entry is not None and entry.scope is scope:
                del self._entries[scope.reference]

    def authorize(self, request):
        """Every accepted callback commits a fresh, non-replayable owner claim.

        The lock serializes one ticket's bind/claim. A lost response, owner crash,
        or broker restart is NOT replay permission. No lock spans provider work.
        """
        if type(request) is not dict:
            return _error("graph_admission_request_invalid")
        stage = request.get("stage")
        keys = {"schema", "reference", "stage", "graph_id", "graph_version",
                "request_digest", "execution_route", "run_id"}
        if stage == "provider":
            keys |= {"node_id", "invocation_ref"}
        if (type(stage) is not str or stage not in {"launch", "provider"} or request.keys() != keys
                or request.get("schema") != SCHEMA
                or not all(_identifier(request.get(k)) for k in ("reference", "run_id"))
                or (stage == "provider" and not all(
                    _identifier(request.get(k)) for k in ("node_id", "invocation_ref")))):
            return _error("graph_admission_request_invalid")
        with self._lock:
            entry = self._entries.get(request["reference"])
            if entry is None:
                return _error("graph_admission_unknown")
            if self._clock() >= entry.expires_at:
                del self._entries[request["reference"]]
                return _error("graph_admission_expired")
            scope = entry.scope
            if any(request[k] != v for k, v in (
                ("graph_id", scope.graph_id), ("graph_version", scope.graph_version),
                ("request_digest", scope.request_digest), ("execution_route", scope.execution_route),
            )):
                return _error("graph_admission_binding_mismatch")
            if stage == "launch":
                if entry.run_id is not None:
                    return _error("graph_admission_replay")
            else:
                if entry.run_id is None:
                    return _error("graph_admission_launch_required")
                if request["run_id"] != entry.run_id:
                    return _error("graph_admission_binding_mismatch")
                if request["invocation_ref"] in entry.invocations:
                    return _error("graph_admission_replay")
                if len(entry.invocations) >= self._max_invocations:
                    return _error("graph_admission_capacity")
            try:
                entry.admission.assert_current(effect="graph")
                claim = entry.admission._owner.claim_orchestration_effect(
                    entry.admission.session_id, expected_revision=entry.admission.revision,
                    effect="graph", route=scope.execution_route,
                    config_fingerprint=entry.admission.config_fingerprint)
            except OrchestrationError as exc:
                # Even a post-commit owner failure must not turn a retry into a
                # second claim. Losing availability is preferable to widening.
                self._entries.pop(scope.reference, None)
                return _error(exc.code)
            except Exception:
                self._entries.pop(scope.reference, None)
                return _error("graph_admission_owner_unavailable")
            if stage == "launch":
                entry.run_id = request["run_id"]
            else:
                entry.invocations.add(request["invocation_ref"])
            return {"schema": SCHEMA, "ok": True, "admission": claim}


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def _read_exact(stream, length, deadline):
    parts = bytearray()
    while len(parts) < length:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("frame deadline")
        stream.settimeout(remaining)
        data = stream.recv(length - len(parts))
        if not data:
            raise ValueError("truncated frame")
        parts.extend(data)
    return bytes(parts)


def _read_frame(stream):
    # One wall-clock budget covers BOTH header and body, not a sliding timeout
    # that a peer could extend indefinitely by trickling individual bytes.
    deadline = time.monotonic() + 1
    length, = struct.unpack("!I", _read_exact(stream, 4, deadline))
    if not 0 < length <= _MAX_FRAME:
        raise ValueError("frame limit")
    return json.loads(_read_exact(stream, length, deadline), object_pairs_hook=_unique_object)


def _write_frame(stream, value):
    data = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    if len(data) > _MAX_FRAME:
        raise ValueError("frame limit")
    stream.sendall(struct.pack("!I", len(data)) + data)


def _same_uid(stream):
    _, uid, _ = struct.unpack("3i", stream.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid != os.getuid():
        raise ValueError("peer identity")


class GraphAdmissionSocket:
    """Linux peer-credentialled, bounded one-frame-per-connection IPC owner."""

    def __init__(self, service, directory):
        self._service = service
        self._directory = Path(directory)
        self.endpoint = self._directory / "authority.sock"
        # Linux sockaddr_un has 108 bytes including the NUL terminator. Never
        # silently relocate an authority endpoint to a different directory.
        if len(os.fsencode(self.endpoint)) >= 108:
            raise OrchestrationError("graph_admission_endpoint_too_long")
        self._stop = threading.Event()
        self._thread = None
        self._identity = None
        self._socket = None
        try:
            self._directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = self._directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
                or info.st_uid != os.getuid()):
            raise OrchestrationError("graph_admission_directory_unqualified")
        if self.endpoint.exists() or self.endpoint.is_symlink():
            raise OrchestrationError("graph_admission_endpoint_occupied")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.endpoint))
            info = self.endpoint.lstat()
            self._identity = (info.st_dev, info.st_ino)
            self._socket = server
            self.endpoint.chmod(0o600)
            server.listen(16)
            server.settimeout(0.2)
        except Exception:
            server.close()
            self._unlink_owned_endpoint()
            raise

    def __enter__(self):
        if self._thread is not None:
            raise OrchestrationError("graph_admission_socket_already_started")
        self._thread = threading.Thread(target=self._serve, name="graph-admission", daemon=True)
        self._thread.start()
        return self

    def _serve(self):
        while not self._stop.is_set():
            try:
                stream, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with stream:
                stream.settimeout(1)
                try:
                    _same_uid(stream)
                    reply = self._service.authorize(_read_frame(stream))
                except (ValueError, TypeError, OSError, RecursionError):
                    reply = _error("graph_admission_request_invalid")
                try:
                    _write_frame(stream, reply)
                except (ValueError, OSError):
                    pass  # lost reply cannot roll back or replay an owner claim

    def _unlink_owned_endpoint(self):
        if self._identity is not None:
            try:
                info = self.endpoint.lstat()
                if (info.st_dev, info.st_ino) == self._identity and stat.S_ISSOCK(info.st_mode):
                    self.endpoint.unlink()
            except FileNotFoundError:
                pass

    def __exit__(self, *exc):
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
        if self._thread is not None:
            self._thread.join(timeout=3)
            if self._thread.is_alive():
                raise OrchestrationError("graph_admission_shutdown_unqualified")
        self._unlink_owned_endpoint()


def call_graph_admission(endpoint, request):
    """Strict client for local consumers; transport failures are typed denials."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(endpoint))
            _same_uid(client)
            _write_frame(client, request)
            reply = _read_frame(client)
            if (type(reply) is not dict or reply.get("schema") != SCHEMA
                    or type(reply.get("ok")) is not bool):
                raise ValueError("invalid reply")
            return reply
    except (ValueError, TypeError, OSError, RecursionError):
        return _error("graph_admission_transport_unavailable")
