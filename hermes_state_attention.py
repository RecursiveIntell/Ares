"""Durable WorkItem/Watch store owned by SessionDB.

Records live in the existing ``state_meta`` key/value table under the
``attention:`` namespace; there is no second database and no table migration.
Each record is strict, versioned, canonical JSON, and every mutation is a
byte-exact compare-and-set inside one ``BEGIN IMMEDIATE`` transaction.

Keys::

    attention:work_item:<work_item_id>
    attention:watch:<work_item_id>:<watch_id>

The parent is part of a Watch key so that parent-scoped operations (cancel
cascade, per-parent cap) are key-range scans.  A corrupt row therefore blocks
only operations that must *read or transition that row* (its own parent's
cancellation, a direct read of it, or the matcher feed); it cannot block
unrelated WorkItems or the creation of unrelated Watches.  ``watch_id`` is
unique across parents.

Boundaries (see P02 decisions): a WorkItem is provider-neutral, only the
current record plus a monotonic revision is kept, expiry is derived at read
time, and nothing here prunes, expires, or deletes a record.  ``GoalManager``
(``goal:<session_id>``) and the context-input inbox are untouched.

Clock: ``now_ms`` is a trusted caller input.  This module neither owns nor
authenticates a clock; ``TIME_REGRESSION`` only keeps a record's own timeline
monotonic.  A caller that supplies a stale time can make an elapsed Watch look
active, so expiry correctness depends on the caller (or a later phase's
authoritative clock owner), not on this store.

Not established here: backup/restore of this namespace beyond the cases in
``tests/hermes_state/test_attention_store.py``, scan cost at scale, and any
operator repair path for a corrupt record (it is refused, never skipped).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Any

from ares_runtime.attention.contracts import (
    AttentionContractError,
    WatchSpecV1,
    WatchState,
)
from ares_runtime.attention.records import (
    EffectiveWatchState,
    WatchRecordV1,
    WorkItemRecordV1,
    WorkItemState,
    canonical_json,
    identifier,
)

WORK_ITEM_PREFIX = "attention:work_item:"
WATCH_PREFIX = "attention:watch:"

MAX_WORK_ITEMS = 1024
MAX_WATCHES = 2048
MAX_WATCHES_PER_WORK_ITEM = 64


class AttentionStoreError(RuntimeError):
    """Typed refusal from the durable attention-record owner."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(code + (": " + detail if detail else ""))


def _ident(value: object) -> str:
    try:
        return identifier(value, "id")
    except AttentionContractError as exc:
        raise AttentionStoreError("INVALID_IDENTIFIER", exc.code) from exc


def attention_work_item_key(work_item_id: str) -> str:
    return WORK_ITEM_PREFIX + _ident(work_item_id)


def attention_watch_parent_prefix(work_item_id: str) -> str:
    """Key prefix shared by every Watch of one WorkItem (ends in ``:``)."""
    return WATCH_PREFIX + _ident(work_item_id) + ":"


def attention_watch_key(work_item_id: str, watch_id: str) -> str:
    return attention_watch_parent_prefix(work_item_id) + _ident(watch_id)


def _split_watch_key(key: str) -> tuple[str, str] | None:
    """Return ``(work_item_id, watch_id)`` or None for a malformed key."""
    parts = key[len(WATCH_PREFIX) :].split(":")
    if len(parts) != 2:
        return None
    try:
        return identifier(parts[0], "id"), identifier(parts[1], "id")
    except AttentionContractError:
        return None


def _upper_bound(prefix: str) -> str:
    # Prefixes end in ':'; the next code point bounds an exact range scan with
    # no LIKE wildcards to escape.
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _cell(row: Any, index: int) -> str:
    return row[index]


