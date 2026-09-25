"""Root-scoped authentic input receipts on the existing SessionDB owner.

Acceptance is independent of the executing turn lease. Projection into the
transcript requires that lease and preserves receipt order. These records do
not authorize a provider/effect or prove that an accepted input was answered.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import math
import re
import time

from agent.context_compressor import user_originated_turn_view
from hermes_state_continuity import ContextContinuationError, _canonical, _identity, _strict_json
from hermes_state_input_turns import SessionContextInputTurnsMixin


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _prefix(root):
    return "context-inbox:" + hashlib.sha256(root.encode()).hexdigest()


def _event_key(root, sequence):
    return f"{_prefix(root)}:event:{sequence}"


_GATEWAY_ROUTE_FIELDS = frozenset({"platform", "chat_id", "chat_type", "user_id", "user_id_alt",
    "chat_id_alt", "thread_id", "scope_id", "parent_chat_id", "prospective_thread_id", "profile", "is_bot"})


def _transport_identity(value):
    # Platform IDs are opaque (negative Telegram chat IDs, Matrix @users,
    # email addresses, URL-like thread IDs). They are not native owner IDs.
    if (type(value) is not str or not value or len(value) > 2048
            or any(ord(char) < 32 for char in value)):
        raise ContextContinuationError("CONTEXT_INPUT_ORIGIN_INVALID")


def _gateway_origin(value):
    """Closed transport evidence, not a serialized authorization decision."""
    if (type(value) is not dict or set(value) != {"schema", "session_key", "route", "transport_profile",
            "transport_fingerprint", "cold_recoverable", "routed_profile"}
            or value["schema"] != "SessionDBGatewayInputOriginV1"
            or type(value["route"]) is not dict or set(value["route"]) != _GATEWAY_ROUTE_FIELDS
            or type(value["cold_recoverable"]) is not bool):
        raise ContextContinuationError("CONTEXT_INPUT_ORIGIN_INVALID")
    _transport_identity(value["session_key"])
    _identity(value["transport_profile"], "CONTEXT_INPUT_ORIGIN_INVALID")
    if value["routed_profile"] is not None:
        _identity(value["routed_profile"], "CONTEXT_INPUT_ORIGIN_INVALID")
    route = value["route"]
    for key in ("platform", "chat_type"):
        _identity(route[key], "CONTEXT_INPUT_ORIGIN_INVALID")
    _transport_identity(route["chat_id"])
    if type(route["is_bot"]) is not bool:
        raise ContextContinuationError("CONTEXT_INPUT_ORIGIN_INVALID")
    for key in _GATEWAY_ROUTE_FIELDS - {"platform", "chat_id", "chat_type", "is_bot"}:
        if route[key] is not None:
            _transport_identity(route[key])
    fingerprint = value["transport_fingerprint"]
    if fingerprint is not None and (type(fingerprint) is not str or re.fullmatch(r"[0-9a-f]{16}", fingerprint) is None):
        raise ContextContinuationError("CONTEXT_INPUT_ORIGIN_INVALID")
    if value["cold_recoverable"] and (fingerprint is None or not route["user_id"]):
        raise ContextContinuationError("CONTEXT_INPUT_ORIGIN_INVALID")
    return value


@dataclass(frozen=True)
class ContextInputReceipt:
    schema: str
    conversation_root: str
    profile_name: str
    sequence: int
    source: str
    event_id: str
    payload_digest: str
    content: object
    timestamp: float
    supplied_timestamp: float | None
    display_metadata: dict | None

    @classmethod
    def from_raw(cls, raw):
        value = _strict_json(raw)
        if (set(value) != set(cls.__dataclass_fields__)
                or value["schema"] != "SessionDBContextInputV1"
                or type(value["sequence"]) is not int or value["sequence"] < 1
                or type(value["timestamp"]) not in (int, float) or not math.isfinite(value["timestamp"])
                or type(value["payload_digest"]) is not str or re.fullmatch(r"[0-9a-f]{64}", value["payload_digest"]) is None
                or value["supplied_timestamp"] is not None and (
                    type(value["supplied_timestamp"]) not in (int, float)
                    or not math.isfinite(value["supplied_timestamp"])
                    or value["supplied_timestamp"] != value["timestamp"])
                or value["display_metadata"] is not None and type(value["display_metadata"]) is not dict):
            raise ContextContinuationError("CONTEXT_INPUT_RECORD_INVALID")
        for key in ("conversation_root", "profile_name", "source", "event_id"):
            _identity(value[key], "CONTEXT_INPUT_RECORD_INVALID")
        if value["payload_digest"] != _hash({"content": value["content"],
                "timestamp": value["supplied_timestamp"], "display_metadata": value["display_metadata"]}):
            raise ContextContinuationError("CONTEXT_INPUT_RECORD_INVALID")
        return cls(**value)


from contextlib import contextmanager
from contextvars import ContextVar

_compaction_input_lease = ContextVar("compaction_input_lease", default=None)


@contextmanager
def context_input_turn_lease_scope(db, holder):
    """Transport the already-owned turn credential to nested compaction calls."""
    token = _compaction_input_lease.set((db, holder))
    try:
        yield
    finally:
        _compaction_input_lease.reset(token)


class SessionContextInboxMixin(SessionContextInputTurnsMixin):
    def _normalize_compacted_context_messages_on_conn(self, conn, source_session, messages):
        """Keep accepted input clean and preserve its exact API-only sidecar."""
        projected = []
        for msg in messages:
            binding = msg.get("_context_input")
            if binding is None:
                projected.append(msg)
                continue
            if type(binding) is not dict or set(binding) != {"conversation_root", "sequence", "payload_digest"}:
                raise ContextContinuationError("CONTEXT_INPUT_BINDING_INVALID")
            root, profile = self._context_input_scope_on_conn(conn, source_session)
            if binding["conversation_root"] != root:
                raise ContextContinuationError("CONTEXT_INPUT_SCOPE_MISMATCH")
            receipt = self._read_context_input_on_conn(conn, root, binding["sequence"])
            if receipt.profile_name != profile or receipt.payload_digest != binding["payload_digest"]:
                raise ContextContinuationError("CONTEXT_INPUT_PAYLOAD_MISMATCH")
            api_content = msg.get("api_content") or msg.get("content")
            if (type(receipt.content) is not str or type(api_content) is not str
                    or receipt.content not in api_content):
                raise ContextContinuationError("CONTEXT_INPUT_PAYLOAD_MISMATCH")
            item = dict(msg, content=receipt.content)
            if api_content != receipt.content:
                item["api_content"] = api_content
            projected.append(item)
        return projected

    def _bind_compacted_context_inputs_on_conn(self, conn, source_session, destination_session, messages, row_ids):
        """First durable input projection shares the compaction transaction.

        Existing projections supply source coordinates, never another inbox
        consumption. Only a live credential for this exact native DB can bind
        previously unprojected input. The ContextVar supplies no new authority.
        """
        sources = {}
        for index, (msg, row_id) in enumerate(zip(messages, row_ids)):
            receipt, existing = self._prepare_context_input_projection_on_conn(
                conn, source_session, msg, allow_existing_alias=True)
            if receipt is None:
                continue
            if existing is not None:
                sources[index] = existing["row_id"]
                continue
            bound = _compaction_input_lease.get()
            holder = bound[1] if bound is not None and bound[0] is self else None
            self._assert_context_rebase_lease_on_conn(conn, source_session, holder)
            coordinate = msg.get("_row_id")
            if coordinate is not None and not (type(coordinate) is int and coordinate <= 0):
                raise ContextContinuationError("CONTEXT_INPUT_FIRST_PROJECTION_CONFLICT")
            self._commit_context_input_projection_on_conn(conn, receipt, destination_session, row_id)
        return sources

    def _assert_context_input_target_on_conn(self, conn, session_id, *, allow_alias=False):
        tip = self._context_continuation_tip_on_conn(conn, session_id)
        row = conn.execute("SELECT ended_at FROM sessions WHERE id=?", (tip,)).fetchone()
        if row is None or row[0] is not None or not allow_alias and tip != session_id:
            raise ContextContinuationError("CONTEXT_INPUT_TARGET_NOT_LIVE")
        return tip

    def _context_input_scope_on_conn(self, conn, session_id):
        _identity(session_id, "INVALID_INPUT_SESSION")
        row = conn.execute("SELECT profile_name FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise ContextContinuationError("CONTEXT_INPUT_SESSION_MISSING")
        # Native SessionDB represents the default profile as NULL.
        profile = row[0] if row[0] is not None else "default"
        _identity(profile, "CONTEXT_INPUT_PROFILE_REQUIRED")
        root = self._session_turn_lease_key_on_conn(conn, session_id)
        lineage = self._context_rebase_lineage_on_conn(conn, session_id)
        for sid in lineage:
            parent = conn.execute("SELECT profile_name FROM sessions WHERE id=?", (sid,)).fetchone()
            if parent is None or (parent[0] if parent[0] is not None else "default") != profile:
                raise ContextContinuationError("CONTEXT_INPUT_PROFILE_MISMATCH")
        return root, profile

    def _context_input_head_on_conn(self, conn, root, profile):
        row = conn.execute("SELECT value FROM state_meta WHERE key=?", (_prefix(root) + ":head",)).fetchone()
        if row is None:
            return {"schema": "SessionDBContextInboxV1", "conversation_root": root,
                    "profile_name": profile, "accepted_sequence": 0, "projected_sequence": 0}
        head = _strict_json(row[0])
        if (set(head) != {"schema", "conversation_root", "profile_name", "accepted_sequence", "projected_sequence"}
                or head["schema"] != "SessionDBContextInboxV1" or head["conversation_root"] != root
                or head["profile_name"] != profile
                or any(type(head[name]) is not int for name in ("accepted_sequence", "projected_sequence"))
                or not 0 <= head["projected_sequence"] <= head["accepted_sequence"] < 2**63):
            raise ContextContinuationError("CONTEXT_INPUT_HEAD_INVALID")
        return head

    def _read_context_input_on_conn(self, conn, root, sequence):
        if type(sequence) is not int or sequence < 1:
            raise ContextContinuationError("CONTEXT_INPUT_SEQUENCE_INVALID")
        row = conn.execute("SELECT value FROM state_meta WHERE key=?", (_event_key(root, sequence),)).fetchone()
        if row is None:
            raise ContextContinuationError("CONTEXT_INPUT_MISSING")
        receipt = ContextInputReceipt.from_raw(row[0])
        if receipt.conversation_root != root or receipt.sequence != sequence:
            raise ContextContinuationError("CONTEXT_INPUT_RECORD_INVALID")
        return receipt

    def accept_context_input(self, session_id, *, source, event_id, content,
                              timestamp=None, display_metadata=None, gateway_origin=None):
        """Persist one authentic event before acknowledging its acceptance.

        The transport supplies a stable event identity. Same-ID changed payloads
        refuse; an identical delivery returns its original sequence/time.
        Synthetic turns use their own existing owners and cannot enter here.
        """
        _identity(source, "CONTEXT_INPUT_SOURCE_INVALID")
        _identity(event_id, "CONTEXT_INPUT_ID_INVALID")
        if timestamp is not None and (type(timestamp) not in (int, float) or not math.isfinite(timestamp)):
            raise ContextContinuationError("CONTEXT_INPUT_TIMESTAMP_INVALID")
        if display_metadata is not None and type(display_metadata) is not dict:
            raise ContextContinuationError("CONTEXT_INPUT_METADATA_INVALID")
        message = {"role": "user", "content": content, "timestamp": timestamp,
                   "display_metadata": display_metadata}
        if user_originated_turn_view(message) is None:
            raise ContextContinuationError("CONTEXT_INPUT_AUTHENTIC_REQUIRED")
        payload = {"content": content, "timestamp": timestamp, "display_metadata": display_metadata}
        raw_payload = _canonical(payload)
        if len(raw_payload.encode()) > 900_000:
            raise ContextContinuationError("CONTEXT_INPUT_TOO_LARGE")
        # Freeze the caller's mutable input before entering native admission.
        payload = _strict_json(raw_payload)
        digest = _hash(payload)
        origin = None if gateway_origin is None else _gateway_origin(_strict_json(_canonical(gateway_origin)))
        if origin is not None and origin["route"]["platform"] != source:
            raise ContextContinuationError("CONTEXT_INPUT_ORIGIN_INVALID")

        def write(conn):
            root, profile = self._context_input_scope_on_conn(conn, session_id)
            tip = self._assert_context_input_target_on_conn(conn, session_id, allow_alias=True)
            if self._context_input_scope_on_conn(conn, tip) != (root, profile):
                raise ContextContinuationError("CONTEXT_INPUT_SCOPE_MISMATCH")
            head = self._context_input_head_on_conn(conn, root, profile)
            dedup = _prefix(root) + ":dedup:" + _hash([source, event_id])
            existing = conn.execute("SELECT value FROM state_meta WHERE key=?", (dedup,)).fetchone()
            if existing is not None:
                pointer = _strict_json(existing[0])
                if set(pointer) != {"sequence", "payload_digest"}:
                    raise ContextContinuationError("CONTEXT_INPUT_RECORD_INVALID")
                if pointer["payload_digest"] != digest:
                    raise ContextContinuationError("CONTEXT_INPUT_ID_COLLISION")
                receipt = self._read_context_input_on_conn(conn, root, pointer["sequence"])
                if (receipt.profile_name != profile or receipt.source != source or receipt.event_id != event_id
                        or receipt.payload_digest != digest or receipt.sequence > head["accepted_sequence"]):
                    raise ContextContinuationError("CONTEXT_INPUT_RECORD_INVALID")
                if self._context_input_origin_on_conn(conn, receipt) != origin:
                    # Neither missing evidence nor a new transport can be
                    # backfilled onto an already accepted occurrence.
                    raise ContextContinuationError("CONTEXT_INPUT_ORIGIN_COLLISION")
                return receipt
            if head["accepted_sequence"] - head["projected_sequence"] >= 128:
                raise ContextContinuationError("CONTEXT_INPUT_PENDING_LIMIT")
            sequence = head["accepted_sequence"] + 1
            receipt = ContextInputReceipt("SessionDBContextInputV1", root, profile, sequence, source,
                event_id, digest, payload["content"], time.time() if timestamp is None else timestamp,
                timestamp, payload["display_metadata"])
            raw = _canonical(asdict(receipt))
            ContextInputReceipt.from_raw(raw)
            conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)", (_event_key(root, sequence), raw))
            if origin is not None:
                bound_origin = {"schema": "SessionDBContextInputOriginV1", "conversation_root": root,
                    "profile_name": profile, "sequence": sequence, "payload_digest": digest, "origin": origin}
                conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)",
                    (_event_key(root, sequence) + ":origin", _canonical(bound_origin)))
            conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)",
                (dedup, _canonical({"sequence": sequence, "payload_digest": digest})))
            head["accepted_sequence"] = sequence
            conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (_prefix(root) + ":head", _canonical(head)))
            return receipt
        return self._execute_write(write)

    def _context_input_origin_on_conn(self, conn, receipt):
        row = conn.execute("SELECT value FROM state_meta WHERE key=?",
            (_event_key(receipt.conversation_root, receipt.sequence) + ":origin",)).fetchone()
        if row is None:
            return None
        value = _strict_json(row[0])
        if (set(value) != {"schema", "conversation_root", "profile_name", "sequence", "payload_digest", "origin"}
                or value["schema"] != "SessionDBContextInputOriginV1"
                or value["conversation_root"] != receipt.conversation_root or value["profile_name"] != receipt.profile_name
                or type(value["sequence"]) is not int or value["sequence"] != receipt.sequence
                or value["payload_digest"] != receipt.payload_digest):
            raise ContextContinuationError("CONTEXT_INPUT_ORIGIN_INVALID")
        origin = _gateway_origin(value["origin"])
        if origin["route"]["platform"] != receipt.source:
            raise ContextContinuationError("CONTEXT_INPUT_ORIGIN_INVALID")
        return origin

    def read_context_input_origin(self, session_id, receipt):
        """Read immutable actor/transport evidence; never restore auth flags."""
        if type(receipt) is not ContextInputReceipt:
            raise ContextContinuationError("CONTEXT_INPUT_RECORD_INVALID")
        with self._read_ctx() as conn:
            conn.execute("SAVEPOINT context_input_origin")
            try:
                if self._context_input_scope_on_conn(conn, session_id) != (receipt.conversation_root, receipt.profile_name):
                    raise ContextContinuationError("CONTEXT_INPUT_SCOPE_MISMATCH")
                if self._read_context_input_on_conn(conn, receipt.conversation_root, receipt.sequence) != receipt:
                    raise ContextContinuationError("CONTEXT_INPUT_RECORD_INVALID")
                return self._context_input_origin_on_conn(conn, receipt)
            finally:
                conn.execute("ROLLBACK TO context_input_origin")
                conn.execute("RELEASE context_input_origin")

    def read_context_input(self, session_id, *, source, event_id):
        """Read the accepted receipt, never turn/effect authority."""
        _identity(source, "CONTEXT_INPUT_SOURCE_INVALID")
        _identity(event_id, "CONTEXT_INPUT_ID_INVALID")
        with self._read_ctx() as conn:
            conn.execute("SAVEPOINT context_input_read")
            try:
                root, profile = self._context_input_scope_on_conn(conn, session_id)
                row = conn.execute("SELECT value FROM state_meta WHERE key=?",
                    (_prefix(root) + ":dedup:" + _hash([source, event_id]),)).fetchone()
                if row is None:
                    return None
                pointer = _strict_json(row[0])
                if set(pointer) != {"sequence", "payload_digest"}:
                    raise ContextContinuationError("CONTEXT_INPUT_RECORD_INVALID")
                receipt = self._read_context_input_on_conn(conn, root, pointer["sequence"])
                head = self._context_input_head_on_conn(conn, root, profile)
                if (receipt.profile_name != profile or receipt.source != source or receipt.event_id != event_id
                        or receipt.payload_digest != pointer["payload_digest"]
                        or receipt.sequence > head["accepted_sequence"]):
                    raise ContextContinuationError("CONTEXT_INPUT_RECORD_INVALID")
                return receipt
            finally:
                conn.execute("ROLLBACK TO context_input_read")
                conn.execute("RELEASE context_input_read")

    def read_pending_context_inputs(self, session_id):
        """Discover accepted, unprojected inputs from one native snapshot.

        This is a scheduler observation, never a lease or execution grant.
        Projected input may still require task/outcome reconciliation.
        """
        with self._read_ctx() as conn:
            conn.execute("SAVEPOINT context_input_pending")
            try:
                root, profile = self._context_input_scope_on_conn(conn, session_id)
                head = self._context_input_head_on_conn(conn, root, profile)
                if head["accepted_sequence"] - head["projected_sequence"] > 128:
                    raise ContextContinuationError("CONTEXT_INPUT_PENDING_LIMIT")
                receipts = []
                for sequence in range(head["projected_sequence"] + 1, head["accepted_sequence"] + 1):
                    receipt = self._read_context_input_on_conn(conn, root, sequence)
                    if (receipt.profile_name != profile
                            or self._context_input_projection_on_conn(conn, receipt) is not None):
                        raise ContextContinuationError("CONTEXT_INPUT_RECORD_INVALID")
                    receipts.append(receipt)
                return tuple(receipts)
            finally:
                conn.execute("ROLLBACK TO context_input_pending")
                conn.execute("RELEASE context_input_pending")

    def _context_input_projection_on_conn(self, conn, receipt):
        row = conn.execute("SELECT value FROM state_meta WHERE key=?",
                           (_event_key(receipt.conversation_root, receipt.sequence) + ":projection",)).fetchone()
        if row is None:
            return None
        projection = _strict_json(row[0])
        if (set(projection) != {"schema", "conversation_root", "sequence", "payload_digest", "session_id", "row_id"}
                or projection["schema"] != "SessionDBContextInputProjectionV1"
                or projection["conversation_root"] != receipt.conversation_root
                or type(projection["sequence"]) is not int
                or projection["sequence"] != receipt.sequence
                or projection["payload_digest"] != receipt.payload_digest
                or type(projection["row_id"]) is not int or projection["row_id"] < 1):
            raise ContextContinuationError("CONTEXT_INPUT_PROJECTION_INVALID")
        _identity(projection["session_id"], "CONTEXT_INPUT_PROJECTION_INVALID")
        root, profile = self._context_input_scope_on_conn(conn, projection["session_id"])
        row = conn.execute("SELECT * FROM messages WHERE id=? AND session_id=?",
                           (projection["row_id"], projection["session_id"])).fetchone()
        if (root != receipt.conversation_root or profile != receipt.profile_name or row is None
                or row["role"] != "user" or self._decode_content(row["content"]) != receipt.content
                or row["timestamp"] != receipt.timestamp or row["display_kind"] or row["_compressed_summary"]
                or not row["active"] and not row["compacted"]
                or (self._decode_display_metadata(row["display_metadata"]) or None) != (receipt.display_metadata or None)):
            raise ContextContinuationError("CONTEXT_INPUT_PROJECTION_CHANGED")
        return projection

    def _prepare_context_input_projection_on_conn(self, conn, session_id, message, *, allow_existing_alias=False):
        binding = message.get("_context_input")
        if binding is None:
            return None, None
        if type(binding) is not dict or set(binding) != {"conversation_root", "sequence", "payload_digest"}:
            raise ContextContinuationError("CONTEXT_INPUT_BINDING_INVALID")
        if type(binding["sequence"]) is not int:
            raise ContextContinuationError("CONTEXT_INPUT_BINDING_INVALID")
        root, profile = self._context_input_scope_on_conn(conn, session_id)
        self._assert_context_input_target_on_conn(conn, session_id)
        if binding["conversation_root"] != root:
            raise ContextContinuationError("CONTEXT_INPUT_SCOPE_MISMATCH")
        receipt = self._read_context_input_on_conn(conn, root, binding["sequence"])
        if (receipt.profile_name != profile or receipt.payload_digest != binding["payload_digest"]
                or message.get("role") != "user" or message.get("content") != receipt.content
                or message.get("timestamp") != receipt.timestamp or message.get("display_kind")
                or message.get("_compressed_summary")
                or (message.get("display_metadata") or None) != (receipt.display_metadata or None)):
            raise ContextContinuationError("CONTEXT_INPUT_PAYLOAD_MISMATCH")
        head = self._context_input_head_on_conn(conn, root, profile)
        existing = self._context_input_projection_on_conn(conn, receipt)
        if existing is None and receipt.sequence != head["projected_sequence"] + 1:
            raise ContextContinuationError("CONTEXT_INPUT_ORDER_MISMATCH")
        if existing is not None and receipt.sequence > head["projected_sequence"]:
            raise ContextContinuationError("CONTEXT_INPUT_PROJECTION_INVALID")
        if existing is not None and existing["session_id"] != session_id and not allow_existing_alias:
            raise ContextContinuationError("CONTEXT_INPUT_ALREADY_PROJECTED")
        return receipt, existing

    def _commit_context_input_projection_on_conn(self, conn, receipt, session_id, row_id):
        projection = {"schema": "SessionDBContextInputProjectionV1", "conversation_root": receipt.conversation_root,
            "sequence": receipt.sequence, "payload_digest": receipt.payload_digest, "session_id": session_id, "row_id": row_id}
        conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)",
            (_event_key(receipt.conversation_root, receipt.sequence) + ":projection", _canonical(projection)))
        head = self._context_input_head_on_conn(conn, receipt.conversation_root, receipt.profile_name)
        if head["projected_sequence"] + 1 != receipt.sequence:
            raise ContextContinuationError("CONTEXT_INPUT_ORDER_MISMATCH")
        head["projected_sequence"] = receipt.sequence
        conn.execute("UPDATE state_meta SET value=? WHERE key=?",
                     (_canonical(head), _prefix(receipt.conversation_root) + ":head"))

    def project_context_inputs_before(self, session_id, *, receipt, turn_lease_holder):
        """Project older queued human input under the current conversation lease.

        The current input keeps the ordinary prologue's API-sidecar persistence
        path. No accepted input is marked answered by projecting its bytes.
        """
        if type(receipt) is not ContextInputReceipt:
            raise ContextContinuationError("CONTEXT_INPUT_RECORD_INVALID")
        def write(conn):
            self._assert_context_rebase_lease_on_conn(conn, session_id, turn_lease_holder)
            self._assert_context_input_target_on_conn(conn, session_id)
            root, profile = self._context_input_scope_on_conn(conn, session_id)
            if root != receipt.conversation_root or profile != receipt.profile_name:
                raise ContextContinuationError("CONTEXT_INPUT_SCOPE_MISMATCH")
            if self._read_context_input_on_conn(conn, root, receipt.sequence) != receipt:
                raise ContextContinuationError("CONTEXT_INPUT_RECORD_INVALID")
            head = self._context_input_head_on_conn(conn, root, profile)
            if receipt.sequence > head["accepted_sequence"]:
                raise ContextContinuationError("CONTEXT_INPUT_SEQUENCE_INVALID")
            rows = []
            for sequence in range(head["projected_sequence"] + 1, receipt.sequence):
                prior = self._read_context_input_on_conn(conn, root, sequence)
                rows.append({"role": "user", "content": prior.content, "timestamp": prior.timestamp,
                    "display_metadata": prior.display_metadata, "_context_input": {
                        "conversation_root": root, "sequence": sequence, "payload_digest": prior.payload_digest}})
            if len(rows) > 128:
                raise ContextContinuationError("CONTEXT_INPUT_PENDING_LIMIT")
            inserted, _, _ = self._insert_message_rows(conn, session_id, rows, bind_context_inputs=True)
            if inserted:
                conn.execute("UPDATE sessions SET message_count=message_count+? WHERE id=?", (inserted, session_id))
            return {"inserted": inserted, "projection": self._context_input_projection_on_conn(conn, receipt)}
        return self._execute_write(write)

    def _context_input_control_on_conn(self, conn, session_id):
        root = self._session_turn_lease_key_on_conn(conn, session_id)
        if conn.execute("SELECT 1 FROM state_meta WHERE key=?", (_prefix(root) + ":head",)).fetchone() is None:
            return None
        root, profile = self._context_input_scope_on_conn(conn, session_id)
        return _canonical(self._context_input_head_on_conn(conn, root, profile))
