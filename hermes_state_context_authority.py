"""SessionDB's durable side of native retire/publish/activate.

The existing rebase recovery row owns the deadline and attempt budget. Native
RPC never runs under a SQLite transaction. Receipts are observations, not a
second permit ledger, and unresolved consumed effects prevent activation.
"""
from copy import deepcopy
import hashlib
import time

from ares_runtime.continuity.authority import closed, exact, head, signing_material, snapshot
from ares_runtime.continuity.credentials import controller_credential


class SessionContextAuthorityMixin:
    def _native_rebase_intent_on_conn(self, conn, session_id):
        from hermes_state_continuity import _strict_json, ContextContinuationError
        root = str(self._session_turn_lease_key_on_conn(conn, session_id))
        row = conn.execute("SELECT value FROM state_meta WHERE key=?", ("native-context-rebase:" + root,)).fetchone()
        if row is None:
            return None
        value = _strict_json(row[0])
        if value.get("schema") != "SessionDBNativeContextRebaseV1" or value.get("root") != root:
            raise ContextContinuationError("CONTEXT_NATIVE_REBASE_INVALID")
        return value

    def read_native_context_rebase(self, session_id):
        with self._read_ctx() as conn:
            return self._native_rebase_intent_on_conn(conn, session_id)

    def _assert_native_context_dispatch_on_conn(self, conn, session_id):
        from hermes_state_continuity import ContextContinuationError
        registration = self._native_context_authority_on_conn(conn, session_id)
        if registration is None:
            return
        intent = self._native_rebase_intent_on_conn(conn, session_id)
        if intent is not None and intent.get("state") != "activated":
            raise ContextContinuationError("CONTEXT_NATIVE_REBASE_PENDING")
        if (registration["authority"]["head"]["context"] != session_id
                or registration["authority"]["head"]["mode"] != "active"):
            raise ContextContinuationError("CONTEXT_AUTHORITY_GENERATION_CHANGED")

    def retire_native_context_for_rebase(self, *, transition_id, parent_session_id,
            child_session_id, continuation_digest, expected_snapshot_digest,
            snapshot_read_limits, turn_lease_holder):
        """Persist exact intent before native retirement or child publication."""
        from hermes_state_continuity import _canonical, _strict_json, ContextContinuationError
        registration = self.read_native_context_authority(parent_session_id)
        if registration is None:
            return None
        expected_intent = expected_recovery = None

        def reserve(conn):
            nonlocal expected_intent, expected_recovery
            self._assert_context_rebase_lease_on_conn(conn, parent_session_id, turn_lease_holder)
            current = self._native_context_authority_on_conn(conn, parent_session_id)
            prior = self._native_rebase_intent_on_conn(conn, parent_session_id)
            if prior is not None and prior["transition_id"] == transition_id:
                if (prior["parent_session_id"] != parent_session_id or prior["child_session_id"] != child_session_id
                        or prior["continuation_digest"] != continuation_digest
                        or prior["snapshot_digest"] != expected_snapshot_digest):
                    raise ContextContinuationError("CONTEXT_NATIVE_REBASE_COLLISION")
                if prior["retire_receipt"] is None:
                    row = conn.execute("SELECT value FROM state_meta WHERE key=?",
                                       ("context-rebase-recovery:" + transition_id,)).fetchone()
                    recovery = {} if row is None else _strict_json(row[0])
                    if recovery.get("attempts", 4) >= 3 or time.time() >= recovery.get("deadline_at", 0):
                        raise ContextContinuationError("CONTEXT_REBASE_RECOVERY_EXHAUSTED")
                    recovery["attempts"] += 1
                    recovery["holder_digest"] = hashlib.sha256(turn_lease_holder.encode()).hexdigest()
                    expected_recovery = recovery
                    expected_intent = prior
                    conn.execute("UPDATE state_meta SET value=? WHERE key=?",
                                 (_canonical(recovery), "context-rebase-recovery:" + transition_id))
                return prior
            self._assert_native_context_dispatch_on_conn(conn, parent_session_id)
            if current != registration:
                raise ContextContinuationError("CONTEXT_AUTHORITY_GENERATION_CHANGED")
            observed = self._read_context_rebase_snapshot_on_conn(conn, parent_session_id, **dict(zip(
                ("recent_limit", "user_limit", "unresolved_effect_limit", "authentic_user_limit"), snapshot_read_limits)))
            if observed.digest != expected_snapshot_digest or observed.has_pending_inputs or observed.dispatch_stopped:
                raise ContextContinuationError("CONTEXT_REBASE_STALE_SNAPSHOT")
            root = registration["authority"]["scope"]["root"]
            intent = {"schema": "SessionDBNativeContextRebaseV1", "root": root,
                      "transition_id": transition_id, "parent_session_id": parent_session_id,
                      "child_session_id": child_session_id, "continuation_digest": continuation_digest,
                      "snapshot_digest": expected_snapshot_digest, "control_digest": observed.action_control_digest,
                      "state": "planned", "registration": registration,
                      "retire_request": None, "retire_receipt": None,
                      "activate_request": None, "activate_receipt": None}
            now = time.time()
            recovery = {"schema": "SessionDBContextRebaseRecoveryV1", "transition_id": transition_id,
                        "child_session_id": child_session_id, "continuation_digest": continuation_digest,
                        "attempts": 1, "holder_digest": hashlib.sha256(turn_lease_holder.encode()).hexdigest(),
                        "action_control_digest": observed.action_control_digest,
                        "wake_dependency": "live_turn_lease_and_owner_reconciliation",
                        "next_check_at": now, "deadline_at": now + 900}
            expected_intent, expected_recovery = intent, recovery
            conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         ("native-context-rebase:" + root, _canonical(intent)))
            conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)",
                         ("context-rebase-recovery:" + transition_id, _canonical(recovery)))
            return intent

        try:
            intent = self._execute_write(reserve)
        except Exception:
            intent = self.read_native_context_rebase(parent_session_id)
            recovery_raw = self.get_meta("context-rebase-recovery:" + transition_id)
            if (expected_intent is None or not exact(intent, expected_intent)
                    or recovery_raw is None or not exact(_strict_json(recovery_raw), expected_recovery)):
                raise
        # A prepublication restart may read back exactly this request. It never
        # manufactures a second child or resets this existing recovery deadline.
        if intent["retire_receipt"] is None:
            def guard(conn):
                self._assert_context_rebase_lease_on_conn(conn, parent_session_id, turn_lease_holder)
                current = self._read_context_rebase_snapshot_on_conn(conn, parent_session_id)
                if current.action_control_digest != intent["control_digest"]:
                    raise ContextContinuationError("CONTEXT_TOOL_CONTROL_SUPERSEDED")
                raw = conn.execute("SELECT value FROM state_meta WHERE key=?",
                                   ("context-rebase-recovery:" + transition_id,)).fetchone()
                recovery = {} if raw is None else _strict_json(raw[0])
                if recovery.get("attempts", 4) > 3 or time.time() >= recovery.get("deadline_at", 0):
                    raise ContextContinuationError("CONTEXT_REBASE_RECOVERY_EXHAUSTED")
            return self._execute_native_rebase_transition(intent, "retire", parent_session_id,
                turn_lease_holder, {"action": "retire", "successor_context": child_session_id}, guard)
        return intent

    def _execute_native_rebase_transition(self, intent, phase, session_id, holder, action, guard):
        from hermes_state_continuity import _canonical, ContextContinuationError
        from ares_runtime.collaboration import DaemonPermitReceiptAdapter
        registration = self.read_native_context_authority(session_id)
        adapter = DaemonPermitReceiptAdapter(registration["transport"])
        identity = self.read_context_controller_identity()
        authority = registration["authority"]
        request = intent[phase + "_request"]
        if request is None:
            observed = snapshot(adapter.context_request("context_authority_readback",
                incarnation=authority["incarnation"], scope=authority["scope"])["snapshot"],
                scope=authority["scope"], public_key=identity["public_key"],
                incarnation=authority["incarnation"], expected=authority)
            if observed["approval_verifier"] != registration["approval_verifier"]:
                raise ContextContinuationError("CONTEXT_APPROVAL_VERIFIER_CHANGED")
            prepared = adapter.context_request("context_transition_prepare", authority=authority,
                transition_ref=intent["transition_id"] + ":" + phase, action=action)
            material, raw = signing_material(prepared, authority=authority, action=action)

            def sign(conn):
                guard(conn)
                if (self._native_rebase_intent_on_conn(conn, session_id) != intent
                        or self._native_context_authority_on_conn(conn, session_id) != registration
                        or self._context_controller_on_conn(conn) != identity):
                    raise ContextContinuationError("CONTEXT_NATIVE_REBASE_CHANGED")
                signed = {**material, "signature": list(key.sign(raw))}
                updated = {**intent, phase + "_request": signed}
                conn.execute("UPDATE state_meta SET value=? WHERE key=?",
                             (_canonical(updated), "native-context-rebase:" + intent["root"]))
                return updated
            with controller_credential(self.db_path, identity) as key:
                try:
                    intent = self._execute_write(sign)
                except Exception:
                    observed_intent = self.read_native_context_rebase(session_id)
                    if observed_intent is None or observed_intent.get(phase + "_request") is None:
                        raise
                    expected_intent = {**intent, phase + "_request": observed_intent[phase + "_request"]}
                    if not exact(expected_intent, observed_intent):
                        raise
                    intent = observed_intent
            request = intent[phase + "_request"]
        receipt = adapter.context_request("context_transition_readback", transition=request)["receipt"]
        if receipt is None:
            def before_send(conn):
                guard(conn)
                if (not exact(self._native_rebase_intent_on_conn(conn, session_id), intent)
                        or self._native_context_authority_on_conn(conn, session_id) != registration
                        or self._context_controller_on_conn(conn) != identity):
                    raise ContextContinuationError("CONTEXT_NATIVE_REBASE_CHANGED")
            self._execute_write(before_send)
            try:
                receipt = adapter.context_request("context_authority_transition", transition=request)["receipt"]
            except Exception:
                receipt = adapter.context_request("context_transition_readback", transition=request)["receipt"]
        closed(receipt, "request successor consumed recorded_at receipt_digest")
        head(receipt["successor"])
        successor = {"context": intent["child_session_id"],
                     "generation": request["authority"]["head"]["generation"] + (phase == "retire"),
                     "mode": "sealed" if phase == "retire" else "active"}
        from ares_runtime.continuity.authority import digest
        digest(receipt["receipt_digest"])
        if not exact(receipt["request"], request) or not exact(receipt["successor"], successor):
            raise ContextContinuationError("CONTEXT_NATIVE_TRANSITION_MISMATCH")
        updated = {**intent, phase + "_receipt": receipt,
                   "state": "retired" if phase == "retire" else "activated"}
        next_registration = deepcopy(registration)
        next_registration["authority"]["head"] = successor

        def observe(conn):
            self._assert_context_rebase_lease_on_conn(conn, session_id, holder)
            prior = self._native_rebase_intent_on_conn(conn, session_id)
            if exact(prior, updated):
                return updated
            if not exact(prior, intent) or self._native_context_authority_on_conn(conn, session_id) != registration:
                raise ContextContinuationError("CONTEXT_NATIVE_REBASE_CHANGED")
            # Preserve the native fence even if controls changed after sending.
            # Publication/READY has its own current-control check below.
            conn.execute("UPDATE state_meta SET value=? WHERE key=?",
                         (_canonical(updated), "native-context-rebase:" + intent["root"]))
            conn.execute("UPDATE state_meta SET value=? WHERE key=?",
                         (_canonical(next_registration), "native-context:" + intent["root"]))
            return updated
        try:
            return self._execute_write(observe)
        except Exception:
            if not exact(self.read_native_context_rebase(session_id), updated):
                raise
            return updated

    def _assert_native_rebase_publication_on_conn(self, conn, parent, child, transition_id, continuation_digest):
        from hermes_state_continuity import ContextContinuationError
        if self._native_context_authority_on_conn(conn, parent) is None:
            return
        intent = self._native_rebase_intent_on_conn(conn, parent)
        if (intent is None or intent["transition_id"] != transition_id or intent["child_session_id"] != child
                or intent["continuation_digest"] != continuation_digest or intent["state"] != "retired"
                or intent["retire_receipt"] is None):
            raise ContextContinuationError("CONTEXT_NATIVE_RETIREMENT_REQUIRED")

    def activate_native_context_rebase(self, transition_id, *, session_id, turn_lease_holder, reservation):
        from hermes_state_continuity import ContextContinuationError
        if self.read_native_context_authority(session_id) is None:
            return
        intent = self.read_native_context_rebase(session_id)
        transition = self.read_context_rebase_transition(transition_id)
        if (intent is None or transition is None or intent["transition_id"] != transition_id
                or intent["child_session_id"] != session_id or transition.child_session_id != session_id
                or intent["retire_receipt"] is None):
            raise ContextContinuationError("CONTEXT_NATIVE_RETIREMENT_REQUIRED")
        if intent["retire_receipt"]["consumed"]:
            # A tool's reported success is not independent external confirmation.
            # Keep every receipt reference and stop for its qualified owner.
            raise ContextContinuationError("CONTEXT_NATIVE_EFFECT_RECONCILIATION_REQUIRED")
        if intent["state"] != "activated":
            def guard(conn):
                self._assert_context_recovery_reservation_on_conn(conn, session_id, turn_lease_holder, reservation)
                self._check_run_control_on_conn(conn, session_id, reservation["control_digest"].removeprefix("sha256:"))
            self._execute_native_rebase_transition(intent, "activate", session_id, turn_lease_holder,
                {"action": "activate", "retirement_digest": intent["retire_receipt"]["receipt_digest"]}, guard)
        self.verify_native_context_current(session_id)

    def verify_native_context_current(self, session_id):
        from ares_runtime.collaboration import DaemonPermitReceiptAdapter
        from hermes_state_continuity import ContextContinuationError
        registration = self.read_native_context_authority(session_id)
        if registration is None:
            return
        identity = self.read_context_controller_identity()
        authority = registration["authority"]
        observed = snapshot(DaemonPermitReceiptAdapter(registration["transport"]).context_request(
            "context_authority_readback", incarnation=authority["incarnation"], scope=authority["scope"])["snapshot"],
            scope=authority["scope"], public_key=identity["public_key"], incarnation=authority["incarnation"], expected=authority)
        if (authority["head"]["context"] != session_id or authority["head"]["mode"] != "active"
                or observed["approval_verifier"] != registration["approval_verifier"]):
            raise ContextContinuationError("CONTEXT_AUTHORITY_GENERATION_CHANGED")
