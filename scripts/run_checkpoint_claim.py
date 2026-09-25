"""Private file-bound client of SessionDB's checked claim API.

The coordinator supplies its already-open writable SessionDB. This module never
opens, migrates, repairs, closes or substitutes a store, acquires a session lease,
or executes downstream work. Caller-supplied digests bind observations, not
permission. Files are observed before the native transaction, not atomically
with it. Unknown write/readback outcomes require reconciliation, never retry.
"""
from hermes_state import SessionDB
from hermes_state_runs import RunCheckpoint, RunCustodyError, RunTaskBinding
from scripts.run_checkpoint_resume import (
    BoundFileReader, ResumeRefusal, exact_keys, strict_json, verify_checkpoint_files,
)


class ClaimRefusal(RuntimeError):
    """Stable code: preflight or native admission refused this invocation."""


class ClaimOutcomeUnknown(RuntimeError):
    """A write may have committed; reconcile native head before any new action."""


def claim_from_files(db, *, run_id, expected_generation, request_path,
                     expected_request_digest, origin_session_id,
                     controller_pid, ttl_seconds, historical_goal_digest=None,
                     task_binding=None, expected_control_digest=None,
                     expected_session_id=None, expected_lease_holder=None, _on_claim=None):
    """Claim through the native owner and compare its exact persisted value.

    An initial claim has caller-selected inventory; this does not authenticate
    its completeness. Takeover remains bound to the native exact checkpoint.
    This function deliberately has no low-level fallback or automatic retry.
    """
    if not isinstance(db, SessionDB) or db.read_only:
        raise ClaimRefusal("WRITABLE_OWNER_REQUIRED")
    v2 = task_binding is not None
    if (v2 and historical_goal_digest is not None
            or not v2 and (historical_goal_digest is None or expected_control_digest is not None)):
        raise ClaimRefusal("MIXED_TASK_BINDING")
    try:
        if v2:
            task_binding = RunTaskBinding.from_dict(task_binding)
            if task_binding.origin_session_id != origin_session_id:
                raise ClaimRefusal("HISTORY_MISMATCH")
        reader = BoundFileReader()
        request = strict_json(reader.verified(str(request_path), expected_request_digest))
        exact_keys(request, ("checkpoint", "files", "session_id", "lease_holder"))
        # The live dispatcher supplies both bindings from its selected agent,
        # never from RPC params. Other private callers retain native-only checks.
        if expected_session_id is not None or expected_lease_holder is not None:
            if (type(expected_session_id) is not str or not expected_session_id
                    or type(expected_lease_holder) is not str or not expected_lease_holder):
                raise ClaimRefusal("INVALID_LIVE_BINDING")
            if request["session_id"] != expected_session_id:
                raise ClaimRefusal("SESSION_MISMATCH")
            if request["lease_holder"] != expected_lease_holder:
                raise ClaimRefusal("LEASE_MISMATCH")
        checkpoint = RunCheckpoint.from_dict(request["checkpoint"])
        source_count = verify_checkpoint_files(checkpoint, request["files"], reader)
    except (ResumeRefusal, RunCustodyError) as exc:
        raise ClaimRefusal(str(exc)) from None
    except (TypeError, ValueError, KeyError, RecursionError):
        raise ClaimRefusal("INVALID_REQUEST") from None
    try:
        common = dict(expected_generation=expected_generation, checkpoint=checkpoint,
            current_session_id=request["session_id"], lease_holder=request["lease_holder"],
            controller_pid=controller_pid, ttl_seconds=ttl_seconds)
        if v2:
            value = db.claim_run_task_custody_checked(run_id, **common,
                task_binding=task_binding, expected_control_digest=expected_control_digest)
        else:
            value = db.claim_run_custody_checked(run_id, **common,
                origin_session_id=origin_session_id, historical_goal_digest=historical_goal_digest)
    except RunCustodyError as exc:
        raise ClaimRefusal(exc.code) from None
    except BaseException:  # Cancellation can follow a committed native mutation.
        # Even a transport-looking error can be a lost ACK after commit.
        raise ClaimOutcomeUnknown("CLAIM_OUTCOME_UNKNOWN") from None
    try:
        # Private executing-owner hook: retain the native handle before readback.
        # It never crosses the RPC boundary and does not authorize other effects.
        if _on_claim is not None:
            _on_claim(value)
        current = db.read_run_custody(run_id)
    except BaseException:  # Cancellation can follow a committed native mutation.
        raise ClaimOutcomeUnknown("CLAIM_READBACK_UNKNOWN") from None
    if current != value:
        raise ClaimOutcomeUnknown("CLAIM_READBACK_MISMATCH")
    return {"status": "claim_observed", "run_id": value.run_id,
            "generation": value.generation, "custody_changed": True,
            "resume_authorized": False, "downstream_effects_executed": False,
            "source_files": source_count, "members": len(checkpoint.members),
            "unresolved_effects": len(checkpoint.unresolved_effects),
            "unresolved_findings": len(checkpoint.unresolved_findings),
            "restrictions": len(checkpoint.restrictions)}
