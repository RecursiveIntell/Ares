"""Task 2.2 grant record: a child's grant can only narrow the parent's authority.

These tests exercise the attenuation owner through the record builder. Each widening
case the audit names (tool, budget, expiry) must be refused, and a valid narrowing must
produce a record whose fingerprints are bound to the parent and child.
"""

from datetime import datetime, timedelta, timezone

import pytest

from agent.delegation_grant import build_child_grant_record
from ares_runtime.authority import ContractError

WINDOW = datetime(2026, 11, 9, 13, 0, tzinfo=timezone.utc)
PARENT = {"tool": "terminal", "time": {"not_after": "2026-11-09T14:00:00+00:00"}}


def _record(**overrides):
    kwargs = dict(
        parent_scope=PARENT,
        parent_generation=1,
        child_scope={"tool": "terminal", "time": {"not_after": "2026-11-09T13:30:00+00:00"}},
        child_generation=2,
        child_budget=5,
        parent_budget=10,
        window_not_after=WINDOW,
        child_id="child-1",
    )
    kwargs.update(overrides)
    return build_child_grant_record(**kwargs)


def test_valid_narrowing_produces_a_bound_record():
    rec = _record()
    assert rec["schema"] == "ares.delegation.grant/v1"
    assert rec["child_id"] == "child-1"
    assert rec["parent_fingerprint"] != rec["child_fingerprint"], "a narrowed child is a distinct scope"
    assert rec["budget"] == {"child_max": 5, "parent_remaining": 10}


def test_child_dropping_a_parent_restriction_is_refused():
    # Parent restricts the tool; a child that omits the tool field would widen.
    with pytest.raises(ContractError):
        _record(child_scope={"time": {"not_after": "2026-11-09T13:30:00+00:00"}})


def test_child_expiry_later_than_parent_is_refused():
    with pytest.raises(ContractError):
        _record(child_scope={"tool": "terminal", "time": {"not_after": "2026-11-09T15:00:00+00:00"}})


def test_child_budget_above_parent_remaining_is_refused():
    with pytest.raises(ValueError):
        _record(child_budget=11, parent_budget=10)


def test_zero_child_budget_is_refused():
    with pytest.raises(ValueError):
        _record(child_budget=0)
