from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class ObserverRecord:
    session_id: str
    profile: str
    runtime_id: str
    transport: Any
    last_seen_revision: int
    registered_at: float


class ObserverRegistry:
    """Bounded in-memory observer projection; never owns session lifecycle."""

    def __init__(self, *, max_observers_per_session: int = 8) -> None:
        self._lock = threading.RLock()
        self._records: dict[tuple[str, str, str], dict[int, ObserverRecord]] = {}
        self._max_observers_per_session = max(1, int(max_observers_per_session))

    def register(
        self,
        *,
        session_id: str,
        profile: str,
        runtime_id: str,
        transport: Any,
        last_seen_revision: int = 0,
    ) -> ObserverRecord:
        key = (str(profile), str(session_id), str(runtime_id))
        transport_key = id(transport)
        with self._lock:
            peers = self._records.setdefault(key, {})
            record = peers.get(transport_key)
            if record is None and len(peers) >= self._max_observers_per_session:
                raise RuntimeError("observer capacity exceeded")
            if record is None:
                record = ObserverRecord(
                    session_id=session_id,
                    profile=str(profile),
                    runtime_id=runtime_id,
                    transport=transport,
                    last_seen_revision=max(0, int(last_seen_revision)),
                    registered_at=time.time(),
                )
                peers[transport_key] = record
            else:
                record.last_seen_revision = max(record.last_seen_revision, int(last_seen_revision))
            return record

    def unregister(self, *, session_id: str, profile: str, runtime_id: str, transport: Any) -> bool:
        key = (str(profile), str(session_id), str(runtime_id))
        with self._lock:
            peers = self._records.get(key)
            if not peers:
                return False
            removed = peers.pop(id(transport), None) is not None
            if not peers:
                self._records.pop(key, None)
            return removed

    def unregister_transport(self, transport: Any) -> int:
        transport_key = id(transport)
        removed = 0
        with self._lock:
            for key, peers in list(self._records.items()):
                if peers.pop(transport_key, None) is not None:
                    removed += 1
                if not peers:
                    self._records.pop(key, None)
        return removed

    def observers_for(self, *, session_id: str, profile: str, runtime_id: str) -> list[ObserverRecord]:
        with self._lock:
            return list(self._records.get((str(profile), str(session_id), str(runtime_id)), {}).values())

    def fanout(
        self,
        *,
        session_id: str,
        profile: str,
        runtime_id: str,
        frame: dict,
        primary_transport: Any = None,
    ) -> int:
        delivered = 0
        stale: list[Any] = []
        for record in self.observers_for(session_id=session_id, profile=profile, runtime_id=runtime_id):
            if record.transport is primary_transport:
                continue
            try:
                if record.transport.write(frame):
                    delivered += 1
                else:
                    stale.append(record.transport)
            except Exception:
                stale.append(record.transport)
        for transport in stale:
            self.unregister(session_id=session_id, profile=profile, runtime_id=runtime_id, transport=transport)
        return delivered

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


observer_registry = ObserverRegistry()