class SessionAttentionMixin:
    """SessionDB mixin for WorkItem and Watch records."""

    _execute_write: Callable[..., Any]
    _read_ctx: Callable[..., Any]

    # -- strict decode/encode ------------------------------------------------

    @staticmethod
    def _attention_decode(text: str, parser: Any, expect_id: str, id_field: str) -> Any:
        # Strictness comes from the byte-exact canonical comparison below: any
        # duplicate key, NaN/Infinity constant, or non-canonical spelling can
        # never re-serialize to the stored text, so it is refused.
        try:
            raw = json.loads(text)
            record = parser.parse(raw)
            if getattr(record, id_field) != expect_id:
                raise AttentionContractError("KEY_RECORD_MISMATCH")
            if canonical_json(record.to_dict()) != text:
                raise AttentionContractError("NOT_CANONICAL")
        except (TypeError, ValueError, AttentionContractError) as exc:
            detail = getattr(exc, "code", type(exc).__name__)
            raise AttentionStoreError(
                "RECORD_INVALID", f"{expect_id}: {detail}"
            ) from exc
        return record

    def _attention_decode_watch(self, key: str, text: str) -> WatchRecordV1:
        ids = _split_watch_key(key)
        if ids is None:
            raise AttentionStoreError("RECORD_INVALID", f"{key}: BAD_KEY")
        parent_id, watch_id = ids
        record = self._attention_decode(text, WatchRecordV1, watch_id, "watch_id")
        if record.expectation_id != parent_id:
            raise AttentionStoreError(
                "RECORD_INVALID", f"{watch_id}: KEY_RECORD_MISMATCH"
            )
        return record

    def _attention_rows_on_conn(
        self, conn: sqlite3.Connection, prefix: str
    ) -> list[tuple[str, str]]:
        rows = conn.execute(
            "SELECT key, value FROM state_meta WHERE key >= ? AND key < ? ORDER BY key",
            (prefix, _upper_bound(prefix)),
        ).fetchall()
        return [(_cell(row, 0), _cell(row, 1)) for row in rows]

    def _attention_keys_on_conn(
        self, conn: sqlite3.Connection, prefix: str
    ) -> list[str]:
        """Keys only: counting and lookup never decode (or trip on) a value."""
        rows = conn.execute(
            "SELECT key FROM state_meta WHERE key >= ? AND key < ? ORDER BY key",
            (prefix, _upper_bound(prefix)),
        ).fetchall()
        return [_cell(row, 0) for row in rows]

    def _attention_work_items_on_conn(
        self, conn: sqlite3.Connection
    ) -> list[WorkItemRecordV1]:
        return [
            self._attention_decode(
                value, WorkItemRecordV1, key[len(WORK_ITEM_PREFIX) :], "work_item_id"
            )
            for key, value in self._attention_rows_on_conn(conn, WORK_ITEM_PREFIX)
        ]

    def _attention_watches_on_conn(
        self, conn: sqlite3.Connection, work_item_id: str | None = None
    ) -> list[WatchRecordV1]:
        prefix = (
            WATCH_PREFIX
            if work_item_id is None
            else attention_watch_parent_prefix(work_item_id)
        )
        return [
            self._attention_decode_watch(key, value)
            for key, value in self._attention_rows_on_conn(conn, prefix)
        ]

    def _attention_read_text_on_conn(
        self, conn: sqlite3.Connection, key: str, missing_code: str, detail: str
    ) -> str:
        row = conn.execute(
            "SELECT value FROM state_meta WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            raise AttentionStoreError(missing_code, detail)
        return _cell(row, 0)

    def _read_work_item_on_conn(
        self, conn: sqlite3.Connection, work_item_id: str
    ) -> tuple[WorkItemRecordV1, str]:
        text = self._attention_read_text_on_conn(
            conn,
            attention_work_item_key(work_item_id),
            "WORK_ITEM_NOT_FOUND",
            work_item_id,
        )
        return (
            self._attention_decode(
                text, WorkItemRecordV1, work_item_id, "work_item_id"
            ),
            text,
        )

    def _attention_watch_keys_for_on_conn(
        self, conn: sqlite3.Connection, watch_id: str
    ) -> list[str]:
        """Keys whose watch_id matches; malformed unrelated keys are ignored here."""
        _ident(watch_id)
        matches = []
        for key in self._attention_keys_on_conn(conn, WATCH_PREFIX):
            ids = _split_watch_key(key)
            if ids is not None and ids[1] == watch_id:
                matches.append(key)
        return matches

    def _read_watch_on_conn(
        self, conn: sqlite3.Connection, watch_id: str
    ) -> tuple[WatchRecordV1, str, str]:
        keys = self._attention_watch_keys_for_on_conn(conn, watch_id)
        if not keys:
            raise AttentionStoreError("WATCH_NOT_FOUND", watch_id)
        if len(keys) > 1:
            raise AttentionStoreError("RECORD_INVALID", f"{watch_id}: DUPLICATE_ID")
        text = self._attention_read_text_on_conn(
            conn, keys[0], "WATCH_NOT_FOUND", watch_id
        )
        return self._attention_decode_watch(keys[0], text), text, keys[0]

    # -- write primitives ----------------------------------------------------

    def _attention_insert_on_conn(
        self, conn: sqlite3.Connection, key: str, text: str, ident: str
    ) -> None:
        try:
            conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)", (key, text))
        except sqlite3.IntegrityError as exc:
            raise AttentionStoreError("ALREADY_EXISTS", ident) from exc

    def _attention_cas_on_conn(
        self, conn: sqlite3.Connection, key: str, previous_text: str, new_text: str
    ) -> None:
        cursor = conn.execute(
            "UPDATE state_meta SET value=? WHERE key=? AND value=?",
            (new_text, key, previous_text),
        )
        if cursor.rowcount != 1:
            raise AttentionStoreError("STALE_REVISION", key)

    @staticmethod
    def _attention_contract(call: Callable[[], Any]) -> Any:
        try:
            return call()
        except AttentionContractError as exc:
            raise AttentionStoreError(
                exc.code, exc.args[0] if exc.args else ""
            ) from exc

    @staticmethod
    def _attention_expected(
        current_revision: int, expected: object, ident: str
    ) -> None:
        if type(expected) is not int or expected < 1:
            raise AttentionStoreError("INVALID_REVISION", ident)
        if current_revision != expected:
            raise AttentionStoreError("STALE_REVISION", ident)

    # -- WorkItem API --------------------------------------------------------

    def create_work_item(self, record: WorkItemRecordV1) -> WorkItemRecordV1:
        if type(record) is not WorkItemRecordV1:
            raise AttentionStoreError("INVALID_RECORD")
        if record.state is not WorkItemState.ACTIVE or record.revision != 1:
            raise AttentionStoreError(
                "WORK_ITEM_MUST_START_ACTIVE", record.work_item_id
            )
        key = attention_work_item_key(record.work_item_id)

        def _do(conn: sqlite3.Connection) -> WorkItemRecordV1:
            if len(self._attention_keys_on_conn(conn, WORK_ITEM_PREFIX)) >= (
                MAX_WORK_ITEMS
            ):
                raise AttentionStoreError("LIMIT_EXCEEDED", "work_items")
            self._attention_insert_on_conn(
                conn, key, canonical_json(record.to_dict()), record.work_item_id
            )
            return record

        return self._execute_write(_do)

    def read_work_item(self, work_item_id: str) -> WorkItemRecordV1:
        with self._read_ctx() as conn:
            return self._read_work_item_on_conn(conn, work_item_id)[0]

    def list_work_items(self) -> list[WorkItemRecordV1]:
        with self._read_ctx() as conn:
            return self._attention_work_items_on_conn(conn)

    def transition_work_item(
        self,
        work_item_id: str,
        new_state: WorkItemState,
        *,
        expected_revision: int,
        now_ms: int,
    ) -> WorkItemRecordV1:
        def _do(conn: sqlite3.Connection) -> WorkItemRecordV1:
            current, current_text = self._read_work_item_on_conn(conn, work_item_id)
            self._attention_expected(current.revision, expected_revision, work_item_id)
            updated = self._attention_contract(
                lambda: current.transition(new_state, now_ms=now_ms)
            )
            self._attention_cas_on_conn(
                conn,
                attention_work_item_key(work_item_id),
                current_text,
                canonical_json(updated.to_dict()),
            )
            if updated.state is WorkItemState.CANCELLED:
                # Cancellation is the only cascading transition and shares this
                # transaction: either every child watch is cancelled or none is.
                # Only this WorkItem's key range is read, so an unrelated
                # corrupt watch cannot block it; a corrupt *child* still does.
                for watch in self._attention_watches_on_conn(conn, work_item_id):
                    if watch.state is WatchState.CANCELLED:
                        continue
                    cancelled = self._attention_contract(
                        lambda w=watch: w.transition(
                            WatchState.CANCELLED, now_ms=now_ms
                        )
                    )
                    self._attention_cas_on_conn(
                        conn,
                        attention_watch_key(work_item_id, watch.watch_id),
                        canonical_json(watch.to_dict()),
                        canonical_json(cancelled.to_dict()),
                    )
            return updated

        return self._execute_write(_do)

    # -- Watch API -----------------------------------------------------------

    def create_watch(self, record: WatchRecordV1, *, now_ms: int) -> WatchRecordV1:
        if type(record) is not WatchRecordV1:
            raise AttentionStoreError("INVALID_RECORD")
        if record.state is not WatchState.ACTIVE or record.revision != 1:
            raise AttentionStoreError("WATCH_MUST_START_ACTIVE", record.watch_id)
        stamp = self._attention_contract(lambda: record.effective_state(now_ms))
        if stamp is EffectiveWatchState.EXPIRED:
            raise AttentionStoreError("WATCH_EXPIRED", record.watch_id)
        key = attention_watch_key(record.expectation_id, record.watch_id)
        sibling_prefix = attention_watch_parent_prefix(record.expectation_id)

        def _do(conn: sqlite3.Connection) -> WatchRecordV1:
            parent, _ = self._read_work_item_on_conn(conn, record.expectation_id)
            if parent.state is not WorkItemState.ACTIVE:
                raise AttentionStoreError("WORK_ITEM_NOT_ACTIVE", record.expectation_id)
            # Keys only: counting and uniqueness never decode a stored value,
            # so an unrelated corrupt Watch cannot block creation.
            keys = self._attention_keys_on_conn(conn, WATCH_PREFIX)
            if self._attention_watch_keys_for_on_conn(conn, record.watch_id):
                raise AttentionStoreError("ALREADY_EXISTS", record.watch_id)
            if len(keys) >= MAX_WATCHES:
                raise AttentionStoreError("LIMIT_EXCEEDED", "watches")
            if sum(1 for k in keys if k.startswith(sibling_prefix)) >= (
                MAX_WATCHES_PER_WORK_ITEM
            ):
                raise AttentionStoreError("LIMIT_EXCEEDED", "watches_per_work_item")
            self._attention_insert_on_conn(
                conn, key, canonical_json(record.to_dict()), record.watch_id
            )
            return record

        return self._execute_write(_do)

    def read_watch(self, watch_id: str) -> WatchRecordV1:
        with self._read_ctx() as conn:
            return self._read_watch_on_conn(conn, watch_id)[0]

    def list_watches(self, work_item_id: str | None = None) -> list[WatchRecordV1]:
        """All Watches, or only one WorkItem's (a scoped scan that ignores others)."""
        with self._read_ctx() as conn:
            return self._attention_watches_on_conn(conn, work_item_id)

    def transition_watch(
        self,
        watch_id: str,
        new_state: WatchState,
        *,
        expected_revision: int,
        now_ms: int,
    ) -> WatchRecordV1:
        def _do(conn: sqlite3.Connection) -> WatchRecordV1:
            current, current_text, key = self._read_watch_on_conn(conn, watch_id)
            self._attention_expected(current.revision, expected_revision, watch_id)
            updated = self._attention_contract(
                lambda: current.transition(new_state, now_ms=now_ms)
            )
            if updated.state is WatchState.ACTIVE:
                parent, _ = self._read_work_item_on_conn(conn, current.expectation_id)
                if parent.state is not WorkItemState.ACTIVE:
                    raise AttentionStoreError(
                        "WORK_ITEM_NOT_ACTIVE", current.expectation_id
                    )
            self._attention_cas_on_conn(
                conn, key, current_text, canonical_json(updated.to_dict())
            )
            return updated

        return self._execute_write(_do)

    def list_active_watch_specs(self, *, now_ms: int) -> list[WatchSpecV1]:
        """Specs the P01 matcher may consume at ``now_ms``.

        Excludes paused, cancelled, and elapsed watches and any watch whose
        WorkItem is not active.  Both namespaces come from one SQL statement so
        the result is a single consistent snapshot.  This is the matcher feed:
        any corrupt row in either namespace refuses the whole listing rather
        than being skipped, because a silently shortened feed would read as
        "nothing to match".
        """
        if type(now_ms) is not int or now_ms < 0:
            raise AttentionStoreError("INVALID_TIMESTAMP", "now_ms")
        with self._read_ctx() as conn:
            rows = conn.execute(
                "SELECT key, value FROM state_meta "
                "WHERE (key >= ? AND key < ?) OR (key >= ? AND key < ?) ORDER BY key",
                (
                    WATCH_PREFIX,
                    _upper_bound(WATCH_PREFIX),
                    WORK_ITEM_PREFIX,
                    _upper_bound(WORK_ITEM_PREFIX),
                ),
            ).fetchall()
        active_items: set[str] = set()
        watches: list[WatchRecordV1] = []
        for row in rows:
            key, value = _cell(row, 0), _cell(row, 1)
            if key.startswith(WORK_ITEM_PREFIX):
                item = self._attention_decode(
                    value,
                    WorkItemRecordV1,
                    key[len(WORK_ITEM_PREFIX) :],
                    "work_item_id",
                )
                if item.state is WorkItemState.ACTIVE:
                    active_items.add(item.work_item_id)
            else:
                watches.append(self._attention_decode_watch(key, value))
        return [
            w.to_spec()
            for w in sorted(watches, key=lambda w: w.watch_id)
            if w.expectation_id in active_items
            and w.effective_state(now_ms) is EffectiveWatchState.ACTIVE
        ]


__all__ = [
    "AttentionStoreError",
    "MAX_WATCHES",
    "MAX_WATCHES_PER_WORK_ITEM",
    "MAX_WORK_ITEMS",
    "SessionAttentionMixin",
    "WATCH_PREFIX",
    "WORK_ITEM_PREFIX",
    "attention_watch_key",
    "attention_watch_parent_prefix",
    "attention_work_item_key",
]
