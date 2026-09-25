"""Input execution lifetime on SessionDB, separate from transcript projection.

The receipt inbox remains the only queue. These records bind a bounded turn
to its inputs and existing dispatch attempts. An observed response is not a
completed task, and an admitted request without a final outcome never replays.
"""
from __future__ import annotations

import hashlib
import math
import os
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from hermes_state_continuity import ContextContinuationError, _canonical, _strict_json


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _key(root):
    return "context-input-turn:" + hashlib.sha256(root.encode()).hexdigest()


_FIELDS = frozenset({"schema", "conversation_root", "profile_name", "phase_id", "first_sequence",
    "last_sequence", "receipts_digest", "started_at", "deadline_at", "attempts", "holder_digest",
    "controller_pid", "process_identity", "state", "dispatch_attempts", "final_row", "control_raw",
    "start_watermark"})

_final_input_response = ContextVar("final_input_response", default=None)


@contextmanager
def input_response_publication_scope(db, holder, phase_id, attempt_id, content):
    token = _final_input_response.set((db, holder, phase_id, attempt_id, content))
    try:
        yield
    finally:
        _final_input_response.reset(token)


class SessionContextInputTurnsMixin:
    def _complete_input_response_batch_on_conn(self, conn, session_id, holder, messages, row_ids):
        bound = _final_input_response.get()
        if bound is None:
            return
        db, expected_holder, phase_id, attempt_id, content = bound
        if db is not self or expected_holder != holder:
            raise ContextContinuationError("CONTEXT_INPUT_TURN_OWNER_CHANGED")
        root, profile = self._context_input_scope_on_conn(conn, session_id)
        phase = self._input_turn_on_conn(conn, root, profile)
        if (phase is None or phase["phase_id"] != phase_id or not messages or not row_ids
                or messages[-1].get("role") != "assistant" or messages[-1].get("content") != content):
            raise ContextContinuationError("CONTEXT_INPUT_FINAL_ROW_INVALID")
        self._complete_context_input_turn_on_conn(conn, session_id, turn_lease_holder=holder,
            final_row_id=row_ids[-1], final_content=content, final_attempt_id=attempt_id)

    def confirm_context_input_response(self, session_id, *, phase_id, final_attempt_id, final_content, turn_lease_holder):
        """Read-only closure of a committed final-publication acknowledgement."""
        with self._read_ctx() as conn:
            conn.execute("SAVEPOINT context_input_confirmation")
            try:
                root, profile = self._context_input_scope_on_conn(conn, session_id)
                value = self._input_turn_on_conn(conn, root, profile)
                self._assert_context_rebase_lease_on_conn(conn, session_id, turn_lease_holder)
                if (value is None or value["phase_id"] != phase_id or value["state"] != "answered"
                        or value["holder_digest"] != _hash(turn_lease_holder)
                        or value["dispatch_attempts"][-1] != final_attempt_id):
                    raise ContextContinuationError("CONTEXT_INPUT_COMPLETION_UNCONFIRMED")
                final = value["final_row"]
                row = conn.execute("SELECT * FROM messages WHERE id=? AND session_id=?", (final["row_id"], final["session_id"])).fetchone()
                if row is None or self._decode_content(row["content"]) != final_content or self._context_message_provenance_digest(row) != final["digest"]:
                    raise ContextContinuationError("CONTEXT_INPUT_COMPLETION_CHANGED")
                return value
            finally:
                conn.execute("ROLLBACK TO context_input_confirmation")
                conn.execute("RELEASE context_input_confirmation")
    def reserve_context_input_wake(self, session_id, *, receipt):
        """Charge controller construction, without granting execution custody.

        The existing gateway timer only discovers work. This write bounds its
        actual construction attempts even when construction fails before an
        AIAgent can acquire the conversation lease. New arrivals cannot renew
        an unfinished wake's deadline or attempts.
        """
        from hermes_state_inbox import ContextInputReceipt
        if type(receipt) is not ContextInputReceipt:
            raise ContextContinuationError("CONTEXT_INPUT_RECORD_INVALID")
        def write(conn):
            root, profile = self._context_input_scope_on_conn(conn, session_id)
            self._assert_context_input_target_on_conn(conn, session_id, allow_alias=True)
            if (root, profile) != (receipt.conversation_root, receipt.profile_name):
                raise ContextContinuationError("CONTEXT_INPUT_SCOPE_MISMATCH")
            if self._read_context_input_on_conn(conn, root, receipt.sequence) != receipt:
                raise ContextContinuationError("CONTEXT_INPUT_RECORD_INVALID")
            phase = self._input_turn_on_conn(conn, root, profile)
            if phase is not None and phase["state"] != "answered" and phase["dispatch_attempts"]:
                raise ContextContinuationError("CONTEXT_INPUT_EXECUTION_UNCERTAIN")
            key = _key(root) + ":wake"
            row = conn.execute("SELECT value FROM state_meta WHERE key=?", (key,)).fetchone()
            previous = None if row is None else _strict_json(row[0])
            if previous is not None and (set(previous) != {"schema", "through_sequence", "attempts", "started_at", "deadline_at"}
                    or previous["schema"] != "SessionDBContextInputWakeV1"
                    or type(previous["through_sequence"]) is not int or previous["through_sequence"] < 1
                    or type(previous["attempts"]) is not int or not 1 <= previous["attempts"] <= 3
                    or type(previous["started_at"]) not in (int, float)
                    or previous["deadline_at"] != previous["started_at"] + 900):
                raise ContextContinuationError("CONTEXT_INPUT_WAKE_INVALID")
            answered = phase is not None and phase["state"] == "answered"
            now = time.time()
            if previous is not None and not (answered and phase["last_sequence"] >= previous["through_sequence"]):
                if previous["attempts"] >= 3 or now >= previous["deadline_at"]:
                    raise ContextContinuationError("CONTEXT_INPUT_WAKE_EXHAUSTED")
                value = dict(previous, attempts=previous["attempts"] + 1)
            else:
                value = {"schema": "SessionDBContextInputWakeV1", "through_sequence": receipt.sequence,
                    "attempts": 1, "started_at": now, "deadline_at": now + 900}
            conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, _canonical(value)))
            return value
        return self._execute_write(write)

    def _input_turn_on_conn(self, conn, root, profile):
        row = conn.execute("SELECT value FROM state_meta WHERE key=?", (_key(root),)).fetchone()
        if row is None:
            return None
        value = _strict_json(row[0])
        if (set(value) != _FIELDS or value["schema"] != "SessionDBContextInputTurnV1"
                or value["conversation_root"] != root or value["profile_name"] != profile
                or any(type(value[k]) is not int for k in ("first_sequence", "last_sequence", "attempts", "controller_pid", "start_watermark"))
                or not 1 <= value["first_sequence"] <= value["last_sequence"]
                or value["last_sequence"] - value["first_sequence"] >= 128
                or not 1 <= value["attempts"] <= 3
                or type(value["started_at"]) not in (float, int)
                or not math.isfinite(value["started_at"])
                or value["deadline_at"] != value["started_at"] + 900
                or value["state"] not in {"active", "idle", "uncertain", "answered"}
                or type(value["dispatch_attempts"]) is not list or len(value["dispatch_attempts"]) > 128
                or any(type(item) is not str for item in value["dispatch_attempts"])
                or len(set(value["dispatch_attempts"])) != len(value["dispatch_attempts"])):
            raise ContextContinuationError("CONTEXT_INPUT_TURN_INVALID")
        if (any(type(value[k]) is not str or not value[k] for k in ("phase_id", "receipts_digest", "holder_digest", "process_identity"))
                or value["controller_pid"] < 1 or value["start_watermark"] < 0
                or value["control_raw"] is not None and type(value["control_raw"]) is not str):
            raise ContextContinuationError("CONTEXT_INPUT_TURN_INVALID")
        receipts = self._input_turn_receipts_on_conn(conn, value)
        if _hash([[r.sequence, r.payload_digest] for r in receipts]) != value["receipts_digest"]:
            raise ContextContinuationError("CONTEXT_INPUT_TURN_INVALID")
        if value["state"] == "answered":
            final = value["final_row"]
            proof = conn.execute("SELECT value FROM state_meta WHERE key=?",
                (f'{_key(root)}:phase:{value["phase_id"]}:response',)).fetchone()
            if (type(final) is not dict or set(final) != {"session_id", "row_id", "digest"}
                    or type(final["row_id"]) is not int or final["row_id"] < 1 or not value["dispatch_attempts"]
                    or proof is None or _strict_json(proof[0]) != value):
                raise ContextContinuationError("CONTEXT_INPUT_TURN_INVALID")
        elif value["final_row"] is not None:
            raise ContextContinuationError("CONTEXT_INPUT_TURN_INVALID")
        return value

    def _input_turn_receipts_on_conn(self, conn, value):
        receipts = tuple(self._read_context_input_on_conn(conn, value["conversation_root"], sequence)
            for sequence in range(value["first_sequence"], value["last_sequence"] + 1))
        if any(r.profile_name != value["profile_name"] for r in receipts):
            raise ContextContinuationError("CONTEXT_INPUT_SCOPE_MISMATCH")
        return receipts

    def _write_input_turn_on_conn(self, conn, value):
        conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_key(value["conversation_root"]), _canonical(value)))

    def _assert_input_turn_owner_on_conn(self, conn, session_id, holder, value):
        from hermes_state_runs import _controller
        self._assert_context_rebase_lease_on_conn(conn, session_id, holder)
        if (value is None or value["state"] != "active" or value["holder_digest"] != _hash(holder)
                or value["controller_pid"] != os.getpid() or value["process_identity"] != _controller(os.getpid())):
            raise ContextContinuationError("CONTEXT_INPUT_TURN_OWNER_CHANGED")
        # This deadline bounds controller construction/recovery. Once the
        # controller is executing, native request/parent budgets govern it;
        # productive work does not become an expired recovery attempt.
        if not value["dispatch_attempts"] and time.time() >= value["deadline_at"]:
            raise ContextContinuationError("CONTEXT_INPUT_RECOVERY_EXHAUSTED")

    def read_context_input_work(self, session_id):
        """Read one bounded unfinished phase or the pending native inbox."""
        with self._read_ctx() as conn:
            conn.execute("SAVEPOINT context_input_work")
            try:
                root, profile = self._context_input_scope_on_conn(conn, session_id)
                phase = self._input_turn_on_conn(conn, root, profile)
                if phase is not None and phase["state"] != "answered":
                    bound = phase
                    if not phase["dispatch_attempts"]:
                        head = self._context_input_head_on_conn(conn, root, profile)
                        if head["accepted_sequence"] - phase["first_sequence"] >= 128:
                            raise ContextContinuationError("CONTEXT_INPUT_PENDING_LIMIT")
                        bound = dict(phase, last_sequence=head["accepted_sequence"])
                    receipts = self._input_turn_receipts_on_conn(conn, bound)
                else:
                    head = self._context_input_head_on_conn(conn, root, profile)
                    if head["accepted_sequence"] - head["projected_sequence"] > 128:
                        raise ContextContinuationError("CONTEXT_INPUT_PENDING_LIMIT")
                    receipts = tuple(self._read_context_input_on_conn(conn, root, n)
                        for n in range(head["projected_sequence"] + 1, head["accepted_sequence"] + 1))
                return {"phase": phase, "receipts": receipts}
            finally:
                conn.execute("ROLLBACK TO context_input_work")
                conn.execute("RELEASE context_input_work")

    def begin_context_input_turn(self, session_id, *, receipt, turn_lease_holder):
        """Reserve under the actual turn lease; recovery never resets budgets."""
        from hermes_state_inbox import ContextInputReceipt
        from hermes_state_runs import _controller, _process_identity
        if type(receipt) is not ContextInputReceipt:
            raise ContextContinuationError("CONTEXT_INPUT_RECORD_INVALID")

        def write(conn):
            self._assert_context_rebase_lease_on_conn(conn, session_id, turn_lease_holder)
            self._assert_context_input_target_on_conn(conn, session_id)
            root, profile = self._context_input_scope_on_conn(conn, session_id)
            if (root, profile) != (receipt.conversation_root, receipt.profile_name):
                raise ContextContinuationError("CONTEXT_INPUT_SCOPE_MISMATCH")
            if self._read_context_input_on_conn(conn, root, receipt.sequence) != receipt:
                raise ContextContinuationError("CONTEXT_INPUT_RECORD_INVALID")
            head = self._context_input_head_on_conn(conn, root, profile)
            previous = self._input_turn_on_conn(conn, root, profile)
            now = time.time()
            if previous is not None and previous["state"] != "answered":
                if previous["dispatch_attempts"]:
                    raise ContextContinuationError("CONTEXT_INPUT_EXECUTION_UNCERTAIN")
                if previous["last_sequence"] > receipt.sequence:
                    raise ContextContinuationError("CONTEXT_INPUT_TURN_PENDING")
                if previous["state"] == "active":
                    if _process_identity(previous["controller_pid"]) == previous["process_identity"]:
                        raise ContextContinuationError("CONTEXT_INPUT_CONTROLLER_STILL_ACTIVE")
                if previous["attempts"] >= 3 or now >= previous["deadline_at"]:
                    raise ContextContinuationError("CONTEXT_INPUT_RECOVERY_EXHAUSTED")
                value = dict(previous, attempts=previous["attempts"] + 1)
                if receipt.sequence > previous["last_sequence"]:
                    if receipt.sequence > head["accepted_sequence"] or receipt.sequence - value["first_sequence"] >= 128:
                        raise ContextContinuationError("CONTEXT_INPUT_SEQUENCE_INVALID")
                    value["last_sequence"] = receipt.sequence
                    value["receipts_digest"] = _hash([[r.sequence, r.payload_digest] for r in self._input_turn_receipts_on_conn(conn, value)])
            else:
                if receipt.sequence <= head["projected_sequence"]:
                    raise ContextContinuationError("CONTEXT_INPUT_ALREADY_PROJECTED")
                value = {"schema": "SessionDBContextInputTurnV1", "conversation_root": root, "profile_name": profile,
                    "phase_id": uuid.uuid4().hex, "first_sequence": head["projected_sequence"] + 1,
                    "last_sequence": receipt.sequence, "receipts_digest": "", "started_at": now, "deadline_at": now + 900,
                    "attempts": 1, "dispatch_attempts": [], "final_row": None,
                    "start_watermark": conn.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]}
                if receipt.sequence > head["accepted_sequence"] or value["last_sequence"] - value["first_sequence"] >= 128:
                    raise ContextContinuationError("CONTEXT_INPUT_SEQUENCE_INVALID")
                value["receipts_digest"] = _hash([[r.sequence, r.payload_digest] for r in self._input_turn_receipts_on_conn(conn, value)])
            control = conn.execute("SELECT value FROM state_meta WHERE key=?", ("context-control:" + root,)).fetchone()
            control_raw = None if control is None else control[0]
            if previous is not None and previous["state"] != "answered" and previous["control_raw"] != control_raw:
                raise ContextContinuationError("CONTEXT_INPUT_CONTROL_CHANGED")
            value.update(holder_digest=_hash(turn_lease_holder), controller_pid=os.getpid(),
                process_identity=_controller(os.getpid()), state="active", control_raw=control_raw)
            self._write_input_turn_on_conn(conn, value)
            conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)",
                (f'{_key(root)}:phase:{value["phase_id"]}:attempt:{value["attempts"]}', _canonical(value)))
            return value
        return self._execute_write(write)

    def _bind_input_turn_dispatch_on_conn(self, conn, session_id, holder, attempt_id):
        root, profile = self._context_input_scope_on_conn(conn, session_id)
        value = self._input_turn_on_conn(conn, root, profile)
        if value is None or value["state"] == "answered":
            return
        self._assert_input_turn_owner_on_conn(conn, session_id, holder, value)
        if len(value["dispatch_attempts"]) >= 128:
            raise ContextContinuationError("CONTEXT_INPUT_DISPATCH_LIMIT")
        value["dispatch_attempts"].append(attempt_id)
        self._write_input_turn_on_conn(conn, value)
        conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)", ("context-input-dispatch:" + attempt_id,
            _canonical({"schema": "SessionDBContextInputDispatchV1", "phase_id": value["phase_id"],
                "conversation_root": root, "receipts_digest": value["receipts_digest"], "attempt": value["attempts"]})))

    def release_context_input_turn(self, session_id, *, turn_lease_holder):
        """Orderly release is explicit; expiry alone never means process death."""
        def write(conn):
            root, profile = self._context_input_scope_on_conn(conn, session_id)
            value = self._input_turn_on_conn(conn, root, profile)
            if value is None or value["state"] != "active":
                return
            self._assert_context_rebase_lease_on_conn(conn, session_id, turn_lease_holder)
            if value["holder_digest"] != _hash(turn_lease_holder):
                raise ContextContinuationError("CONTEXT_INPUT_TURN_OWNER_CHANGED")
            value["state"] = "uncertain" if value["dispatch_attempts"] else "idle"
            self._write_input_turn_on_conn(conn, value)
        self._execute_write(write)

    def complete_context_input_turn(self, session_id, *, turn_lease_holder, final_row_id, final_content, final_attempt_id):
        """Bind a successfully finalized response, not completion of its task."""
        return self._execute_write(lambda conn: self._complete_context_input_turn_on_conn(conn, session_id,
            turn_lease_holder=turn_lease_holder, final_row_id=final_row_id,
            final_content=final_content, final_attempt_id=final_attempt_id))

    def _complete_context_input_turn_on_conn(self, conn, session_id, *, turn_lease_holder, final_row_id, final_content, final_attempt_id):
        root, profile = self._context_input_scope_on_conn(conn, session_id)
        value = self._input_turn_on_conn(conn, root, profile)
        if value is not None and value["state"] == "answered":
            self._assert_context_rebase_lease_on_conn(conn, session_id, turn_lease_holder)
            row = conn.execute("SELECT * FROM messages WHERE id=? AND session_id=?", (final_row_id, session_id)).fetchone()
            if (value["holder_digest"] != _hash(turn_lease_holder) or row is None
                    or value["dispatch_attempts"][-1] != final_attempt_id
                    or self._decode_content(row["content"]) != final_content
                    or value["final_row"] != {"session_id": session_id, "row_id": final_row_id,
                        "digest": self._context_message_provenance_digest(row)}):
                raise ContextContinuationError("CONTEXT_INPUT_COMPLETION_CHANGED")
            return value
        self._assert_input_turn_owner_on_conn(conn, session_id, turn_lease_holder, value)
        snapshot = self._read_context_rebase_snapshot_on_conn(conn, session_id)
        if snapshot.dispatch_stopped or snapshot.has_pending_inputs or snapshot.has_unresolved_effects:
            raise ContextContinuationError("CONTEXT_INPUT_COMPLETION_SUPERSEDED")
        control = conn.execute("SELECT value FROM state_meta WHERE key=?", ("context-control:" + root,)).fetchone()
        if (None if control is None else control[0]) != value["control_raw"]:
            raise ContextContinuationError("CONTEXT_INPUT_CONTROL_CHANGED")
        if not value["dispatch_attempts"]:
            raise ContextContinuationError("CONTEXT_INPUT_RESPONSE_REQUIRED")
        if final_attempt_id != value["dispatch_attempts"][-1]:
            raise ContextContinuationError("CONTEXT_INPUT_FINAL_ATTEMPT_CHANGED")
        for attempt_id in value["dispatch_attempts"]:
            result = conn.execute("SELECT value FROM state_meta WHERE key=?", ("context-dispatch-result:" + attempt_id,)).fetchone()
            allowed = {"response_received"} if attempt_id == final_attempt_id else {"response_received", "response_discarded"}
            if result is None or _strict_json(result[0]).get("disposition") not in allowed:
                raise ContextContinuationError("CONTEXT_INPUT_EXECUTION_UNCERTAIN")
        admission_row = conn.execute("SELECT value FROM state_meta WHERE key=?", ("context-dispatch:" + final_attempt_id,)).fetchone()
        if admission_row is None:
            raise ContextContinuationError("CONTEXT_INPUT_FINAL_ATTEMPT_CHANGED")
        admission = _strict_json(admission_row[0])
        if (type(final_row_id) is not int or final_row_id <= max(value["start_watermark"], admission["input_watermark"])
                or admission["conversation_root"] != root or admission["session_id"] != session_id):
            raise ContextContinuationError("CONTEXT_INPUT_FINAL_ROW_INVALID")
        row = conn.execute("SELECT * FROM messages WHERE id=? AND session_id=?", (final_row_id, session_id)).fetchone()
        if (row is None or row["role"] != "assistant" or not row["active"] or row["_compressed_summary"]
                or row["tool_calls"] or self._decode_content(row["content"]) != final_content
                or conn.execute("SELECT MAX(id) FROM messages WHERE session_id=? AND active=1", (session_id,)).fetchone()[0] != final_row_id):
            raise ContextContinuationError("CONTEXT_INPUT_FINAL_ROW_INVALID")
        for receipt in self._input_turn_receipts_on_conn(conn, value):
            projection = self._context_input_projection_on_conn(conn, receipt)
            if projection is None or projection["row_id"] >= final_row_id:
                raise ContextContinuationError("CONTEXT_INPUT_FINAL_ROW_INVALID")
        value["state"] = "answered"
        value["final_row"] = {"session_id": session_id, "row_id": final_row_id,
            "digest": self._context_message_provenance_digest(row)}
        self._write_input_turn_on_conn(conn, value)
        conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)",
            (f'{_key(root)}:phase:{value["phase_id"]}:response', _canonical(value)))
        return value
