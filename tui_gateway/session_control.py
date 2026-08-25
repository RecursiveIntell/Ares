from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class ControllerLease:
    host_id: str
    profile: str
    session_id: str
    runtime_id: str
    principal_id: str
    controller_instance_id: str
    generation: int
    fencing_token: str
    expires_at: float


class ControllerLeaseError(RuntimeError):
    pass


class SessionController:
    """Process-local controller arbitration; authority remains the gateway."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._leases: dict[tuple[str, str, str, str], ControllerLease] = {}
        self._generations: dict[tuple[str, str, str, str], int] = {}

    @staticmethod
    def _scope(host_id: str, profile: str, session_id: str, runtime_id: str):
        return (str(host_id), str(profile), str(session_id), str(runtime_id))

    def _next_generation(self, scope) -> int:
        generation = self._generations.get(scope, 0) + 1
        self._generations[scope] = generation
        return generation

    def acquire(
        self,
        *,
        host_id: str,
        profile: str,
        session_id: str,
        runtime_id: str,
        principal_id: str,
        controller_instance_id: str,
        ttl_seconds: float,
    ) -> ControllerLease:
        ttl = max(1.0, float(ttl_seconds))
        scope = self._scope(host_id, profile, session_id, runtime_id)
        now = time.monotonic()
        with self._lock:
            current = self._leases.get(scope)
            if current is not None and current.expires_at > now:
                if (
                    current.principal_id == principal_id
                    and current.controller_instance_id == controller_instance_id
                ):
                    return current
                raise ControllerLeaseError("controller lease held by another principal")
            generation = self._next_generation(scope)
            lease = ControllerLease(
                host_id=scope[0],
                profile=scope[1],
                session_id=scope[2],
                runtime_id=scope[3],
                principal_id=str(principal_id),
                controller_instance_id=str(controller_instance_id),
                generation=generation,
                fencing_token=uuid.uuid4().hex,
                expires_at=now + ttl,
            )
            self._leases[scope] = lease
            return lease

    def status(self, *, host_id: str, profile: str, session_id: str, runtime_id: str):
        scope = self._scope(host_id, profile, session_id, runtime_id)
        with self._lock:
            lease = self._leases.get(scope)
            if lease is not None and lease.expires_at <= time.monotonic():
                self._leases.pop(scope, None)
                lease = None
            return lease

    def validate(
        self,
        *,
        host_id: str,
        profile: str,
        session_id: str,
        runtime_id: str,
        principal_id: str,
        controller_instance_id: str,
        generation: int,
        fencing_token: str,
    ) -> ControllerLease:
        current = self.status(
            host_id=host_id,
            profile=profile,
            session_id=session_id,
            runtime_id=runtime_id,
        )
        if current is None:
            raise ControllerLeaseError("controller lease missing or expired")
        if current.generation != generation or current.fencing_token != fencing_token:
            raise ControllerLeaseError("stale controller fencing generation")
        if current.principal_id != principal_id or current.controller_instance_id != controller_instance_id:
            raise ControllerLeaseError("controller principal is not lease holder")
        return current

    def renew(
        self,
        *,
        host_id: str,
        profile: str,
        session_id: str,
        runtime_id: str,
        principal_id: str,
        controller_instance_id: str,
        generation: int,
        fencing_token: str,
        ttl_seconds: float,
    ) -> ControllerLease:
        """Extend only the current holder's lease without changing its fence."""
        ttl = max(1.0, float(ttl_seconds))
        scope = self._scope(host_id, profile, session_id, runtime_id)
        now = time.monotonic()
        with self._lock:
            current = self._leases.get(scope)
            if current is None or current.expires_at <= now:
                self._leases.pop(scope, None)
                raise ControllerLeaseError("controller lease missing or expired")
            if current.generation != generation or current.fencing_token != fencing_token:
                raise ControllerLeaseError("stale controller fencing generation")
            if current.principal_id != principal_id or current.controller_instance_id != controller_instance_id:
                raise ControllerLeaseError("controller principal is not lease holder")
            renewed = replace(current, expires_at=now + ttl)
            self._leases[scope] = renewed
            return renewed

    def release(self, lease: ControllerLease) -> bool:
        scope = self._scope(lease.host_id, lease.profile, lease.session_id, lease.runtime_id)
        with self._lock:
            current = self._leases.get(scope)
            if current != lease:
                return False
            self._leases.pop(scope, None)
            return True

    def handoff(
        self,
        *,
        lease: ControllerLease,
        principal_id: str,
        controller_instance_id: str,
        ttl_seconds: float,
    ) -> ControllerLease:
        self.validate(
            host_id=lease.host_id,
            profile=lease.profile,
            session_id=lease.session_id,
            runtime_id=lease.runtime_id,
            principal_id=lease.principal_id,
            controller_instance_id=lease.controller_instance_id,
            generation=lease.generation,
            fencing_token=lease.fencing_token,
        )
        self.release(lease)
        return self.acquire(
            host_id=lease.host_id,
            profile=lease.profile,
            session_id=lease.session_id,
            runtime_id=lease.runtime_id,
            principal_id=principal_id,
            controller_instance_id=controller_instance_id,
            ttl_seconds=ttl_seconds,
        )
