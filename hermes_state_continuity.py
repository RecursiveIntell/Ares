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
import hashlib
import json
import math
import re
import time
from typing import Any, Dict, List, Optional

from agent.context_compressor import user_originated_turn_view
from hermes_state_common import _sql_session_last_active


_CONTEXT_REBASE_SCHEMA = "SessionDBContextRebaseV1"
_CONTEXT_REBASE_EPISODE_SCHEMA = "SessionDBContextRebaseEpisodeV1"
_CONTEXT_REBASE_KEY_PREFIX = "context-rebase:"
_CONTEXT_REBASE_EPISODE_KEY_PREFIX = "context-rebase-episode:"
_CONTEXT_REBASE_RECOVERY_PREFIX = "context-rebase-recovery:"
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
class ContextRebaseEpisode:
    """Conversation-level anti-thrash state; physical session IDs cannot reset it."""

    schema: str
    conversation_root: str
    attempts_without_recovery: int
    last_transition_id: Optional[str]
    last_before_tokens: Optional[int]
    last_after_tokens: Optional[int]
    updated_at: float
    recovered_at: Optional[float]

    def __post_init__(self) -> None:
        if self.schema != _CONTEXT_REBASE_EPISODE_SCHEMA:
            raise ContextContinuationError("CONTEXT_REBASE_EPISODE_SCHEMA")
        _identity(self.conversation_root, "INVALID_CONVERSATION_ROOT")
        _nonnegative_int(
            self.attempts_without_recovery,
            "INVALID_CONTEXT_REBASE_EPISODE_ATTEMPTS",
        )
        if self.last_transition_id is not None:
            _identity(self.last_transition_id, "INVALID_TRANSITION_ID")
        for value in (self.last_before_tokens, self.last_after_tokens):
            if value is not None:
                _nonnegative_int(value, "INVALID_CONTEXT_REBASE_EPISODE_TOKENS")
        if type(self.updated_at) not in (int, float) or self.updated_at <= 0:
            raise ContextContinuationError("CONTEXT_REBASE_EPISODE_INVALID")
        if self.recovered_at is not None and (
            type(self.recovered_at) not in (int, float) or self.recovered_at <= 0
        ):
            raise ContextContinuationError("CONTEXT_REBASE_EPISODE_INVALID")

    @classmethod
    def from_raw(cls, raw: str) -> "ContextRebaseEpisode":
        value = _strict_json(raw)
        expected = {
            "schema", "conversation_root", "attempts_without_recovery",
            "last_transition_id", "last_before_tokens", "last_after_tokens",
            "updated_at", "recovered_at",
        }
        if set(value) != expected:
            raise ContextContinuationError("CONTEXT_REBASE_EPISODE_INVALID")
        try:
            return cls(**value)
        except TypeError as exc:
            raise ContextContinuationError("CONTEXT_REBASE_EPISODE_INVALID") from exc

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
    control_revision: int
    first_user: Optional[Dict[str, Any]]
    authentic_users: tuple[Dict[str, Any], ...]
    current_users: tuple[Dict[str, Any], ...]
    latest_summary: Optional[Dict[str, Any]]
    recent_events: tuple[Dict[str, Any], ...]
    unresolved_effects: tuple[Dict[str, Any], ...]
    goal_raw: Optional[str]
    heartbeat_raw: Optional[str]
    loop_raw: Optional[str]
    todo_json: Optional[str]
    run_custodies: tuple[Dict[str, Any], ...] = ()
    read_limits: tuple[int, int, int, int] = (12, 32, 32, 128)
    control_raw: Optional[str] = None
    input_control_raw: Optional[str] = None

    @property
    def has_pending_inputs(self) -> bool:
        head = {} if self.input_control_raw is None else _strict_json(self.input_control_raw)
        return head.get("accepted_sequence", 0) != head.get("projected_sequence", 0)

    @property
    def dispatch_stopped(self) -> bool:
        if self.control_raw is None:
            return False
        control = _strict_json(self.control_raw)
        if (control.get("schema") not in {"SessionDBContextControlV1", "SessionDBContextControlV2"}
                or type(control.get("input_watermark")) is not int
                or self.control_revision <= control["input_watermark"]):
            return True
        if control["schema"] == "SessionDBContextControlV2":
            # A queued event accepted before stop is not a subsequent user
            # instruction merely because its transcript row is inserted later.
            head = {} if self.input_control_raw is None else _strict_json(self.input_control_raw)
            sequence = control.get("input_sequence")
            if type(sequence) is not int or sequence < 0:
                return True
            if head and head["projected_sequence"] <= sequence:
                return True
        return False

    @property
    def has_unresolved_effects(self) -> bool:
        return bool(self.unresolved_effects or any(
            value["checkpoint"]["unresolved_effects"] for value in self.run_custodies
        ))

    @property
    def digest(self) -> str:
        """Exact bounded local read-set identity, not external-owner authority."""
        return "sha256:" + hashlib.sha256(_canonical(asdict(self)).encode("utf-8")).hexdigest()

    @property
    def action_control_digest(self) -> str:
        """Control binding that survives this response's assistant/tool rows."""
        value = {"session_id": self.session_id, "conversation_root": self.conversation_root,
                 "authentic_users": self.authentic_users, "control_raw": self.control_raw,
                 "goal_raw": self.goal_raw, "heartbeat_raw": self.heartbeat_raw,
                 "loop_raw": self.loop_raw, "input_control_raw": self.input_control_raw}
        return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()

