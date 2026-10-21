"""Durable execution-record store owned by SessionDB.

The record head is authoritative.  Event rows are a bounded, rebuildable
projection of the head history; pruning them never changes the head.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from ares_runtime.execution_record import (
    ExecutionRecordError,
    ExecutionRecordV1,
    ExecutionState,
)

HEAD_PREFIX = "execution:"
EVENT_MARKER = ":event:"
MAX_EVENTS = 32


class ExecutionRecordStoreError(RuntimeError):
    """Typed refusal from the durable execution-record owner."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(code + (": " + detail if detail else ""))


def _canonical(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ExecutionRecordStoreError("EXECUTION_RECORD_NOT_CANONICAL") from exc


def _value(row: Any) -> str:
    return row["value"] if isinstance(row, sqlite3.Row) else row[0]


class SessionExecutionRecordMixin:
    """SessionDB mixin for execution heads and their bounded event projection."""

    def _execution_head_key(self, execution_id: str) -> str:
        if not isinstance(execution_id, str) or not execution_id:
            raise ExecutionRecordStoreError("INVALID_EXECUTION_ID")
        return HEAD_PREFIX + execution_id

    def _execution_event_key(self, execution_id: str, sequence: int) -> str:
        if type(sequence) is not int or sequence < 1:
            raise ExecutionRecordStoreError("INVALID_EXECUTION_SEQUENCE")
        return f"{HEAD_PREFIX}{execution_id}{EVENT_MARKER}{sequence:06d}"

    def _read_execution_on_conn(
        self, conn: sqlite3.Connection, execution_id: str
    ) -> ExecutionRecordV1:
        key = self._execution_head_key(execution_id)
        row = conn.execute(
            "SELECT value FROM state_meta WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            raise ExecutionRecordStoreError("EXECUTION_NOT_FOUND", execution_id)
        try:
            raw = json.loads(_value(row))
        except (TypeError, ValueError) as exc:
            raise ExecutionRecordStoreError(
                "EXECUTION_RECORD_INVALID", execution_id
            ) from exc
        if not isinstance(raw, dict):
            raise ExecutionRecordStoreError("EXECUTION_RECORD_INVALID", execution_id)
        try:
            return ExecutionRecordV1.parse(raw)
        except ExecutionRecordError as exc:
            raise ExecutionRecordStoreError(
                "EXECUTION_RECORD_INVALID", exc.code
            ) from exc

    def _insert_head_on_conn(
        self, conn: sqlite3.Connection, record: ExecutionRecordV1
    ) -> None:
        key = self._execution_head_key(record.execution_id)
        try:
            conn.execute(
                "INSERT INTO state_meta(key,value) VALUES(?,?)",
                (key, _canonical(record.to_dict())),
            )
        except sqlite3.IntegrityError as exc:
            raise ExecutionRecordStoreError(
                "EXECUTION_ALREADY_EXISTS", record.execution_id
            ) from exc

    def _compare_and_swap_head_on_conn(
        self,
        conn: sqlite3.Connection,
        record: ExecutionRecordV1,
        previous: ExecutionRecordV1,
    ) -> None:
        key = self._execution_head_key(record.execution_id)
        cursor = conn.execute(
            "UPDATE state_meta SET value=? WHERE key=? AND value=?",
            (_canonical(record.to_dict()), key, _canonical(previous.to_dict())),
        )
        if cursor.rowcount != 1:
            raise ExecutionRecordStoreError("ALREADY_TRANSITIONED", record.execution_id)

    def _append_event_on_conn(
        self, conn: sqlite3.Connection, record: ExecutionRecordV1
    ) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO state_meta(key,value) VALUES(?,?)",
            (
                self._execution_event_key(record.execution_id, record.sequence),
                _canonical(record.to_dict()),
            ),
        )
        self._prune_execution_events_on_conn(conn, record.execution_id, keep=MAX_EVENTS)

    def _prune_execution_events_on_conn(
        self,
        conn: sqlite3.Connection,
        execution_id: str,
        *,
        keep: int = MAX_EVENTS,
    ) -> int:
        if type(keep) is not int or keep < 1:
            raise ExecutionRecordStoreError("INVALID_EVENT_LIMIT")
        prefix = f"{HEAD_PREFIX}{execution_id}{EVENT_MARKER}"
        rows = conn.execute(
            "SELECT key FROM state_meta WHERE key LIKE ? ORDER BY key",
            (prefix + "%",),
        ).fetchall()
        excess = len(rows) - keep
        if excess <= 0:
            return 0
        keys = [
            _value(row) if not isinstance(row, sqlite3.Row) else row["key"]
            for row in rows[:excess]
        ]
        conn.executemany("DELETE FROM state_meta WHERE key=?", [(key,) for key in keys])
        return len(keys)

    def create_execution_record_on_conn(
        self, conn: sqlite3.Connection, record: ExecutionRecordV1
    ) -> ExecutionRecordV1:
        if not isinstance(record, ExecutionRecordV1):
            raise ExecutionRecordStoreError("INVALID_EXECUTION_RECORD")
        if record.state != ExecutionState.ADMITTED.value:
            raise ExecutionRecordStoreError("EXECUTION_MUST_START_ADMITTED")
        self._insert_head_on_conn(conn, record)
        self._append_event_on_conn(conn, record)
        return record

    def create_execution_record(self, record: ExecutionRecordV1) -> ExecutionRecordV1:
        return self._execute_write(
            lambda conn: self.create_execution_record_on_conn(conn, record)
        )

    def transition_execution_record_on_conn(
        self,
        conn: sqlite3.Connection,
        execution_id: str,
        new_state: str,
        **updates: Any,
    ) -> ExecutionRecordV1:
        current = self._read_execution_on_conn(conn, execution_id)
        if current.state == new_state:
            raise ExecutionRecordStoreError("ALREADY_TRANSITIONED", execution_id)
        try:
            updated = current.transition(new_state, **updates)
        except ExecutionRecordError as exc:
            if exc.code in {
                "ILLEGAL_TRANSITION",
                "TERMINAL_STATE_IMMUTABLE",
            } and current.state in {
                ExecutionState.SETTLED.value,
                ExecutionState.DENIED.value,
                ExecutionState.ABANDONED.value,
                ExecutionState.INDETERMINATE.value,
            }:
                raise ExecutionRecordStoreError(
                    "ALREADY_TERMINAL", execution_id
                ) from exc
            raise ExecutionRecordStoreError(
                exc.code, exc.args[0] if exc.args else ""
            ) from exc
        self._compare_and_swap_head_on_conn(conn, updated, current)
        self._append_event_on_conn(conn, updated)
        return updated

    def transition_execution_record(
        self, execution_id: str, new_state: str, **updates: Any
    ) -> ExecutionRecordV1:
        return self._execute_write(
            lambda conn: self.transition_execution_record_on_conn(
                conn, execution_id, new_state, **updates
            )
        )

    def append_dispatch_intent_on_conn(
        self,
        conn: sqlite3.Connection,
        execution_id: str,
        dispatch_intent_ref: str,
    ) -> ExecutionRecordV1:
        if not isinstance(dispatch_intent_ref, str) or not dispatch_intent_ref:
            raise ExecutionRecordStoreError("INVALID_DISPATCH_INTENT")
        current = self._read_execution_on_conn(conn, execution_id)
        if current.is_terminal:
            return current
        refs = list(current.to_dict()["dispatch_intent_refs"])
        if dispatch_intent_ref in refs:
            return current
        refs.append(dispatch_intent_ref)
        updated = current.evolve(dispatch_intent_refs=refs)
        self._compare_and_swap_head_on_conn(conn, updated, current)
        self._append_event_on_conn(conn, updated)
        return updated

    def append_dispatch_intent(
        self, execution_id: str, dispatch_intent_ref: str
    ) -> ExecutionRecordV1:
        return self._execute_write(
            lambda conn: self.append_dispatch_intent_on_conn(
                conn, execution_id, dispatch_intent_ref
            )
        )

    def reconcile_execution_record_on_conn(
        self, conn: sqlite3.Connection, execution_id: str
    ) -> ExecutionRecordV1:
        """Mark a sent-but-unsettled record indeterminate after restart.

        A record that never reached ``send_started`` is left for the recovery
        owner; it is not promoted to a terminal state here.
        """
        current = self._read_execution_on_conn(conn, execution_id)
        if current.state != ExecutionState.SEND_STARTED.value:
            return current
        return self.transition_execution_record_on_conn(
            conn, execution_id, ExecutionState.INDETERMINATE.value
        )

    def reconcile_execution_record(self, execution_id: str) -> ExecutionRecordV1:
        return self._execute_write(
            lambda conn: self.reconcile_execution_record_on_conn(conn, execution_id)
        )

    def read_execution_record(self, execution_id: str) -> ExecutionRecordV1:
        with self._read_ctx() as conn:
            return self._read_execution_on_conn(conn, execution_id)

    def list_execution_ids(self, prefix: str = "") -> list[str]:
        if not isinstance(prefix, str):
            raise ExecutionRecordStoreError("INVALID_PREFIX")
        with self._read_ctx() as conn:
            rows = conn.execute(
                "SELECT key FROM state_meta WHERE key LIKE ? AND key NOT LIKE ? ORDER BY key",
                (HEAD_PREFIX + prefix + "%", "%" + EVENT_MARKER + "%"),
            ).fetchall()
        return [
            (_value(row) if not isinstance(row, sqlite3.Row) else row["key"])[
                len(HEAD_PREFIX) :
            ]
            for row in rows
        ]

    def prune_execution_events(
        self, execution_id: str, *, keep: int = MAX_EVENTS
    ) -> int:
        return self._execute_write(
            lambda conn: self._prune_execution_events_on_conn(
                conn, execution_id, keep=keep
            )
        )

    def read_execution_events(self, execution_id: str) -> list[dict[str, Any]]:
        prefix = f"{HEAD_PREFIX}{execution_id}{EVENT_MARKER}"
        with self._read_ctx() as conn:
            rows = conn.execute(
                "SELECT key,value FROM state_meta WHERE key LIKE ? ORDER BY key",
                (prefix + "%",),
            ).fetchall()
        result = []
        for row in rows:
            key = row["key"] if isinstance(row, sqlite3.Row) else row[0]
            value = row["value"] if isinstance(row, sqlite3.Row) else row[1]
            result.append({"key": key, "record": json.loads(value)})
        return result


__all__ = [
    "EVENT_MARKER",
    "ExecutionRecordStoreError",
    "HEAD_PREFIX",
    "MAX_EVENTS",
    "SessionExecutionRecordMixin",
]
