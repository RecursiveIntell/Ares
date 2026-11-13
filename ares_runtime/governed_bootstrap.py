"""Consume-time policy-basis admission for managed model calls.

This module is deliberately pre-egress and non-persistent. It accepts the exact
profile-runtime owner output, performs a fresh consume-time owner readback
through a caller-supplied resolver, and returns a task-bound
``ResolvedPolicyBasisV2``. A serialized basis is evidence only: it never carries
the in-process owner capability, and the resolver is re-invoked on every
admission attempt rather than trusting a prior round-trip.

Refusal is typed and never repaired: a task mismatch, a missing or changed
owner, a not-yet-valid window, an expired window, or a blocked basis all raise
``ContractError`` with a stable code for the caller to record.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .collaboration import ContractError, ResolvedPolicyBasisV2

BOOTSTRAP_SCHEMA = "AresPolicyBootstrapV1"


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ContractError("INVALID_POLICY_FIELD", "not_before/not_after")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ContractError("INVALID_POLICY_FIELD", "not_before/not_after") from error
    if parsed.tzinfo is None:
        raise ContractError("INVALID_POLICY_FIELD", "not_before/not_after")
    return parsed.astimezone(timezone.utc)


def resolve_policy_basis(
    raw: Mapping[str, Any],
    *,
    expected_task_ref: str,
    resolve_owner: Callable[[str, str], Mapping[str, Any] | None],
    now: datetime | None = None,
) -> ResolvedPolicyBasisV2:
    """Admit one exact task-bound policy basis, or refuse with a typed code.

    ``resolve_owner`` is the production readback seam: it receives the owner's
    ``basis_ref`` and ``blake3:<digest>`` and must return the *current* owner
    payload, or ``None`` when unavailable. The canonical V2 constructor performs
    the schema, task-binding, and owner-equality checks; this wrapper adds the
    consume-time validity window so a stale owner cannot be admitted.
    """
    basis = ResolvedPolicyBasisV2.from_profile_runtime(
        raw,
        expected_task_ref=expected_task_ref,
        resolve_owner=resolve_owner,
    )
    # V2 keeps the composed owner at ``owner_projection`` and the interpreted
    # v1 policy payload one level below it; the validity window lives on the
    # interpreted payload (the object the canonical validator admitted).
    owner = basis.to_dict()["owner_projection"]["owner_projection"]
    current = now or datetime.now(timezone.utc)
    not_before = _parse_utc(owner.get("not_before"))
    not_after = _parse_utc(owner.get("not_after"))
    if not_before > current:
        raise ContractError("POLICY_NOT_YET_VALID")
    if not_after <= current:
        raise ContractError("POLICY_EXPIRED")
    if owner.get("status") != "admitted":
        raise ContractError("POLICY_BASIS_BLOCKED")
    return basis


__all__ = ["BOOTSTRAP_SCHEMA", "resolve_policy_basis"]
