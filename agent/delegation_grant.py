"""Attenuation grant record for a delegated child (Task 2.2).

The child's authority scope is derived from the parent's through the canonical
``AuthorityScopeV1.attenuate`` (ares_runtime/authority.py), so the monotone-narrowing
rule is the owner's, not a second copy. The record also carries the child's iteration
budget and its window end, the two limits the audit names alongside tools.

The record is data for audit and receipts. It grants nothing by itself: the existing
effect boundary and budget debit still enforce each limit at run time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Optional

from ares_runtime.authority import AuthorityScopeV1, ContractError, is_subset_scope, normalize_scope


def build_child_grant_record(
    *,
    parent_scope: Mapping[str, Any],
    parent_generation: int,
    child_scope: Mapping[str, Any],
    child_generation: int,
    child_budget: int,
    parent_budget: int,
    window_not_after: Optional[datetime],
    child_id: str,
) -> dict[str, Any]:
    """Return an audit record for one child's attenuated grant.

    Raises the authority layer's ContractError if the child widens the parent's scope.
    Raises ValueError if the child's budget exceeds the parent's remaining budget, since
    debit-from-parent makes that a widening too.
    """
    if child_budget < 1 or parent_budget < 0:
        raise ValueError("budgets must be non-negative and the child budget positive")
    if child_budget > parent_budget:
        raise ValueError("child budget exceeds the parent's remaining budget (debit-from-parent)")

    parent = AuthorityScopeV1(scope=parent_scope, generation=parent_generation)
    # attenuate() RESTATES missing parent restrictions instead of refusing them, so it
    # cannot be the widening check. The explicit containment check is the refusal: a
    # child that drops a parent restriction is not a subset and is rejected here.
    if not is_subset_scope(child_scope, parent_scope):
        raise ContractError("CHILD_SCOPE_WIDENS_PARENT")
    child = parent.attenuate(child_scope, child_generation=child_generation, child_holder=child_id)

    return {
        "schema": "ares.delegation.grant/v1",
        "child_id": child_id,
        "parent_fingerprint": parent.fingerprint(),
        "child_fingerprint": child.fingerprint(),
        "child_scope": normalize_scope(child_scope),
        "child_generation": child_generation,
        "budget": {"child_max": int(child_budget), "parent_remaining": int(parent_budget)},
        "window_not_after": window_not_after.isoformat() if window_not_after is not None else None,
    }


__all__ = ["build_child_grant_record"]
