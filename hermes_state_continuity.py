"""SessionDB-owned context-rebase lineage and local publication.

This mixin adds only the local durable boundary for replacing a model working
context.  It does not authorize provider/tool calls, settle external effects,
or claim that cross-owner activation is atomic with SQLite publication.

A committed rebase child starts in ``committed_pending_activation``.  The
runtime may mark it ``ready`` only after the existing goal/task, custody,
context-engine/provider and other required owners have been reconciled.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
import time
from typing import Any, Dict, List, Optional

from hermes_state_common import _sql_session_last_active


_CONTEXT_REBASE_SCHEMA = "SessionDBContextRebaseV1"
_CONTEXT_REBASE_KEY_PREFIX = "context-rebase:"
_CONTEXT_REBASE_END_REASON = "context_rebase"
_ALLOWED_STATES = {"committed_pending_activation", "ready", "reconciliation_required", "cancelled"}
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")


class ContextContinuationError(RuntimeError):
    """Typed continuity refusal with a stable payload-free code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _identity(value: str, code: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        raise ContextContinuationError(code)
    return value


def _digest(value: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ContextContinuationError("INVALID_CONTINUATION_DIGEST")
    return value


def _nonnegative_int(value: int, code: str) -> int:
    if type(value) is not int or value < 0 or value > 2**63 - 1:
        raise ContextContinuationError(code)
    return value


def _strict_json(raw: str) -> dict:
    if type(raw) is not str or len(raw) > 1_000_000:
        raise ContextContinuationError("CONTEXT_REBASE_RECORD_INVALID")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContextContinuationError("CONTEXT_REBASE_RECORD_INVALID")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ContextContinuationError("CONTEXT_REBASE_RECORD_INVALID") from exc
    if type(value) is not dict:
        raise ContextContinuationError("CONTEXT_REBASE_RECORD_INVALID")
    return value


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ContextContinuationError("CONTEXT_REBASE_RECORD_INVALID") from exc


@dataclass(frozen=True)
class ContextRebaseTransition:
    schema: str
    transition_id: str
    parent_session_id: str
    child_session_id: str
    context_epoch: int
    continuation_digest: str
    control_revision: int
    input_watermark: int
    state: str
    created_at: float
    ready_at: Optional[float]

    def __post_init__(self) -> None:
        if self.schema != _CONTEXT_REBASE_SCHEMA:
            raise ContextContinuationError("CONTEXT_REBASE_SCHEMA")
        _identity(self.transition_id, "INVALID_TRANSITION_ID")
        _identity(self.parent_session_id, "INVALID_PARENT_SESSION")
        _identity(self.child_session_id, "INVALID_CHILD_SESSION")
        _nonnegative_int(self.context_epoch, "INVALID_CONTEXT_EPOCH")
        _digest(self.continuation_digest)
        _nonnegative_int(self.control_revision, "INVALID_CONTROL_REVISION")
        _nonnegative_int(self.input_watermark, "INVALID_INPUT_WATERMARK")
        if self.state not in _ALLOWED_STATES:
            raise ContextContinuationError("INVALID_CONTEXT_REBASE_STATE")
        if type(self.created_at) not in (int, float) or self.created_at <= 0:
            raise ContextContinuationError("CONTEXT_REBASE_RECORD_INVALID")
        if self.ready_at is not None and (type(self.ready_at) not in (int, float) or self.ready_at <= 0):
            raise ContextContinuationError("CONTEXT_REBASE_RECORD_INVALID")
        if self.state == "ready" and self.ready_at is None:
            raise ContextContinuationError("CONTEXT_REBASE_RECORD_INVALID")
        if self.state != "ready" and self.ready_at is not None:
            raise ContextContinuationError("CONTEXT_REBASE_RECORD_INVALID")

    @classmethod
    def from_raw(cls, raw: str) -> "ContextRebaseTransition":
        value = _strict_json(raw)
        if set(value) != {"schema", "transition_id", "parent_session_id", "child_session_id",
                          "context_epoch", "continuation_digest", "control_revision",
                          "input_watermark", "state", "created_at", "ready_at"}:
            raise ContextContinuationError("CONTEXT_REBASE_RECORD_INVALID")
        try:
            return cls(**value)
        except TypeError as exc:
            raise ContextContinuationError("CONTEXT_REBASE_RECORD_INVALID") from exc

    def raw(self) -> str:
        return _canonical(asdict(self))




@dataclass(frozen=True)
class ContextRebaseSnapshot:
    """One bounded SessionDB read for successor compilation, never authority."""

    session_id: str
    conversation_root: str
    profile_name: Optional[str]
    cwd: Optional[str]
    git_branch: Optional[str]
    git_repo_root: Optional[str]
    input_watermark: int
    first_user: Optional[Dict[str, Any]]
    current_users: tuple[Dict[str, Any], ...]
    latest_summary: Optional[Dict[str, Any]]
    recent_events: tuple[Dict[str, Any], ...]
    goal_raw: Optional[str]
    todo_json: Optional[str]

class SessionContextContinuityMixin:
    """Local SessionDB continuity owner; external activation remains separate."""

    def _context_rebase_key(self, transition_id: str) -> str:
        return _CONTEXT_REBASE_KEY_PREFIX + _identity(transition_id, "INVALID_TRANSITION_ID")

    @staticmethod
    def _context_epoch_from_model_config(raw: Any) -> int:
        if raw in (None, ""):
            return 0
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            raise ContextContinuationError("CONTEXT_EPOCH_METADATA_INVALID") from None
        if type(value) is not dict:
            raise ContextContinuationError("CONTEXT_EPOCH_METADATA_INVALID")
        epoch = value.get("_context_epoch", 0)
        return _nonnegative_int(epoch, "CONTEXT_EPOCH_METADATA_INVALID")

    @staticmethod
    def _context_rebase_child_matches(row: Any, parent_session_id: str) -> bool:
        if row is None:
            return False
        try:
            raw = row["model_config"] if hasattr(row, "keys") else row.get("model_config")
            value = json.loads(raw or "{}") if isinstance(raw, str) else (raw or {})
        except (TypeError, ValueError, AttributeError):
            return False
        return (
            type(value) is dict
            and value.get("_context_rebase_from") == parent_session_id
            and isinstance(value.get("_context_rebase_transition"), str)
            and bool(value.get("_context_rebase_transition"))
            and type(value.get("_context_epoch")) is int
            and value.get("_context_epoch") >= 1
        )

    def read_context_rebase_snapshot(
        self, session_id: str, *, recent_limit: int = 12, user_limit: int = 32
    ) -> ContextRebaseSnapshot:
        """Read one bounded compilation snapshot under a single SQLite read context."""
        _identity(session_id, "INVALID_PARENT_SESSION")
        if type(recent_limit) is not int or not 1 <= recent_limit <= 64:
            raise ContextContinuationError("INVALID_RECENT_LIMIT")
        if type(user_limit) is not int or not 1 <= user_limit <= 128:
            raise ContextContinuationError("INVALID_USER_LIMIT")

        with self._read_ctx() as conn:
            session = conn.execute(
                "SELECT id,profile_name,cwd,git_branch,git_repo_root,ended_at FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if session is None or session["ended_at"] is not None:
                raise ContextContinuationError("CONTEXT_REBASE_PARENT_NOT_LIVE")
            root = self._session_turn_lease_key_on_conn(conn, session_id)
            watermark_row = conn.execute(
                "SELECT COALESCE(MAX(id),0) AS watermark FROM messages WHERE session_id=? AND active=1",
                (session_id,),
            ).fetchone()
            watermark = int(watermark_row["watermark"] if watermark_row else 0)
            if watermark <= 0:
                raise ContextContinuationError("CONTEXT_REBASE_PARENT_EMPTY")

            # Only traverse canonical continuation parents. Explicit branches
            # have their own copied transcript and therefore root at themselves.
            lineage = [session_id]
            current = session_id
            seen = {current}
            for _ in range(1000):
                row = conn.execute(
                    "SELECT parent_session_id,model_config FROM sessions WHERE id=?",
                    (current,),
                ).fetchone()
                if row is None or not row["parent_session_id"]:
                    break
                parent_id = row["parent_session_id"]
                if parent_id in seen:
                    raise ContextContinuationError("CONTINUATION_CYCLE")
                parent = conn.execute(
                    "SELECT end_reason FROM sessions WHERE id=?", (parent_id,),
                ).fetchone()
                if parent is None:
                    break
                reason = parent["end_reason"]
                if reason == "compression":
                    # Match the same explicit fork boundary used by the turn lease.
                    config = json.loads(row["model_config"] or "{}")
                    if (type(config) is not dict
                            or config.get("_branched_from") == parent_id
                            or config.get("_delegate_from") == parent_id):
                        break
                elif reason == _CONTEXT_REBASE_END_REASON:
                    if not self._context_rebase_child_matches(row, parent_id):
                        break
                else:
                    break
                lineage.append(parent_id)
                seen.add(parent_id)
                current = parent_id
            else:
                raise ContextContinuationError("CONTINUATION_DEPTH_LIMIT")

            first_user = None
            for sid in reversed(lineage):
                row = conn.execute(
                    "SELECT id,content,timestamp FROM messages "
                    "WHERE session_id=? AND active=1 AND role='user' ORDER BY id ASC LIMIT 1",
                    (sid,),
                ).fetchone()
                if row is not None:
                    first_user = {
                        "row_id": int(row["id"]),
                        "content": self._decode_content(row["content"]),
                        "timestamp": row["timestamp"],
                    }
                    break

            summary = conn.execute(
                "SELECT id,content,timestamp FROM messages "
                "WHERE session_id=? AND active=1 AND _compressed_summary=1 "
                "ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            summary_id = int(summary["id"]) if summary is not None else 0
            latest_summary = None if summary is None else {
                "row_id": summary_id,
                "content": self._decode_content(summary["content"]),
                "timestamp": summary["timestamp"],
            }

            user_rows = conn.execute(
                "SELECT id,content,timestamp FROM messages "
                "WHERE session_id=? AND active=1 AND role='user' AND id>? "
                "ORDER BY id ASC LIMIT ?",
                (session_id, summary_id, user_limit + 1),
            ).fetchall()
            if len(user_rows) > user_limit:
                raise ContextContinuationError("TOO_MANY_UNSUMMARIZED_USER_CHANGES")
            current_users = tuple({
                "row_id": int(row["id"]),
                "content": self._decode_content(row["content"]),
                "timestamp": row["timestamp"],
            } for row in user_rows)
            if not current_users:
                raise ContextContinuationError("CONTEXT_REBASE_USER_ANCHOR_MISSING")

            event_rows = conn.execute(
                "SELECT id,role,content,tool_name,tool_call_id,finish_reason,timestamp,_compressed_summary "
                "FROM messages WHERE session_id=? AND active=1 AND id>? "
                "ORDER BY id DESC LIMIT ?",
                (session_id, summary_id, recent_limit),
            ).fetchall()
            recent_events = tuple({
                "row_id": int(row["id"]),
                "role": row["role"],
                "content": self._decode_content(row["content"]),
                "tool_name": row["tool_name"],
                "tool_call_id": row["tool_call_id"],
                "finish_reason": row["finish_reason"],
                "timestamp": row["timestamp"],
                "compressed_summary": bool(row["_compressed_summary"]),
            } for row in reversed(event_rows))

            goal_row = conn.execute(
                "SELECT value FROM state_meta WHERE key=?", (f"goal:{session_id}",),
            ).fetchone()
            goal_raw = None if goal_row is None else goal_row[0]
            todo = self._current_todo_snapshot_on_conn(conn, session_id)
            todo_json = None if todo is None else todo["todos_json"]

            # Recheck the local read coordinates inside this same read transaction.
            final = conn.execute(
                "SELECT COALESCE(MAX(id),0) AS watermark FROM messages WHERE session_id=? AND active=1",
                (session_id,),
            ).fetchone()
            if int(final["watermark"] if final else 0) != watermark:
                raise ContextContinuationError("CONTEXT_REBASE_SNAPSHOT_CHANGED")
            return ContextRebaseSnapshot(
                session_id, str(root), session["profile_name"], session["cwd"],
                session["git_branch"], session["git_repo_root"], watermark,
                first_user, current_users, latest_summary, recent_events, goal_raw, todo_json,
            )

    def read_context_rebase_transition(self, transition_id: str) -> Optional[ContextRebaseTransition]:
        raw = self.get_meta(self._context_rebase_key(transition_id))
        return None if raw is None else ContextRebaseTransition.from_raw(raw)

    def publish_context_rebase_child(
        self,
        *,
        transition_id: str,
        parent_session_id: str,
        child_session_id: str,
        continuation_digest: str,
        control_revision: int,
        input_watermark: int,
        turn_lease_holder: str,
        source: str,
        messages: List[Dict[str, Any]],
        model: str = None,
        model_config: Dict[str, Any] = None,
        system_prompt: str = None,
        cwd: str = None,
        profile_name: str = None,
    ) -> ContextRebaseTransition:
        """Atomically publish one complete local successor and close its parent.

        The expected input watermark prevents a compiled candidate from winning
        after another durable message arrived.  The current conversation turn
        lease is required so a sibling process cannot concurrently publish a
        different successor.  The transition is deliberately *not* READY yet.
        """
        transition_id = _identity(transition_id, "INVALID_TRANSITION_ID")
        parent_session_id = _identity(parent_session_id, "INVALID_PARENT_SESSION")
        child_session_id = _identity(child_session_id, "INVALID_CHILD_SESSION")
        _digest(continuation_digest)
        _nonnegative_int(control_revision, "INVALID_CONTROL_REVISION")
        _nonnegative_int(input_watermark, "INVALID_INPUT_WATERMARK")
        if type(turn_lease_holder) is not str or not turn_lease_holder:
            raise ContextContinuationError("TURN_LEASE_REQUIRED")
        if type(messages) is not list or not messages:
            raise ContextContinuationError("CONTEXT_REBASE_EMPTY_CHILD")
        if type(model_config) not in (dict, type(None)):
            raise ContextContinuationError("CONTEXT_REBASE_MODEL_CONFIG")

        key = self._context_rebase_key(transition_id)
        requested_identity = {
            "transition_id": transition_id,
            "parent_session_id": parent_session_id,
            "child_session_id": child_session_id,
            "continuation_digest": continuation_digest,
            "control_revision": control_revision,
            "input_watermark": input_watermark,
        }

        def _do(conn):
            prior_row = conn.execute("SELECT value FROM state_meta WHERE key=?", (key,)).fetchone()
            if prior_row is not None:
                prior = ContextRebaseTransition.from_raw(prior_row[0])
                for field, expected in requested_identity.items():
                    if getattr(prior, field) != expected:
                        raise ContextContinuationError("CONTEXT_REBASE_IDEMPOTENCY_MISMATCH")
                child = conn.execute("SELECT parent_session_id, model_config FROM sessions WHERE id=?",
                                     (prior.child_session_id,)).fetchone()
                if child is None or child["parent_session_id"] != prior.parent_session_id or not self._context_rebase_child_matches(child, prior.parent_session_id):
                    raise ContextContinuationError("CONTEXT_REBASE_COMMIT_CORRUPT")
                return prior, None

            parent = conn.execute(
                """SELECT ended_at, end_reason, cwd, git_branch, git_repo_root,
                          user_id, session_key, chat_id, chat_type, thread_id,
                          display_name, origin_json, profile_name, model_config
                   FROM sessions WHERE id=?""",
                (parent_session_id,),
            ).fetchone()
            if parent is None:
                raise ContextContinuationError("CONTEXT_REBASE_PARENT_MISSING")
            if parent["ended_at"] is not None or parent["end_reason"] is not None:
                raise ContextContinuationError("CONTEXT_REBASE_PARENT_CLOSED")
            if conn.execute("SELECT 1 FROM sessions WHERE id=?", (child_session_id,)).fetchone() is not None:
                raise ContextContinuationError("CONTEXT_REBASE_CHILD_EXISTS")

            lease_key = self._session_turn_lease_key_on_conn(conn, parent_session_id)
            lease = conn.execute(
                "SELECT holder,expires_at FROM session_turn_leases WHERE conversation_id=?",
                (lease_key,),
            ).fetchone()
            if lease is None or lease["holder"] != turn_lease_holder:
                raise ContextContinuationError("TURN_LEASE_MISMATCH")
            try:
                lease_expires = float(lease["expires_at"])
            except (TypeError, ValueError, OverflowError):
                raise ContextContinuationError("TURN_LEASE_MISMATCH") from None
            if lease_expires <= time.time():
                raise ContextContinuationError("TURN_LEASE_MISMATCH")

            row = conn.execute(
                "SELECT COALESCE(MAX(id),0) AS watermark FROM messages WHERE session_id=? AND active=1",
                (parent_session_id,),
            ).fetchone()
            if int(row["watermark"] if row else 0) != input_watermark:
                raise ContextContinuationError("CONTEXT_REBASE_STALE_INPUT")

            parent_epoch = self._context_epoch_from_model_config(parent["model_config"])
            child_epoch = parent_epoch + 1
            config = dict(model_config or {})
            reserved = {
                "_context_rebase_from": parent_session_id,
                "_context_rebase_transition": transition_id,
                "_context_epoch": child_epoch,
            }
            for field, expected in reserved.items():
                if field in config and config[field] != expected:
                    raise ContextContinuationError("CONTEXT_REBASE_MODEL_CONFIG")
                config[field] = expected

            todo_baseline = self._todo_compaction_baseline_on_conn(
                conn, parent_session_id, messages, input_watermark, input_watermark,
            )
            system_prompt_hash = self._store_system_prompt(conn, system_prompt)
            conn.execute(
                """INSERT INTO sessions (
                   id, source, model, model_config, system_prompt, system_prompt_hash,
                   parent_session_id, cwd, git_branch, git_repo_root, profile_name,
                   user_id, session_key, chat_id, chat_type, thread_id,
                   display_name, origin_json, started_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    child_session_id, source, model, _canonical(config), system_prompt_hash,
                    parent_session_id, cwd or parent["cwd"], parent["git_branch"],
                    parent["git_repo_root"], profile_name or parent["profile_name"],
                    parent["user_id"], parent["session_key"], parent["chat_id"],
                    parent["chat_type"], parent["thread_id"], parent["display_name"],
                    parent["origin_json"], time.time(),
                ),
            )
            total_messages, total_tool_calls, row_ids = self._insert_message_rows(
                conn, child_session_id, messages
            )
            self._project_todo_baseline_on_conn(
                conn, parent_session_id, child_session_id, todo_baseline, row_ids,
                "context_rebase_child", input_watermark, input_watermark,
            )
            conn.execute(
                "UPDATE sessions SET message_count=?,tool_call_count=? WHERE id=?",
                (total_messages, total_tool_calls, child_session_id),
            )
            now = time.time()
            updated = conn.execute(
                "UPDATE sessions SET ended_at=?,end_reason=? WHERE id=? AND ended_at IS NULL AND end_reason IS NULL",
                (now, _CONTEXT_REBASE_END_REASON, parent_session_id),
            )
            if updated.rowcount != 1:
                raise ContextContinuationError("CONTEXT_REBASE_PARENT_CHANGED")
            transition = ContextRebaseTransition(
                _CONTEXT_REBASE_SCHEMA, transition_id, parent_session_id, child_session_id,
                child_epoch, continuation_digest, control_revision, input_watermark,
                "committed_pending_activation", now, None,
            )
            conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)", (key, transition.raw()))
            return transition, row_ids

        transition, row_ids = self._execute_write(_do)
        if row_ids is not None:
            self._publish_message_row_ids(messages, row_ids)
        return transition

    def mark_context_rebase_ready(
        self,
        transition_id: str,
        *,
        expected_continuation_digest: str,
        expected_child_session_id: str,
    ) -> ContextRebaseTransition:
        """Mark the local transition ready after external owner reconciliation."""
        key = self._context_rebase_key(transition_id)
        _digest(expected_continuation_digest)
        _identity(expected_child_session_id, "INVALID_CHILD_SESSION")

        def _do(conn):
            row = conn.execute("SELECT value FROM state_meta WHERE key=?", (key,)).fetchone()
            if row is None:
                raise ContextContinuationError("CONTEXT_REBASE_NOT_FOUND")
            old = ContextRebaseTransition.from_raw(row[0])
            if (old.continuation_digest != expected_continuation_digest
                    or old.child_session_id != expected_child_session_id):
                raise ContextContinuationError("CONTEXT_REBASE_READY_BINDING_MISMATCH")
            if old.state == "ready":
                return old
            if old.state != "committed_pending_activation":
                raise ContextContinuationError("CONTEXT_REBASE_NOT_ACTIVATABLE")
            child = conn.execute("SELECT ended_at,model_config FROM sessions WHERE id=?", (old.child_session_id,)).fetchone()
            if child is None or child["ended_at"] is not None or not self._context_rebase_child_matches(child, old.parent_session_id):
                raise ContextContinuationError("CONTEXT_REBASE_CHILD_NOT_LIVE")
            ready = ContextRebaseTransition(
                old.schema, old.transition_id, old.parent_session_id, old.child_session_id,
                old.context_epoch, old.continuation_digest, old.control_revision,
                old.input_watermark, "ready", old.created_at, time.time(),
            )
            cursor = conn.execute("UPDATE state_meta SET value=? WHERE key=? AND value=?",
                                  (ready.raw(), key, row[0]))
            if cursor.rowcount != 1:
                raise ContextContinuationError("CONTEXT_REBASE_STATE_CHANGED")
            return ready

        return self._execute_write(_do)

    def mark_context_rebase_reconciliation_required(
        self,
        transition_id: str,
    ) -> ContextRebaseTransition:
        """Persist post-publication owner ambiguity without reopening the parent.

        This state is intentionally terminal for ordinary turn admission until
        the existing owners reconcile the committed successor.  Repeated calls
        are idempotent; READY/CANCELLED transitions are never silently demoted.
        """
        key = self._context_rebase_key(transition_id)

        def _do(conn):
            row = conn.execute("SELECT value FROM state_meta WHERE key=?", (key,)).fetchone()
            if row is None:
                raise ContextContinuationError("CONTEXT_REBASE_NOT_FOUND")
            old = ContextRebaseTransition.from_raw(row[0])
            if old.state == "reconciliation_required":
                return old
            if old.state != "committed_pending_activation":
                raise ContextContinuationError("CONTEXT_REBASE_NOT_RECONCILABLE")
            updated = ContextRebaseTransition(
                old.schema, old.transition_id, old.parent_session_id,
                old.child_session_id, old.context_epoch,
                old.continuation_digest, old.control_revision,
                old.input_watermark, "reconciliation_required",
                old.created_at, None,
            )
            cursor = conn.execute(
                "UPDATE state_meta SET value=? WHERE key=? AND value=?",
                (updated.raw(), key, row[0]),
            )
            if cursor.rowcount != 1:
                raise ContextContinuationError("CONTEXT_REBASE_STATE_CHANGED")
            return updated

        return self._execute_write(_do)

    def get_context_continuation_tip(self, session_id: str, *, max_depth: int = 1000) -> Optional[str]:
        """Follow compression + authenticated context-rebase edges to one tip.

        Compression keeps its historical ranked-child behavior.  Context-rebase
        publication is stricter: more than one marker-bound child is corrupt or
        ambiguous, and hitting the traversal limit is a typed refusal rather
        than a plausible stale ancestor.
        """
        if not session_id:
            return session_id
        if type(max_depth) is not int or max_depth < 1 or max_depth > 10000:
            raise ContextContinuationError("INVALID_CONTINUATION_DEPTH")
        current = session_id
        seen = {current}
        for _ in range(max_depth):
            with self._lock:
                parent = self._conn.execute(
                    "SELECT id,end_reason FROM sessions WHERE id=?", (current,),
                ).fetchone()
                if parent is None:
                    return current
                reason = parent["end_reason"]
                if reason == "compression":
                    row = self._conn.execute(
                        f"""SELECT child.id
                            FROM sessions child
                            WHERE child.parent_session_id=?
                              AND json_extract(COALESCE(child.model_config, '{{}}'), '$._branched_from') IS NULL
                              AND json_extract(COALESCE(child.model_config, '{{}}'), '$._delegate_from') IS NULL
                              AND COALESCE(child.source, '') != 'tool'
                            ORDER BY CASE WHEN child.end_reason='compression' THEN 0 WHEN child.ended_at IS NULL THEN 1 ELSE 2 END,
                                     {_sql_session_last_active('child')} DESC,
                                     child.started_at DESC, child.id DESC
                            LIMIT 1""",
                        (current,),
                    ).fetchone()
                    child_id = None if row is None else row["id"]
                elif reason == _CONTEXT_REBASE_END_REASON:
                    rows = self._conn.execute(
                        """SELECT id,model_config FROM sessions
                           WHERE parent_session_id=? AND COALESCE(source,'')!='tool'
                           ORDER BY started_at ASC,id ASC""",
                        (current,),
                    ).fetchall()
                    matches = [row for row in rows if self._context_rebase_child_matches(row, current)]
                    if len(matches) > 1:
                        raise ContextContinuationError("AMBIGUOUS_CONTEXT_REBASE")
                    child_id = matches[0]["id"] if matches else None
                else:
                    return current
            if not child_id:
                return current
            if child_id in seen:
                raise ContextContinuationError("CONTINUATION_CYCLE")
            seen.add(child_id)
            current = child_id
        with self._lock:
            row = self._conn.execute("SELECT end_reason FROM sessions WHERE id=?", (current,)).fetchone()
        if row is not None and row["end_reason"] in {"compression", _CONTEXT_REBASE_END_REASON}:
            raise ContextContinuationError("CONTINUATION_DEPTH_LIMIT")
        return current
