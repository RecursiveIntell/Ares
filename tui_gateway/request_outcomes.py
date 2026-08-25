from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any


class RequestOutcomeState(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"


class IdempotencyConflict(RuntimeError):
    pass


def mobile_request_outcome_scope_key(
    *,
    host_id: str,
    profile: str,
    session_key: str,
    principal_id: str,
) -> str:
    """Encode the server-derived durable outcome scope without delimiter ambiguity."""
    return json.dumps(
        [str(host_id), str(profile), str(session_key), str(principal_id)],
        ensure_ascii=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class RequestOutcome:
    scope: tuple[str, ...]
    method: str
    key: str
    payload_digest: str
    state: RequestOutcomeState
    result: Any = None
    error: Any = None
    created_at: float = 0.0
    updated_at: float = 0.0


class RequestOutcomeLedger:
    """Bounded process-local outcome projection used before durable admission.

    This class intentionally does not claim restart durability. The server
    capability remains disabled until the authoritative SessionDB migration
    stores these outcomes and restart/indeterminate tests pass.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[tuple[tuple[str, ...], str, str], RequestOutcome] = {}

    def reserve(
        self,
        *,
        scope: tuple[str, ...],
        method: str,
        key: str,
        payload_digest: str,
    ) -> RequestOutcome:
        if not key or not payload_digest:
            raise ValueError("idempotency key and payload digest are required")
        identity = (tuple(scope), str(method), str(key))
        now = time.time()
        with self._lock:
            current = self._records.get(identity)
            if current is not None:
                if current.payload_digest != payload_digest:
                    raise IdempotencyConflict("idempotency key payload mismatch")
                return current
            record = RequestOutcome(
                scope=identity[0],
                method=identity[1],
                key=identity[2],
                payload_digest=str(payload_digest),
                state=RequestOutcomeState.IN_PROGRESS,
                created_at=now,
                updated_at=now,
            )
            self._records[identity] = record
            return record

    def complete(self, record: RequestOutcome, *, result: Any) -> RequestOutcome:
        return self._finish(record, state=RequestOutcomeState.COMPLETED, result=result, error=None)

    def fail(self, record: RequestOutcome, *, error: Any) -> RequestOutcome:
        return self._finish(record, state=RequestOutcomeState.FAILED, result=None, error=error)

    def indeterminate(self, record: RequestOutcome, *, error: Any) -> RequestOutcome:
        return self._finish(record, state=RequestOutcomeState.INDETERMINATE, result=None, error=error)

    def _finish(self, record: RequestOutcome, *, state, result, error) -> RequestOutcome:
        identity = (record.scope, record.method, record.key)
        with self._lock:
            current = self._records.get(identity)
            if current is None or current.payload_digest != record.payload_digest:
                raise IdempotencyConflict("outcome record is no longer current")
            if current.state is not RequestOutcomeState.IN_PROGRESS:
                return current
            finished = replace(
                current,
                state=state,
                result=result,
                error=error,
                updated_at=time.time(),
            )
            self._records[identity] = finished
            return finished


class DurableRequestOutcomeLedger:
    """Typed adapter over the canonical SessionDB outcome owner."""

    def __init__(self, db) -> None:
        self._db = db

    @staticmethod
    def _scope_key(scope: tuple[str, ...]) -> str:
        return json.dumps(list(scope), separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _decode(value):
        if value is None:
            return None
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value

    def _record(self, row: dict) -> RequestOutcome:
        return RequestOutcome(
            scope=tuple(json.loads(row["scope_key"])),
            method=row["method"],
            key=row["idempotency_key"],
            payload_digest=row["payload_digest"],
            state=RequestOutcomeState(row["state"]),
            result=self._decode(row.get("result_json")),
            error=self._decode(row.get("error_json")),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def reserve(
        self,
        *,
        scope: tuple[str, ...],
        method: str,
        key: str,
        payload_digest: str,
        now: float | None = None,
    ) -> RequestOutcome:
        row = self._db.mobile_request_outcome_reserve(
            scope_key=self._scope_key(scope),
            method=method,
            idempotency_key=key,
            payload_digest=payload_digest,
            now=now,
        )
        if row.get("conflict"):
            raise IdempotencyConflict("idempotency key payload mismatch")
        return self._record(row)

    def finish(
        self,
        record: RequestOutcome,
        *,
        state: RequestOutcomeState,
        result: Any = None,
        error: Any = None,
        now: float | None = None,
    ) -> RequestOutcome:
        row = self._db.mobile_request_outcome_finish(
            scope_key=self._scope_key(record.scope),
            method=record.method,
            idempotency_key=record.key,
            payload_digest=record.payload_digest,
            state=state.value,
            result_json=json.dumps(result, sort_keys=True, separators=(",", ":")) if result is not None else None,
            error_json=json.dumps(error, sort_keys=True, separators=(",", ":")) if error is not None else None,
            now=now,
        )
        if row.get("conflict"):
            raise IdempotencyConflict("idempotency key payload mismatch")
        return self._record(row)