class SessionContextContinuityMixin:
    """Local SessionDB continuity owner; external activation remains separate."""

    def context_dispatch_required_for_session(self, session_id):
        """Continue enforcing a committed lineage when publication is disabled."""
        _identity(session_id, "INVALID_CHILD_SESSION")
        with self._read_ctx() as conn:
            conn.execute("SAVEPOINT context_dispatch_required")
            try:
                if self._context_input_control_on_conn(conn, session_id) is not None:
                    return True
                for sid in self._context_rebase_lineage_on_conn(conn, session_id):
                    row = conn.execute("SELECT model_config,end_reason FROM sessions WHERE id=?", (sid,)).fetchone()
                    if row is None:
                        continue
                    config = _strict_json(row["model_config"] or "{}")
                    if (row["end_reason"] == _CONTEXT_REBASE_END_REASON
                            or "_context_rebase_transition" in config
                            or "_context_rebase_from" in config):
                        return True
                return False
            finally:
                conn.execute("ROLLBACK TO context_dispatch_required")
                conn.execute("RELEASE context_dispatch_required")

    def _context_rebase_key(self, transition_id: str) -> str:
        return _CONTEXT_REBASE_KEY_PREFIX + _identity(transition_id, "INVALID_TRANSITION_ID")

    @staticmethod
    def _context_rebase_episode_key(conversation_root: str) -> str:
        return (
            _CONTEXT_REBASE_EPISODE_KEY_PREFIX
            + _identity(conversation_root, "INVALID_CONVERSATION_ROOT")
        )

    def read_context_rebase_episode(self, session_id: str) -> ContextRebaseEpisode:
        """Read durable no-progress state for the whole logical conversation."""
        _identity(session_id, "INVALID_CHILD_SESSION")
        with self._read_ctx() as conn:
            root = str(self._session_turn_lease_key_on_conn(conn, session_id))
            key = self._context_rebase_episode_key(root)
            row = conn.execute(
                "SELECT value FROM state_meta WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            now = time.time()
            return ContextRebaseEpisode(
                _CONTEXT_REBASE_EPISODE_SCHEMA,
                root,
                0,
                None,
                None,
                None,
                now,
                now,
            )
        return ContextRebaseEpisode.from_raw(row[0])

    def reset_context_rebase_episode(self, session_id):
        """Unattributed health/progress claims cannot replenish task attempts."""
        raise ContextContinuationError("CONTEXT_REBASE_PROGRESS_EVIDENCE_REQUIRED")

    def _reset_context_rebase_episode_on_conn(self, conn, session_id):
        root = str(self._session_turn_lease_key_on_conn(conn, session_id))
        key = self._context_rebase_episode_key(root)
        row = conn.execute(
            "SELECT value FROM state_meta WHERE key=?", (key,)
        ).fetchone()
        old = (
            None
            if row is None
            else ContextRebaseEpisode.from_raw(row[0])
        )
        now = time.time()
        updated = ContextRebaseEpisode(
            _CONTEXT_REBASE_EPISODE_SCHEMA,
            root,
            0,
            None if old is None else old.last_transition_id,
            None if old is None else old.last_before_tokens,
            None if old is None else old.last_after_tokens,
            now,
            now,
        )
        if row is None:
            conn.execute(
                "INSERT INTO state_meta(key,value) VALUES(?,?)",
                (key, updated.raw()),
            )
        else:
            cursor = conn.execute(
                "UPDATE state_meta SET value=? WHERE key=? AND value=?",
                (updated.raw(), key, row[0]),
            )
            if cursor.rowcount != 1:
                raise ContextContinuationError(
                    "CONTEXT_REBASE_EPISODE_CHANGED"
                )
        return updated


    def read_context_resume_basis(self, session_id):
        """Read the exact goal/episode CAS pair for an operator command."""
        def read(conn):
            root = str(self._session_turn_lease_key_on_conn(conn, session_id))
            def raw(key):
                row = conn.execute("SELECT value FROM state_meta WHERE key=?", (key,)).fetchone()
                return None if row is None else row[0]
            return {"expected_goal_raw": raw("goal:" + session_id),
                    "expected_episode_raw": raw(self._context_rebase_episode_key(root))}
        return self._execute_write(read)

    def read_context_resume_outcome(self, session_id, operator_action_id):
        """Resolve a lost command ACK without repeating its mutation."""
        def read(conn):
            goal = conn.execute("SELECT value FROM state_meta WHERE key=?", ("goal:" + session_id,)).fetchone()
            receipt = conn.execute("SELECT value FROM state_meta WHERE key=?",
                                   ("context-operator-resume:" + operator_action_id,)).fetchone()
            raw = None if goal is None else goal[0]
            if receipt is None:
                return raw, False
            value = _strict_json(receipt[0])
            root = str(self._session_turn_lease_key_on_conn(conn, session_id))
            if (value.get("schema") != "SessionDBContextOperatorResumeV1"
                    or value.get("action_id") != operator_action_id
                    or value.get("session_id") != session_id
                    or value.get("conversation_root") != root):
                raise ContextContinuationError("CONTEXT_REBASE_PROGRESS_RECEIPT_INVALID")
            digest = None if raw is None else "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
            return raw, digest == value.get("resumed_goal_digest")
        return self._execute_write(read)

    def resume_context_goal(self, session_id, *, expected_goal_raw, expected_episode_raw,
                            reset_budget=False, operator_action_id):
        """Commit an explicit goal-owner resume and its one-use episode rearm.

        Only the existing operator resume path calls this operation. Provider
        response health and model-authored evidence never enter this API.
        The owner builds the checkpoint; a supplied reason string is not proof.
        """
        import uuid
        from hermes_cli.goals import goal_resume_state

        _identity(session_id, "INVALID_CHILD_SESSION")
        try:
            action = uuid.UUID(operator_action_id)
            if action.version != 4 or str(action) != operator_action_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ContextContinuationError("INVALID_OPERATOR_ACTION") from None
        if type(reset_budget) is not bool:
            raise ContextContinuationError("INVALID_OPERATOR_ACTION")
        _strict_json(expected_goal_raw)

        def write(conn):
            session = conn.execute("SELECT ended_at FROM sessions WHERE id=?", (session_id,)).fetchone()
            if session is None or session["ended_at"] is not None:
                raise ContextContinuationError("CONTEXT_REBASE_PARENT_NOT_LIVE")
            root = str(self._session_turn_lease_key_on_conn(conn, session_id))
            receipt_key = "context-operator-resume:" + operator_action_id
            if conn.execute("SELECT 1 FROM state_meta WHERE key=?", (receipt_key,)).fetchone() is not None:
                raise ContextContinuationError("CONTEXT_REBASE_PROGRESS_ALREADY_CONSUMED")
            goal_key = "goal:" + session_id
            before_episode = conn.execute("SELECT value FROM state_meta WHERE key=?",
                                          (self._context_rebase_episode_key(root),)).fetchone()
            if (None if before_episode is None else before_episode[0]) != expected_episode_raw:
                raise ContextContinuationError("CONTEXT_REBASE_EPISODE_CHANGED")
            resumed = goal_resume_state(expected_goal_raw, reset_budget=reset_budget,
                                        operator_action_id=operator_action_id)
            after = resumed.to_json()
            if not self._compare_and_set_meta_many_on_conn(conn, [(goal_key, expected_goal_raw, after)]):
                raise ContextContinuationError("CONTEXT_REBASE_GOAL_CHANGED")
            updated = self._reset_context_rebase_episode_on_conn(conn, session_id)
            receipt = {"schema": "SessionDBContextOperatorResumeV1", "action_id": operator_action_id,
                       "conversation_root": root, "session_id": session_id,
                       "goal_id": resumed.goal_id, "checkpoint_revision": resumed.checkpoint_revision,
                       "previous_goal_digest": "sha256:" + hashlib.sha256(expected_goal_raw.encode()).hexdigest(),
                       "resumed_goal_digest": "sha256:" + hashlib.sha256(after.encode()).hexdigest(),
                       "previous_episode": None if before_episode is None else _strict_json(before_episode[0]),
                       "episode": _strict_json(updated.raw()), "consumed_at": time.time()}
            conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)", (receipt_key, _canonical(receipt)))
            return after
        return self._execute_write(write)

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

    def _context_rebase_lineage_on_conn(self, conn, session_id):
        # Only traverse canonical continuation parents. Explicit branches
        # have their own copied transcript and therefore root at themselves.
        lineage = [session_id]
        current = session_id
        seen = {current}
        for _ in range(1000):
            row = conn.execute(
                "SELECT parent_session_id,source,model_config FROM sessions WHERE id=?",
                (current,),
            ).fetchone()
            if row is None or not row["parent_session_id"]:
                break
            if self._is_explicit_fork_child_row(dict(row)):
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

        return lineage

    def read_context_rebase_snapshot(
        self,
        session_id: str,
        *,
        recent_limit: int = 12,
        user_limit: int = 32,
        unresolved_effect_limit: int = 32,
        authentic_user_limit: int = 128,
    ) -> ContextRebaseSnapshot:
        """Read one bounded compilation snapshot under a single SQLite read context."""
        _identity(session_id, "INVALID_PARENT_SESSION")
        if type(recent_limit) is not int or not 1 <= recent_limit <= 64:
            raise ContextContinuationError("INVALID_RECENT_LIMIT")
        if type(user_limit) is not int or not 1 <= user_limit <= 128:
            raise ContextContinuationError("INVALID_USER_LIMIT")
        if (
            type(unresolved_effect_limit) is not int
            or not 1 <= unresolved_effect_limit <= 128
        ):
            raise ContextContinuationError("INVALID_UNRESOLVED_EFFECT_LIMIT")
        if (
            type(authentic_user_limit) is not int
            or not 1 <= authentic_user_limit <= 512
        ):
            raise ContextContinuationError("INVALID_AUTHENTIC_USER_LIMIT")

        with self._read_ctx() as conn:
            # _read_ctx owns connection checkout/locking, not a SQLite snapshot.
            # A savepoint composes with a borrowed transaction and also leaves
            # pooled readers clean on every success/refusal path.
            conn.execute("SAVEPOINT context_rebase_snapshot")
            try:
                return self._read_context_rebase_snapshot_on_conn(
                    conn, session_id, recent_limit=recent_limit,
                    user_limit=user_limit, unresolved_effect_limit=unresolved_effect_limit,
                    authentic_user_limit=authentic_user_limit,
                )
            finally:
                conn.execute("ROLLBACK TO context_rebase_snapshot")
                conn.execute("RELEASE context_rebase_snapshot")

    def _read_context_rebase_snapshot_on_conn(
        self, conn, session_id, *, recent_limit=12, user_limit=32,
        unresolved_effect_limit=32, authentic_user_limit=128,
    ) -> ContextRebaseSnapshot:
        """Caller owns one read snapshot or the publication write transaction."""
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

        lineage = self._context_rebase_lineage_on_conn(conn, session_id)

        # Build one exact human-originated instruction ledger across the
        # canonical continuation lineage. Physical rebase replay rows carry
        # the same content into a child; deduplicate only those exact replay
        # clones so an epoch boundary cannot multiply user authority.
        authentic_user_list = []
        authentic_seen = set()
        for sid in reversed(lineage):
            rows = conn.execute(
                "SELECT id,content,timestamp,display_kind,display_metadata "
                "FROM messages WHERE session_id=? AND active=1 AND role='user' "
                "ORDER BY id ASC",
                (sid,),
            ).fetchall()
            for row in rows:
                candidate = {
                    "role": "user",
                    "content": self._decode_content(row["content"]),
                    "timestamp": row["timestamp"],
                }
                if row["display_kind"]:
                    candidate["display_kind"] = row["display_kind"]
                if row["display_metadata"]:
                    decoded_meta = self._decode_display_metadata(
                        row["display_metadata"]
                    )
                    if decoded_meta is not None:
                        candidate["display_metadata"] = decoded_meta
                live_view = user_originated_turn_view(candidate)
                if live_view is None:
                    continue
                try:
                    canonical_content = _canonical(live_view.get("content"))
                except ContextContinuationError:
                    canonical_content = repr(live_view.get("content"))
                replay_key = (
                    live_view.get("timestamp"),
                    canonical_content,
                )
                if replay_key in authentic_seen:
                    continue
                authentic_seen.add(replay_key)
                authentic_user_list.append({
                    "row_id": int(row["id"]),
                    "session_id": sid,
                    "content": live_view.get("content"),
                    "timestamp": live_view.get("timestamp"),
                })
                if len(authentic_user_list) > authentic_user_limit:
                    raise ContextContinuationError(
                        "TOO_MANY_AUTHENTIC_USER_INSTRUCTIONS"
                    )
        authentic_users = tuple(authentic_user_list)
        if not authentic_users:
            raise ContextContinuationError(
                "CONTEXT_REBASE_AUTHENTIC_USER_ANCHOR_MISSING"
            )
        first_user = authentic_users[0]
        control_revision = int(authentic_users[-1]["row_id"])

        summary = conn.execute(
            "SELECT id,content,timestamp,display_metadata FROM messages "
            "WHERE session_id=? AND active=1 AND _compressed_summary=1 "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        summary_id = int(summary["id"]) if summary is not None else 0
        summary_metadata = (
            self._decode_display_metadata(summary["display_metadata"])
            if summary is not None and summary["display_metadata"]
            else None
        )
        is_derived_rebase_brief = bool(
            isinstance(summary_metadata, dict)
            and summary_metadata.get("continuation_kind")
            == "context_rebase_brief"
        )
        latest_summary = (
            None
            if summary is None or is_derived_rebase_brief
            else {
                "row_id": summary_id,
                "content": self._decode_content(summary["content"]),
                "timestamp": summary["timestamp"],
            }
        )

        user_rows = conn.execute(
            "SELECT id,content,timestamp,display_kind,display_metadata "
            "FROM messages WHERE session_id=? AND active=1 AND role='user' AND id>? "
            "ORDER BY id ASC LIMIT ?",
            (session_id, summary_id, user_limit + 1),
        ).fetchall()
        if len(user_rows) > user_limit:
            raise ContextContinuationError("TOO_MANY_UNSUMMARIZED_USER_CHANGES")
        current_users_list = []
        for row in user_rows:
            item = {
                "row_id": int(row["id"]),
                "content": self._decode_content(row["content"]),
                "timestamp": row["timestamp"],
                "display_kind": row["display_kind"],
            }
            decoded_meta = (
                self._decode_display_metadata(row["display_metadata"])
                if row["display_metadata"]
                else None
            )
            if decoded_meta is not None:
                item["display_metadata"] = decoded_meta
            live_view = user_originated_turn_view({
                "role": "user",
                "content": item["content"],
                "timestamp": item["timestamp"],
                **(
                    {"display_kind": item["display_kind"]}
                    if item["display_kind"]
                    else {}
                ),
                **(
                    {"display_metadata": decoded_meta}
                    if decoded_meta is not None
                    else {}
                ),
            })
            item["authentic_user"] = live_view is not None
            current_users_list.append(item)
        current_users = tuple(current_users_list)
        if not current_users:
            raise ContextContinuationError("CONTEXT_REBASE_USER_ANCHOR_MISSING")

        event_rows = conn.execute(
            "SELECT id,role,content,tool_name,tool_call_id,effect_disposition,observed,"
            "finish_reason,timestamp,_compressed_summary "
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
            "effect_disposition": row["effect_disposition"],
            "observed": bool(row["observed"]),
            "finish_reason": row["finish_reason"],
            "timestamp": row["timestamp"],
            "compressed_summary": bool(row["_compressed_summary"]),
        } for row in reversed(event_rows))

        unresolved_effects_list = []
        for sid in reversed(lineage):
            rows = conn.execute(
                "SELECT id,role,content,tool_name,tool_call_id,effect_disposition,"
                "observed,finish_reason,timestamp,_compressed_summary "
                "FROM messages WHERE session_id=? AND active=1 AND role='tool' "
                "AND effect_disposition='unknown' ORDER BY id ASC LIMIT ?",
                (sid, unresolved_effect_limit + 1),
            ).fetchall()
            for row in rows:
                unresolved_effects_list.append({
                    "row_id": int(row["id"]),
                    "session_id": sid,
                    "role": row["role"],
                    "content": self._decode_content(row["content"]),
                    "tool_name": row["tool_name"],
                    "tool_call_id": row["tool_call_id"],
                    "effect_disposition": row["effect_disposition"],
                    "observed": bool(row["observed"]),
                    "finish_reason": row["finish_reason"],
                    "timestamp": row["timestamp"],
                    "compressed_summary": bool(row["_compressed_summary"]),
                })
                if len(unresolved_effects_list) > unresolved_effect_limit:
                    raise ContextContinuationError(
                        "TOO_MANY_UNRESOLVED_EFFECTS"
                    )
        unresolved_effects = tuple(unresolved_effects_list)

        goal_row = conn.execute(
            "SELECT value FROM state_meta WHERE key=?", (f"goal:{session_id}",),
        ).fetchone()
        goal_raw = None if goal_row is None else goal_row[0]
        heartbeat_row = conn.execute(
            "SELECT value FROM state_meta WHERE key=?",
            (f"heartbeat:{session_id}",),
        ).fetchone()
        heartbeat_raw = None if heartbeat_row is None else heartbeat_row[0]
        loop_row = conn.execute(
            "SELECT value FROM state_meta WHERE key=?",
            (f"loop:{session_id}",),
        ).fetchone()
        loop_raw = None if loop_row is None else loop_row[0]
        todo = self._current_todo_snapshot_on_conn(conn, session_id)
        todo_json = None if todo is None else todo["todos_json"]

        # Recheck the local read coordinates inside this same read transaction.
        final = conn.execute(
            "SELECT COALESCE(MAX(id),0) AS watermark FROM messages WHERE session_id=? AND active=1",
            (session_id,),
        ).fetchone()
        if int(final["watermark"] if final else 0) != watermark:
            raise ContextContinuationError("CONTEXT_REBASE_SNAPSHOT_CHANGED")
        custody = self._run_custodies_for_session_on_conn(conn, session_id)
        safe_custody = tuple({
            "run_id": value.run_id,
            "generation": value.generation,
            "origin_session_id": value.origin_session_id,
            "current_session_id": value.current_session_id,
            "checkpoint": asdict(value.checkpoint),
        } for _, value in sorted(custody.values(), key=lambda item: item[1].run_id))
        control_row = conn.execute("SELECT value FROM state_meta WHERE key=?", (
            "context-control:" + str(root),
        )).fetchone()
        return ContextRebaseSnapshot(
            session_id, str(root), session["profile_name"], session["cwd"],
            session["git_branch"], session["git_repo_root"], watermark,
            control_revision, first_user, authentic_users, current_users,
            latest_summary, recent_events, unresolved_effects, goal_raw,
            heartbeat_raw, loop_raw, todo_json, safe_custody,
            (recent_limit, user_limit, unresolved_effect_limit, authentic_user_limit),
            None if control_row is None else control_row[0],
            self._context_input_control_on_conn(conn, session_id),
        )

    def record_context_stop(self, session_id):
        """Linearize an explicit user stop with the existing dispatch owner."""
        def write(conn):
            root = str(self._session_turn_lease_key_on_conn(conn, session_id))
            key = "context-control:" + root
            row = conn.execute("SELECT value FROM state_meta WHERE key=?", (key,)).fetchone()
            previous = {} if row is None else _strict_json(row[0])
            watermark = conn.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
            input_raw = self._context_input_control_on_conn(conn, session_id)
            sequence = 0 if input_raw is None else _strict_json(input_raw)["accepted_sequence"]
            value = {"schema": "SessionDBContextControlV2", "revision": previous.get("revision", 0) + 1,
                     "stopped": True, "stopped_at": time.time(), "input_watermark": watermark,
                     "input_sequence": sequence}
            conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, _canonical(value)))
            return value
        return self._execute_write(write)

    def admit_context_dispatch(self, session_id, *, turn_lease_holder, attempt_id,
                               expected_snapshot_digest, payload_digest, route_ref):
        """Seal one final request and consume its intent in the native owner.

        The admitted record is immutable. Repeated delivery never replays an
        effect; a caller must settle the existing attempt through owner state.
        """
        _digest(expected_snapshot_digest)
        _digest(payload_digest)
        _identity(attempt_id, "INVALID_DISPATCH_ATTEMPT")
        if type(route_ref) is not str or not route_ref or len(route_ref) > 2048:
            raise ContextContinuationError("INVALID_DISPATCH_ROUTE")
        key = "context-dispatch:" + attempt_id

        def write(conn):
            self._assert_context_rebase_lease_on_conn(conn, session_id, turn_lease_holder)
            snapshot = self._read_context_rebase_snapshot_on_conn(conn, session_id)
            if snapshot.has_pending_inputs:
                raise ContextContinuationError("CONTEXT_DISPATCH_INPUT_PENDING")
            if snapshot.dispatch_stopped:
                raise ContextContinuationError("CONTEXT_DISPATCH_STOPPED")
            if snapshot.digest != expected_snapshot_digest:
                raise ContextContinuationError("CONTEXT_DISPATCH_STALE_MATERIALIZATION")
            if snapshot.has_unresolved_effects:
                raise ContextContinuationError("CONTEXT_DISPATCH_UNRESOLVED_EFFECTS")
            prior = conn.execute("SELECT value FROM state_meta WHERE key=?", (key,)).fetchone()
            if prior is not None:
                old = _strict_json(prior[0])
                if old.get("payload_digest") != payload_digest or old.get("session_id") != session_id:
                    raise ContextContinuationError("CONTEXT_DISPATCH_INTENT_COLLISION")
                raise ContextContinuationError("CONTEXT_DISPATCH_ALREADY_ADMITTED")
            record = {"schema": "SessionDBContextDispatchV1", "attempt_id": attempt_id,
                      "session_id": session_id, "conversation_root": snapshot.conversation_root,
                      "snapshot_digest": expected_snapshot_digest, "payload_digest": payload_digest,
                      "route_ref": route_ref, "control_revision": snapshot.control_revision,
                      "action_control_digest": snapshot.action_control_digest,
                      "input_watermark": snapshot.input_watermark, "admitted_at": time.time()}
            conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)", (key, _canonical(record)))
            return record
        return self._execute_write(write)

    def settle_context_dispatch_response(self, attempt_id, *, turn_lease_holder):
        """Attach response disposition to its attempt and reject stale consumption."""
        _identity(attempt_id, "INVALID_DISPATCH_ATTEMPT")

        def write(conn):
            row = conn.execute("SELECT value FROM state_meta WHERE key=?", ("context-dispatch:" + attempt_id,)).fetchone()
            if row is None:
                raise ContextContinuationError("CONTEXT_DISPATCH_NOT_ADMITTED")
            admitted = _strict_json(row[0])
            key = "context-dispatch-result:" + attempt_id
            if conn.execute("SELECT 1 FROM state_meta WHERE key=?", (key,)).fetchone() is not None:
                return "CONTEXT_DISPATCH_RESPONSE_ALREADY_SETTLED"
            reason = None
            try:
                self._assert_context_rebase_lease_on_conn(conn, admitted["session_id"], turn_lease_holder)
                snapshot = self._read_context_rebase_snapshot_on_conn(conn, admitted["session_id"])
                if snapshot.digest != admitted["snapshot_digest"]:
                    reason = "CONTEXT_DISPATCH_RESPONSE_SUPERSEDED"
            except ContextContinuationError as exc:
                reason = exc.code
            result = {"schema": "SessionDBContextDispatchResultV1", "attempt_id": attempt_id,
                      "payload_digest": admitted["payload_digest"], "settled_at": time.time(),
                      "disposition": "quarantined" if reason else "response_received",
                      "reason": reason}
            conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)", (key, _canonical(result)))
            return reason
        reason = self._execute_write(write)
        if reason is not None:
            raise ContextContinuationError(reason)

    def assert_context_dispatch_current(self, attempt_id, *, turn_lease_holder):
        """Fence each buffered response delivery against the admitted source."""
        _identity(attempt_id, "INVALID_DISPATCH_ATTEMPT")

        def read(conn):
            row = conn.execute("SELECT value FROM state_meta WHERE key=?", ("context-dispatch:" + attempt_id,)).fetchone()
            if row is None:
                raise ContextContinuationError("CONTEXT_DISPATCH_NOT_ADMITTED")
            admitted = _strict_json(row[0])
            self._assert_context_rebase_lease_on_conn(conn, admitted["session_id"], turn_lease_holder)
            current = self._read_context_rebase_snapshot_on_conn(conn, admitted["session_id"])
            if current.digest != admitted["snapshot_digest"]:
                raise ContextContinuationError("CONTEXT_DISPATCH_RESPONSE_SUPERSEDED")
        self._execute_write(read)

    def assert_context_dispatch_control_current(self, attempt_id, *, session_id, turn_lease_holder):
        """Recheck a settled response at the local tool-dispatch boundary.

        Assistant and completed tool observations may advance the transcript;
        authentic input and controls may not change beneath its tool proposals.
        This local check does not consume or revoke an external owner's permit.
        """
        _identity(attempt_id, "INVALID_DISPATCH_ATTEMPT")

        def check(conn):
            row = conn.execute("SELECT value FROM state_meta WHERE key=?", ("context-dispatch:" + attempt_id,)).fetchone()
            result = conn.execute("SELECT value FROM state_meta WHERE key=?", ("context-dispatch-result:" + attempt_id,)).fetchone()
            if row is None or result is None:
                raise ContextContinuationError("CONTEXT_TOOL_RESPONSE_NOT_ADMITTED")
            admitted, settled = _strict_json(row[0]), _strict_json(result[0])
            if (admitted.get("session_id") != session_id
                    or settled.get("disposition") != "response_received"
                    or settled.get("payload_digest") != admitted.get("payload_digest")):
                raise ContextContinuationError("CONTEXT_TOOL_RESPONSE_NOT_ADMITTED")
            self._assert_context_rebase_lease_on_conn(conn, session_id, turn_lease_holder)
            current = self._read_context_rebase_snapshot_on_conn(conn, session_id)
            if current.action_control_digest != admitted.get("action_control_digest"):
                raise ContextContinuationError("CONTEXT_TOOL_CONTROL_SUPERSEDED")
            if current.has_unresolved_effects:
                raise ContextContinuationError("CONTEXT_DISPATCH_UNRESOLVED_EFFECTS")
        self._execute_write(check)

    def read_context_rebase_transition(self, transition_id: str) -> Optional[ContextRebaseTransition]:
        raw = self.get_meta(self._context_rebase_key(transition_id))
        return None if raw is None else ContextRebaseTransition.from_raw(raw)

    def context_rebase_transition_for_session(
        self, session_id: str
    ) -> Optional[ContextRebaseTransition]:
        """Resolve a marker-bound child to its durable transition record."""
        _identity(session_id, "INVALID_CHILD_SESSION")
        row = self.get_session(session_id)
        if not isinstance(row, dict):
            return None
        config = row.get("model_config") or {}
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except (TypeError, ValueError):
                raise ContextContinuationError("CONTEXT_EPOCH_METADATA_INVALID") from None
        if not isinstance(config, dict):
            raise ContextContinuationError("CONTEXT_EPOCH_METADATA_INVALID")
        transition_id = config.get("_context_rebase_transition")
        parent_id = config.get("_context_rebase_from")
        if transition_id is None and parent_id is None:
            return None
        if not isinstance(transition_id, str) or not isinstance(parent_id, str):
            raise ContextContinuationError("CONTEXT_REBASE_MARKER_INVALID")
        transition = self.read_context_rebase_transition(transition_id)
        if transition is None:
            raise ContextContinuationError("CONTEXT_REBASE_RECORD_MISSING")
        if (
            transition.child_session_id != session_id
            or transition.parent_session_id != parent_id
        ):
            raise ContextContinuationError("CONTEXT_REBASE_COMMIT_CORRUPT")
        return transition

    def assert_context_rebase_ready_for_turn(
        self, session_id: str
    ) -> Optional[ContextRebaseTransition]:
        """Fail closed when a committed successor is not READY for normal work."""
        transition = self.context_rebase_transition_for_session(session_id)
        if transition is None:
            return None
        if transition.state != "ready":
            raise ContextContinuationError("CONTEXT_REBASE_NOT_READY")
        return transition

    def _assert_context_rebase_lease_on_conn(self, conn, session_id, holder):
        root = self._session_turn_lease_key_on_conn(conn, session_id)
        lease = conn.execute(
            "SELECT holder,expires_at FROM session_turn_leases WHERE conversation_id=?", (root,)
        ).fetchone()
        try:
            expiry = float(lease["expires_at"]) if lease is not None else 0
        except (TypeError, ValueError, OverflowError):
            expiry = 0
        if (not holder or lease is None or lease["holder"] != holder
                or not math.isfinite(expiry) or expiry <= time.time()):
            raise ContextContinuationError("TURN_LEASE_MISMATCH")

    def begin_context_rebase_recovery(self, transition_id, *, turn_lease_holder):
        """Reserve bounded deterministic reconciliation under the live turn owner.

        The wake dependency and attempts belong to the existing SessionDB
        transition, not another scheduler. Restart may retry the same intent;
        it cannot mint a fresh child or reset its deadline and attempt budget.
        """
        key = self._context_rebase_key(transition_id)

        def write(conn):
            row = conn.execute("SELECT value FROM state_meta WHERE key=?", (key,)).fetchone()
            if row is None:
                raise ContextContinuationError("CONTEXT_REBASE_NOT_FOUND")
            transition = ContextRebaseTransition.from_raw(row[0])
            self._assert_context_rebase_lease_on_conn(conn, transition.child_session_id, turn_lease_holder)
            if transition.state not in {"committed_pending_activation", "reconciliation_required"}:
                raise ContextContinuationError("CONTEXT_REBASE_NOT_ACTIVATABLE")
            recovery_key = _CONTEXT_REBASE_RECOVERY_PREFIX + transition_id
            row = conn.execute("SELECT value FROM state_meta WHERE key=?", (recovery_key,)).fetchone()
            if row is None:
                raise ContextContinuationError("CONTEXT_REBASE_LEGACY_RECOVERY_REQUIRED")
            recovery = _strict_json(row[0])
            if (recovery.get("schema") != "SessionDBContextRebaseRecoveryV1"
                    or recovery.get("transition_id") != transition_id
                    or recovery.get("child_session_id") != transition.child_session_id
                    or recovery.get("continuation_digest") != transition.continuation_digest
                    or type(recovery.get("attempts")) is not int
                    or type(recovery.get("deadline_at")) not in (int, float)
                    or not math.isfinite(recovery["deadline_at"])
                    or recovery["attempts"] < 0):
                raise ContextContinuationError("CONTEXT_REBASE_RECOVERY_INVALID")
            if recovery["attempts"] >= 3 or time.time() >= recovery["deadline_at"]:
                raise ContextContinuationError("CONTEXT_REBASE_RECOVERY_EXHAUSTED")
            snapshot = self._read_context_rebase_snapshot_on_conn(conn, transition.child_session_id)
            recovery["action_control_digest"] = snapshot.action_control_digest
            recovery["attempts"] += 1
            recovery["holder_digest"] = hashlib.sha256(turn_lease_holder.encode()).hexdigest()
            recovery["next_check_at"] = time.time()
            conn.execute("UPDATE state_meta SET value=? WHERE key=?", (_canonical(recovery), recovery_key))
            return recovery

        return self._execute_write(write)

    def _assert_context_recovery_reservation_on_conn(self, conn, session_id, holder, expected):
        """Recheck the bounded recovery reservation at each native mutation."""
        if type(expected) is not dict or set(expected) != {"transition_id", "attempt", "control_digest"}:
            raise ContextContinuationError("CONTEXT_REBASE_RECOVERY_FENCE_MISMATCH")
        key = self._context_rebase_key(expected["transition_id"])
        row = conn.execute("SELECT value FROM state_meta WHERE key=?", (key,)).fetchone()
        if row is None:
            raise ContextContinuationError("CONTEXT_REBASE_NOT_FOUND")
        transition = ContextRebaseTransition.from_raw(row[0])
        if (transition.child_session_id != session_id
                or transition.state not in {"committed_pending_activation", "reconciliation_required"}):
            raise ContextContinuationError("CONTEXT_REBASE_NOT_ACTIVATABLE")
        row = conn.execute("SELECT value FROM state_meta WHERE key=?", (
            _CONTEXT_REBASE_RECOVERY_PREFIX + expected["transition_id"],
        )).fetchone()
        recovery = {} if row is None else _strict_json(row[0])
        if (recovery.get("schema") != "SessionDBContextRebaseRecoveryV1"
                or recovery.get("transition_id") != transition.transition_id
                or recovery.get("child_session_id") != session_id
                or recovery.get("continuation_digest") != transition.continuation_digest
                or type(expected["attempt"]) is not int or not 1 <= expected["attempt"] <= 3
                or recovery.get("attempts") != expected["attempt"]
                or recovery.get("action_control_digest") != expected["control_digest"]
                or recovery.get("holder_digest") != hashlib.sha256(holder.encode()).hexdigest()):
            raise ContextContinuationError("CONTEXT_REBASE_RECOVERY_FENCE_MISMATCH")
        if (type(recovery.get("deadline_at")) not in (int, float)
                or not math.isfinite(recovery["deadline_at"])
                or time.time() >= recovery["deadline_at"]):
            raise ContextContinuationError("CONTEXT_REBASE_RECOVERY_EXHAUSTED")

    def publish_context_rebase_child(
        self,
        *,
        transition_id: str,
        parent_session_id: str,
        child_session_id: str,
        continuation_digest: str,
        expected_snapshot_digest: str,
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
        custody_transfers: tuple = (),
        snapshot_read_limits: tuple = (12, 32, 32, 128),
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
        _digest(expected_snapshot_digest)
        _nonnegative_int(control_revision, "INVALID_CONTROL_REVISION")
        _nonnegative_int(input_watermark, "INVALID_INPUT_WATERMARK")
        if type(turn_lease_holder) is not str or not turn_lease_holder:
            raise ContextContinuationError("TURN_LEASE_REQUIRED")
        if type(messages) is not list or not messages:
            raise ContextContinuationError("CONTEXT_REBASE_EMPTY_CHILD")
        if type(model_config) not in (dict, type(None)):
            raise ContextContinuationError("CONTEXT_REBASE_MODEL_CONFIG")
        if (type(snapshot_read_limits) is not tuple or len(snapshot_read_limits) != 4
                or any(type(n) is not int or not 1 <= n <= maximum
                       for n, maximum in zip(snapshot_read_limits, (64, 128, 128, 512)))):
            raise ContextContinuationError("INVALID_SNAPSHOT_READ_LIMITS")

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
                config = json.loads(child["model_config"] or "{}")
                if config.get("_context_rebase_snapshot_digest") != expected_snapshot_digest:
                    raise ContextContinuationError("CONTEXT_REBASE_IDEMPOTENCY_MISMATCH")
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

            # Compare all compiled SessionDB observations under this same
            # BEGIN IMMEDIATE, before any successor row or parent closure.
            try:
                current_snapshot = self._read_context_rebase_snapshot_on_conn(
                    conn, parent_session_id, **dict(zip(
                        ("recent_limit", "user_limit", "unresolved_effect_limit", "authentic_user_limit"),
                        snapshot_read_limits,
                    )),
                )
            except ContextContinuationError:
                raise ContextContinuationError("CONTEXT_REBASE_STALE_SNAPSHOT") from None
            if (current_snapshot.digest != expected_snapshot_digest
                    or current_snapshot.control_revision != control_revision):
                raise ContextContinuationError("CONTEXT_REBASE_STALE_SNAPSHOT")
            if current_snapshot.has_pending_inputs:
                raise ContextContinuationError("CONTEXT_REBASE_INPUT_PENDING")

            parent_epoch = self._context_epoch_from_model_config(parent["model_config"])
            child_epoch = parent_epoch + 1
            config = dict(model_config or {})
            reserved = {
                "_context_rebase_from": parent_session_id,
                "_context_rebase_transition": transition_id,
                "_context_epoch": child_epoch,
                "_context_rebase_snapshot_digest": expected_snapshot_digest,
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
            self._transfer_context_rebase_custodies_on_conn(
                conn, old_session_id=parent_session_id, new_session_id=child_session_id,
                expected=custody_transfers, lease_holder=turn_lease_holder,
            )
            self._migrate_context_rebase_owners_on_conn(conn, parent_session_id, child_session_id)
            recovery = {
                "schema": "SessionDBContextRebaseRecoveryV1",
                "transition_id": transition_id,
                "child_session_id": child_session_id,
                "continuation_digest": continuation_digest,
                "attempts": 0,
                "holder_digest": None,
                "wake_dependency": "live_turn_lease_and_owner_reconciliation",
                "next_check_at": now,
                "deadline_at": now + 900,
            }
            conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)", (
                _CONTEXT_REBASE_RECOVERY_PREFIX + transition_id, _canonical(recovery),
            ))
            return transition, row_ids

        transition, row_ids = self._execute_write(_do)
        if row_ids is not None:
            self._publish_message_row_ids(messages, row_ids)
        return transition

    def _migrate_context_rebase_owners_on_conn(self, conn, parent_session_id, child_session_id):
        """Apply owner-defined transformations in the child publication transaction."""
        from hermes_cli.goals import GoalState, goal_session_migration
        from hermes_cli.heartbeat import HeartbeatState, heartbeat_session_migration
        from hermes_cli.loops import LoopState, loop_session_migration

        def raw(key):
            row = conn.execute("SELECT value FROM state_meta WHERE key=?", (key,)).fetchone()
            return None if row is None else row[0]

        changes = []
        for owner, state_type, migrate in (
            ("goal", GoalState, goal_session_migration),
            ("heartbeat", HeartbeatState, heartbeat_session_migration),
            ("loop", LoopState, loop_session_migration),
        ):
            try:
                parent_raw = raw(f"{owner}:{parent_session_id}")
                child_raw = raw(f"{owner}:{child_session_id}")
                migrated, updates = migrate(
                    parent_session_id, child_session_id,
                    parent_raw, child_raw,
                    **({"reason": "context_rebase"} if owner == "goal" else {}),
                )
                required = parent_raw is not None and state_type.from_json(parent_raw).status != "cleared"
                if not migrated and (required or child_raw is not None):
                    raise ValueError("unacknowledged owner transfer")
            except Exception:
                raise ContextContinuationError(f"{owner.upper()}_RECONCILIATION_FAILED") from None
            changes.extend(updates)
        if not self._compare_and_set_meta_many_on_conn(conn, changes):
            raise ContextContinuationError("CONTEXT_REBASE_OWNER_CHANGED")

    def mark_context_rebase_ready(
        self,
        transition_id: str,
        *,
        expected_continuation_digest: str,
        expected_child_session_id: str,
        before_tokens: Optional[int] = None,
        after_tokens: Optional[int] = None,
        turn_lease_holder: Optional[str] = None,
        recovery_attempt: Optional[int] = None,
        expected_control_digest: Optional[str] = None,
        expected_custody: tuple = (),
    ) -> ContextRebaseTransition:
        """Mark the local transition ready after external owner reconciliation."""
        key = self._context_rebase_key(transition_id)
        _digest(expected_continuation_digest)
        _identity(expected_child_session_id, "INVALID_CHILD_SESSION")
        if (before_tokens is None) != (after_tokens is None):
            raise ContextContinuationError("CONTEXT_REBASE_EPISODE_TOKENS_REQUIRED")
        if before_tokens is not None:
            _nonnegative_int(before_tokens, "INVALID_CONTEXT_REBASE_EPISODE_TOKENS")
            _nonnegative_int(after_tokens, "INVALID_CONTEXT_REBASE_EPISODE_TOKENS")

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
            if old.state not in {
                "committed_pending_activation",
                "reconciliation_required",
            }:
                raise ContextContinuationError("CONTEXT_REBASE_NOT_ACTIVATABLE")
            self._assert_context_rebase_lease_on_conn(conn, old.child_session_id, turn_lease_holder)
            recovery_key = _CONTEXT_REBASE_RECOVERY_PREFIX + transition_id
            recovery_row = conn.execute("SELECT value FROM state_meta WHERE key=?", (recovery_key,)).fetchone()
            recovery = {} if recovery_row is None else _strict_json(recovery_row[0])
            if (recovery.get("schema") != "SessionDBContextRebaseRecoveryV1"
                    or recovery.get("transition_id") != old.transition_id
                    or recovery.get("child_session_id") != old.child_session_id
                    or recovery.get("continuation_digest") != old.continuation_digest
                    or type(recovery_attempt) is not int
                    or not 1 <= recovery_attempt <= 3
                    or recovery.get("attempts") != recovery_attempt
                    or recovery.get("holder_digest") != hashlib.sha256(turn_lease_holder.encode()).hexdigest()):
                raise ContextContinuationError("CONTEXT_REBASE_RECOVERY_FENCE_MISMATCH")
            if (type(recovery.get("deadline_at")) not in (int, float)
                    or not math.isfinite(recovery["deadline_at"])
                    or time.time() >= recovery["deadline_at"]):
                raise ContextContinuationError("CONTEXT_REBASE_RECOVERY_EXHAUSTED")
            if (not isinstance(expected_control_digest, str)
                    or recovery.get("action_control_digest") != expected_control_digest):
                raise ContextContinuationError("CONTEXT_REBASE_RECOVERY_CONTROL_MISMATCH")
            self._check_run_control_on_conn(conn, old.child_session_id,
                                             expected_control_digest.removeprefix("sha256:"))
            self._check_context_ready_custodies_on_conn(
                conn, old.child_session_id, expected_custody, turn_lease_holder,
            )
            recovery["completed_attempt"] = recovery_attempt
            conn.execute("UPDATE state_meta SET value=? WHERE key=?", (_canonical(recovery), recovery_key))
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

            root = str(
                self._session_turn_lease_key_on_conn(
                    conn, old.child_session_id
                )
            )
            episode_key = self._context_rebase_episode_key(root)
            episode_row = conn.execute(
                "SELECT value FROM state_meta WHERE key=?",
                (episode_key,),
            ).fetchone()
            previous = (
                None
                if episode_row is None
                else ContextRebaseEpisode.from_raw(episode_row[0])
            )
            episode = ContextRebaseEpisode(
                _CONTEXT_REBASE_EPISODE_SCHEMA,
                root,
                1 if previous is None else previous.attempts_without_recovery + 1,
                old.transition_id,
                before_tokens if before_tokens is not None else (
                    None if previous is None else previous.last_before_tokens
                ),
                after_tokens if after_tokens is not None else (
                    None if previous is None else previous.last_after_tokens
                ),
                ready.ready_at,
                None if previous is None else previous.recovered_at,
            )
            if episode_row is None:
                conn.execute(
                    "INSERT INTO state_meta(key,value) VALUES(?,?)",
                    (episode_key, episode.raw()),
                )
            else:
                ep_cursor = conn.execute(
                    "UPDATE state_meta SET value=? WHERE key=? AND value=?",
                    (episode.raw(), episode_key, episode_row[0]),
                )
                if ep_cursor.rowcount != 1:
                    raise ContextContinuationError(
                        "CONTEXT_REBASE_EPISODE_CHANGED"
                    )
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
        with self._read_ctx() as conn:
            conn.execute("SAVEPOINT context_continuation_tip")
            try:
                return self._context_continuation_tip_on_conn(conn, session_id, max_depth=max_depth)
            finally:
                conn.execute("ROLLBACK TO context_continuation_tip")
                conn.execute("RELEASE context_continuation_tip")

    def _context_continuation_tip_on_conn(self, conn, session_id, *, max_depth=1000):
        current = session_id
        seen = {current}
        for _ in range(max_depth):
            parent = conn.execute(
                "SELECT id,end_reason FROM sessions WHERE id=?", (current,),
            ).fetchone()
            if parent is None:
                return current
            reason = parent["end_reason"]
            if reason == "compression":
                row = conn.execute(
                    f"""SELECT child.id
                        FROM sessions child
                        WHERE child.parent_session_id=?
                          {self._NON_CONTINUATION_CHILD_FILTER_SQL.format(alias='child.')}
                        ORDER BY CASE WHEN child.end_reason='compression' THEN 0 WHEN child.ended_at IS NULL THEN 1 ELSE 2 END,
                                 {_sql_session_last_active('child')} DESC,
                                 child.started_at DESC, child.id DESC
                        LIMIT 1""",
                    (current, current, current),
                ).fetchone()
                child_id = None if row is None else row["id"]
            elif reason == _CONTEXT_REBASE_END_REASON:
                rows = conn.execute(
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
        row = conn.execute("SELECT end_reason FROM sessions WHERE id=?", (current,)).fetchone()
        if row is not None and row["end_reason"] in {"compression", _CONTEXT_REBASE_END_REASON}:
            raise ContextContinuationError("CONTINUATION_DEPTH_LIMIT")
        return current
