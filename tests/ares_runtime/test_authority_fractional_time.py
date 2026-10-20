"""Temporal attenuation compares UTC instants without changing wire digests."""

import pytest

from ares_runtime.authority import (
    AuthorityScopeV1,
    ContractError,
    is_subset_scope,
    normalize_scope,
    scope_fingerprint,
)


WHOLE = "2026-10-04T00:00:00Z"
FRACTION = "2026-10-04T00:00:00.100000Z"


@pytest.mark.parametrize(
    "start,end",
    [
        (WHOLE, FRACTION),
        ("2026-10-04T01:00:00+01:00", FRACTION),
        (FRACTION, "2026-10-04T00:00:00.200000Z"),
        (WHOLE, "2026-10-04T00:00:00.000000Z"),
    ],
)
def test_interval_accepts_ordered_same_second_instants(start, end):
    scope = normalize_scope({"time": {"not_before": start, "not_after": end}})
    assert set(scope["time"]) == {"not_before", "not_after"}


@pytest.mark.parametrize(
    "start,end",
    [
        (FRACTION, WHOLE),
        (FRACTION, "2026-10-04T01:00:00+01:00"),
        ("2026-10-04T00:00:00.200000Z", FRACTION),
    ],
)
def test_interval_rejects_reversed_same_second_instants(start, end):
    with pytest.raises(ContractError, match="INVALID_TIME_SCOPE"):
        normalize_scope({"time": {"not_before": start, "not_after": end}})


@pytest.mark.parametrize(
    "bound,parent,child,contained",
    [
        ("not_after", WHOLE, FRACTION, False),
        ("not_after", FRACTION, WHOLE, True),
        ("not_before", FRACTION, WHOLE, False),
        ("not_before", WHOLE, FRACTION, True),
        ("not_after", WHOLE, "2026-10-04T01:00:00.000000+01:00", True),
        ("not_before", FRACTION, "2026-10-04T01:00:00.100000+01:00", True),
    ],
)
def test_same_second_subset_respects_each_bound(bound, parent, child, contained):
    assert is_subset_scope(
        {"time": {bound: child}}, {"time": {bound: parent}}
    ) is contained


@pytest.mark.parametrize(
    "bound,parent_time,child_time",
    [("not_after", WHOLE, FRACTION), ("not_before", FRACTION, WHOLE)],
)
def test_rejected_temporal_widening_does_not_spend_delegation(
    bound, parent_time, child_time
):
    parent = AuthorityScopeV1(
        scope={"tool": "read_file", "time": {bound: parent_time}, "use_count": 1},
        generation=1,
    )
    with pytest.raises(ContractError, match="ATTENUATION_ESCALATION"):
        parent.attenuate({"time": {bound: child_time}}, child_generation=2)
    child = parent.attenuate({"time": {bound: parent_time}}, child_generation=2)
    assert child.subset_witness(parent)["contained"] is True


def test_existing_canonical_timestamp_and_fingerprint_are_preserved():
    scope = {"time": {"not_after": WHOLE}}
    equivalent = {"time": {"not_after": "2026-10-04T01:00:00.000000+01:00"}}
    assert normalize_scope(scope) == scope
    assert normalize_scope(equivalent) == scope
    assert scope_fingerprint(equivalent) == scope_fingerprint(scope)
