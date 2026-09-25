"""Private turn-bound handles; SessionDB remains the sole durable custody owner.

No database is opened here and no renewal timer is created. Uncertain mutations
retain a handle requiring reconciliation; neither teardown nor a later turn
retries them. Tokens and lease holders never appear in returned summaries.
"""
from dataclasses import dataclass, field
import os
import threading

from hermes_state_runs import RunCustody, RunCustodyV2, RunCustodyError
from scripts import run_checkpoint_claim as claim_client
from scripts.run_checkpoint_claim import ClaimOutcomeUnknown, ClaimRefusal


@dataclass
class _Handle:
    holder: str = field(repr=False)
    value: RunCustody | RunCustodyV2 | None = field(default=None, repr=False)
    status: str = "pending"
    error: str = "CLAIM_OUTCOME_UNKNOWN"
    recovery_pending: bool = False


class TurnRunCustody:
    """Per-agent capability handles, not a second persistent lease/fence."""

    def __init__(self, db):
        self.db = db
        self._lock = threading.RLock()
        self._active_holder = None
        self._handles: dict[str, _Handle] = {}

    def begin_turn(self, holder):
        with self._lock:
            if type(holder) is not str or not holder:
                raise ClaimRefusal("INVALID_LIVE_BINDING")
            if self._active_holder is not None:
                raise ClaimRefusal("TURN_ALREADY_ACTIVE")
            self._active_holder = holder

    def _active(self, holder):
        if not holder or self._active_holder != holder:
            raise ClaimRefusal("TURN_NOT_ACTIVE")

    def _handle(self, holder, run_id, generation):
        handle = self._handles.get(run_id)
        if handle is None:
            raise ClaimRefusal("CUSTODY_HANDLE_REQUIRED")
        if handle.status != "owned":
            raise ClaimOutcomeUnknown(handle.error)
        if handle.holder != holder:
            raise ClaimRefusal("TURN_NOT_ACTIVE")
        if handle.value is None or handle.value.generation != generation:
            raise ClaimRefusal("FENCE_MISMATCH")
        return handle

    def claim(self, holder, *, session_id, run_id, **kwargs):
        with self._lock:
            self._active(holder)
            if run_id in self._handles:
                if self._handles[run_id].status != "owned":
                    raise ClaimOutcomeUnknown(self._handles[run_id].error)
                if self._handles[run_id].value.generation != kwargs.get("expected_generation"):
                    raise ClaimRefusal("FENCE_MISMATCH")
                raise ClaimRefusal("OWNER_ACTIVE")
            if len(self._handles) >= 16:
                raise ClaimRefusal("CUSTODY_HANDLE_LIMIT")
            handle = _Handle(holder)
            self._handles[run_id] = handle

            def captured(value):
                handle.value = value

            try:
                result = claim_client.claim_from_files(self.db, run_id=run_id,
                    expected_session_id=session_id, expected_lease_holder=holder,
                    controller_pid=os.getpid(), _on_claim=captured, **kwargs)
            except ClaimRefusal:
                del self._handles[run_id]
                raise
            except ClaimOutcomeUnknown as exc:
                handle.status, handle.error = "unknown", str(exc)
                raise
            except BaseException:  # Cancellation can follow a committed native mutation.
                handle.status, handle.error = "unknown", "CLAIM_OUTCOME_UNKNOWN"
                raise ClaimOutcomeUnknown(handle.error) from None
            if type(handle.value) not in (RunCustody, RunCustodyV2):
                handle.status, handle.error = "unknown", "CLAIM_HANDLE_UNKNOWN"
                raise ClaimOutcomeUnknown(handle.error)
            handle.status = "owned"
            return result

    @staticmethod
    def _summary(value, status):
        return {"status": status, "run_id": value.run_id, "generation": value.generation,
                "custody_changed": True, "resume_authorized": False,
                "downstream_effects_executed": False}

    def _mutate(self, handle, operation, **kwargs):
        value = handle.value
        # Mark uncertainty before crossing the native effect boundary, including
        # cancellation between commit and Python return/readback.
        handle.status, handle.error = "pending", "CUSTODY_OUTCOME_UNKNOWN"
        try:
            updated = operation(value.run_id, owner_token=value.owner_token,
                                expected_generation=value.generation, **kwargs)
        except RunCustodyError as exc:
            handle.status = "owned"
            raise ClaimRefusal(exc.code) from None
        except BaseException:  # Cancellation can follow a committed native mutation.
            handle.status, handle.error = "unknown", "CUSTODY_OUTCOME_UNKNOWN"
            raise ClaimOutcomeUnknown(handle.error) from None
        # Retain the native return before readback; an ACK/readback failure must
        # not discard the token or be turned into an automatic second mutation.
        handle.value = updated
        try:
            if self.db.read_run_custody(updated.run_id) != updated:
                raise ClaimOutcomeUnknown("CUSTODY_READBACK_MISMATCH")
        except BaseException:  # Cancellation can follow a committed native mutation.
            handle.status, handle.error = "unknown", "CUSTODY_READBACK_UNKNOWN"
            raise ClaimOutcomeUnknown(handle.error) from None
        handle.status = "owned" if updated.disposition == "active" else "released"
        return updated

    def refresh(self, holder, *, run_id, expected_generation, ttl_seconds):
        with self._lock:
            self._active(holder)
            handle = self._handle(holder, run_id, expected_generation)
            value = self._mutate(handle, self.db.refresh_run_custody, ttl_seconds=ttl_seconds)
            return self._summary(value, "refresh_observed")

    def bind_recovery_files(self, holder, *, run_id, expected_generation, files, ttl_seconds=300):
        """Explicit checked locator renewal after checkpoint/source advancement."""
        from scripts.run_checkpoint_resume import BoundFileReader, ResumeRefusal, verify_checkpoint_files
        with self._lock:
            self._active(holder)
            handle = self._handle(holder, run_id, expected_generation)
            value = handle.value
            try:
                control = self.db.read_context_rebase_snapshot(value.current_session_id).action_control_digest
                verify_checkpoint_files(value.checkpoint, files, BoundFileReader())
            except (ResumeRefusal, RunCustodyError) as exc:
                raise ClaimRefusal(str(exc)) from None
            updated = self._mutate(handle, self.db.bind_run_recovery_files_checked,
                lease_holder=holder, expected_control_digest=control.removeprefix("sha256:"),
                files=files, ttl_seconds=ttl_seconds)
            return self._summary(updated, "recovery_files_bound")

    def assert_goal_migration_safe(self, holder, *, session_id):
        """Refuse to mutate a goal row that an owned checkpoint binds as history."""
        with self._lock:
            self._active(holder)
            mutable_key = f"goal:{session_id}"
            for handle in self._handles.values():
                if handle.holder != holder or handle.status != "owned" or handle.value is None:
                    continue
                historical_key = dict(handle.value.checkpoint.members).get(
                    "historical-goal-key"
                )
                if historical_key == mutable_key:
                    raise ClaimRefusal("HISTORICAL_GOAL_ALIAS_COLLISION")
        return True

    def transfer_session(self, holder, *, old_session_id, new_session_id, ttl_seconds):
        """Transfer every owned run handle across one already-published session edge."""
        with self._lock:
            self._active(holder)
            summaries = []
            for run_id, handle in list(self._handles.items()):
                if handle.holder != holder or handle.status != "owned":
                    continue
                value = self._mutate(
                    handle, self.db.transfer_run_session,
                    expected_session_id=old_session_id, new_session_id=new_session_id,
                    ttl_seconds=ttl_seconds,
                )
                summaries.append(self._summary(value, "session_transfer_observed"))
            return summaries

    def publish_context_rebase(self, holder, **publication):
        """Keep local handles aligned with atomic native child/custody publication."""
        from hermes_state_continuity import ContextContinuationError

        with self._lock:
            self._active(holder)
            handles = []
            for handle in self._handles.values():
                if handle.holder != holder:
                    raise ClaimRefusal("TURN_NOT_ACTIVE")
                if handle.status != "owned" or handle.value is None:
                    raise ClaimOutcomeUnknown(handle.error)
                handles.append(handle)
            for handle in handles:
                handle.status, handle.error = "pending", "CONTEXT_REBASE_CUSTODY_UNKNOWN"
            try:
                result = self.db.publish_context_rebase_child(
                    **publication, custody_transfers=tuple(handle.value for handle in handles),
                )
            except (ContextContinuationError, RunCustodyError):
                # Native typed refusal rolls the entire owner transaction back.
                for handle in handles:
                    handle.status = "owned"
                raise
            except BaseException:
                for handle in handles:
                    handle.status = "unknown"
                raise ClaimOutcomeUnknown("CONTEXT_REBASE_CUSTODY_UNKNOWN") from None
            for handle in handles:
                handle.recovery_pending = True
            self.reconcile_context_rebase(holder, result.child_session_id)
            return result

    def reconcile_context_rebase(self, holder, child_session_id, *, recovery_reservation=None):
        """Resolve a lost publication ACK by readback, without a second mutation."""
        with self._lock:
            self._active(holder)
            for run_id, handle in self._handles.items():
                if handle.status != "owned" and handle.error != "CONTEXT_REBASE_CUSTODY_UNKNOWN":
                    raise ClaimOutcomeUnknown(handle.error)
                if (handle.value is None or handle.holder != holder
                        and not (handle.recovery_pending and handle.status == "owned")):
                    raise ClaimOutcomeUnknown("CONTEXT_REBASE_CUSTODY_UNKNOWN")
                old = handle.value
                current = self.db.read_run_custody(run_id)
                if (current is None or current.current_session_id != child_session_id
                        or current.owner_token != old.owner_token
                        or current.checkpoint != old.checkpoint
                        or current.generation not in {old.generation, old.generation + 1}
                        or current.disposition != "active"):
                    handle.status, handle.error = "unknown", "CONTEXT_REBASE_CUSTODY_UNKNOWN"
                    raise ClaimOutcomeUnknown(handle.error)
                handle.value, handle.status = current, "owned"
                handle.holder = holder
                if recovery_reservation is not None:
                    handle.recovery_pending = True
            native = self.db.list_run_custody_for_session(child_session_id)
            if len(native) > 16:
                raise ClaimRefusal("CUSTODY_HANDLE_LIMIT")
            if recovery_reservation is not None:
                # Recovery is entered only under the runtime's durable bounded
                # reservation. A missing handle is distinct from an uncertain
                # same-process mutation, which was refused above.
                for value in native:
                    if value.run_id in self._handles:
                        continue
                    handle = _Handle(holder, recovery_pending=True)
                    self._handles[value.run_id] = handle
                    def captured(updated):
                        handle.value = updated
                    try:
                        claim_client.recover_from_files(self.db, value,
                            expected_session_id=child_session_id, expected_lease_holder=holder,
                            expected_control_digest=recovery_reservation["control_digest"].removeprefix("sha256:"),
                            recovery_reservation=recovery_reservation, controller_pid=os.getpid(),
                            ttl_seconds=300, _on_claim=captured)
                    except ClaimRefusal:
                        del self._handles[value.run_id]
                        raise
                    except BaseException as exc:
                        handle.status = "unknown"
                        handle.error = str(exc) if isinstance(exc, ClaimOutcomeUnknown) else "CLAIM_OUTCOME_UNKNOWN"
                        raise ClaimOutcomeUnknown(handle.error) from None
                    handle.status = "owned"
            owned = {run_id for run_id, handle in self._handles.items() if handle.status == "owned"}
            current_ids = {value.run_id for value in self.db.list_run_custody_for_session(child_session_id)}
            if current_ids != owned:
                raise ClaimOutcomeUnknown("CONTEXT_REBASE_CUSTODY_HANDLE_REQUIRED")
            return tuple(self._handles[run_id].value for run_id in sorted(owned))

    def release(self, holder, *, run_id, expected_generation):
        with self._lock:
            handle = self._handle(holder, run_id, expected_generation)
            value = self._mutate(handle, self.db.release_run_custody)
            del self._handles[run_id]
            return self._summary(value, "release_observed")

    def finish_turn(self, holder):
        with self._lock:
            if not holder or self._active_holder != holder:
                return []
            # Close admission before any cleanup; late claims cannot outlive
            # this turn even while the native session lease still exists.
            self._active_holder = None
            errors = []
            for run_id, handle in list(self._handles.items()):
                if handle.holder != holder:
                    continue
                if handle.status == "released":
                    del self._handles[run_id]
                    continue
                if handle.status == "owned" and handle.recovery_pending:
                    try:
                        transition = self.db.context_rebase_transition_for_session(handle.value.current_session_id)
                        completed = transition is not None and transition.state == "ready"
                    except Exception:
                        completed = False
                    if not completed:
                        errors.append({"run_id": run_id, "status": "recovery_pending",
                                       "code": "RUN_CUSTODY_RECOVERY_PENDING"})
                        continue
                    handle.recovery_pending = False
                if handle.status == "owned":
                    try:
                        self.release(holder, run_id=run_id, expected_generation=handle.value.generation)
                    except (ClaimRefusal, ClaimOutcomeUnknown) as exc:
                        handle.status, handle.error = "unknown", str(exc)
                if handle.status != "owned" and run_id in self._handles:
                    errors.append({"run_id": run_id, "status": "unknown", "code": handle.error})
            return errors
