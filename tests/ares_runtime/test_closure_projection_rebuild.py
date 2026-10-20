"""Rebuilding closed evidence is idempotent; reopening still needs evidence."""

import pytest

from ares_runtime.collaboration import ClosureProjector, ContractError


def project(projector, *, gates=None, events=("event:close",), flags=(), prior=None):
    return projector.project(
        "mission:rebuild",
        "engineering",
        {"test": True} if gates is None else gates,
        source_event_refs=events,
        source_event_exists=lambda _ref: True,
        flags=flags,
        previous_projection=prior,
    )


@pytest.mark.parametrize("prior_form", ["artifact", "mapping"])
def test_identical_closed_projection_rebuilds_same_artifact(prior_form):
    projector = ClosureProjector()
    closed = project(projector)
    prior = closed if prior_form == "artifact" else closed.to_dict()
    rebuilt = project(projector, prior=prior)
    assert rebuilt.canonical_bytes() == closed.canonical_bytes()
    assert rebuilt.artifact_digest == closed.artifact_digest


def test_rebuild_normalizes_duplicate_event_order_without_new_evidence():
    projector = ClosureProjector()
    closed = project(projector, events=("event:a", "event:b"))
    rebuilt = project(projector, events=("event:b", "event:a", "event:a"), prior=closed)
    assert rebuilt.canonical_bytes() == closed.canonical_bytes()


@pytest.mark.parametrize(
    "gates,flags",
    [
        ({"test": False}, ()),
        ({"test": True}, ("LEDGER_AHEAD_OF_UI",)),
        ({"test": True}, ("AMBIGUOUS_EFFECT",)),
    ],
)
def test_departure_from_closed_state_without_new_evidence_remains_rejected(gates, flags):
    projector = ClosureProjector()
    closed = project(projector)
    with pytest.raises(ContractError, match="REOPEN_REQUIRES_NEW_EVIDENCE"):
        project(projector, gates=gates, flags=flags, prior=closed)


@pytest.mark.parametrize("flags,state", [((), "evidence_pending"), (("AMBIGUOUS_EFFECT",), "quarantined")])
def test_new_source_event_allows_derived_reopening(flags, state):
    projector = ClosureProjector()
    closed = project(projector)
    reopened = project(
        projector,
        gates={"test": False},
        events=("event:close", "event:failure"),
        flags=flags,
        prior=closed,
    )
    assert reopened.to_dict()["state"] == state


def test_same_evidence_closed_rebuild_still_requires_matching_lineage():
    projector = ClosureProjector()
    closed = project(projector).to_dict()
    closed["mission_ref"] = "mission:other"
    with pytest.raises(ContractError, match="PROJECTION_LINEAGE_MISMATCH"):
        project(projector, prior=closed)


@pytest.mark.parametrize("change", ["gate", "event"])
def test_changed_closed_projection_without_new_evidence_is_still_rejected(change):
    projector = ClosureProjector()
    closed = project(projector, events=("event:a", "event:b"))
    gates = {"test": True, "other_check": True} if change == "gate" else None
    events = ("event:a",) if change == "event" else ("event:a", "event:b")
    with pytest.raises(ContractError, match="REOPEN_REQUIRES_NEW_EVIDENCE"):
        project(projector, gates=gates, events=events, prior=closed)


def test_identical_rebuild_rechecks_source_event_existence():
    projector = ClosureProjector()
    closed = project(projector)
    with pytest.raises(ContractError, match="MISSING_SOURCE_EVENT"):
        projector.project(
            "mission:rebuild", "engineering", {"test": True},
            source_event_refs=["event:close"], source_event_exists=lambda _ref: False,
            previous_projection=closed,
        )
