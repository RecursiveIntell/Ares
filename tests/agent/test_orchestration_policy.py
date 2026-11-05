"""Strict, pure contract tests for session orchestration policy."""

from dataclasses import FrozenInstanceError

import pytest


from agent.orchestration_policy import (
    OrchestrationError,
    OrchestrationPolicy,
    SecondaryRoute,
)


def policy_payload(**overrides):
    payload = {
        "schema": "orchestration.policy/v1",
        "mode": "prefer_workers",
        "allow_delegation": True,
        "allow_graph": True,
        "secondary_route": {
            "provider": "openrouter",
            "model": "provider/model-exact",
            "reasoning_effort": "high",
            "service_tier": "priority",
        },
    }
    payload.update(overrides)
    return payload


def test_policy_round_trip_preserves_exact_secondary_route_and_is_immutable():
    raw = policy_payload()
    policy = OrchestrationPolicy.from_dict(raw)

    assert policy.to_dict() == raw
    assert policy.secondary_route == SecondaryRoute(
        provider="openrouter",
        model="provider/model-exact",
        reasoning_effort="high",
        service_tier="priority",
    )
    with pytest.raises(FrozenInstanceError):
        policy.mode = "direct"


def test_default_policy_has_declared_worker_preference_and_permissions():
    policy = OrchestrationPolicy()

    assert policy.to_dict() == {
        "schema": "orchestration.policy/v1",
        "mode": "prefer_workers",
        "allow_delegation": True,
        "allow_graph": True,
        "secondary_route": None,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", "unknown"),
        ("mode", "Prefer_workers"),
        ("allow_delegation", 1),
        ("allow_graph", "false"),
        ("schema", "orchestration.policy/v2"),
    ],
)
def test_policy_rejects_unknown_values_and_non_boolean_permissions(field, value):
    payload = policy_payload()
    payload[field] = value

    with pytest.raises(OrchestrationError) as caught:
        OrchestrationPolicy.from_dict(payload)

    assert caught.value.code == "policy_invalid"


@pytest.mark.parametrize(
    "route_override",
    [
        {"provider": " openrouter", "model": "m"},
        {"provider": "openrouter ", "model": "m"},
        {"provider": "", "model": "m"},
        {"provider": "openrouter", "model": " "},
        {"provider": "openrouter", "model": "m", "reasoning_effort": "medium-ish"},
        {"provider": "openrouter", "model": "m", "credentials": "never-accepted"},
    ],
)
def test_secondary_route_requires_exact_identifiers_known_effort_and_no_unknown_fields(
    route_override,
):
    payload = policy_payload(secondary_route=route_override)

    with pytest.raises(OrchestrationError) as caught:
        OrchestrationPolicy.from_dict(payload)

    assert caught.value.code == "policy_invalid"


def test_policy_rejects_unknown_fields_and_non_mapping_roots():
    payload = policy_payload(unexpected=True)
    with pytest.raises(OrchestrationError) as caught:
        OrchestrationPolicy.from_dict(payload)
    assert caught.value.code == "policy_invalid"

    for invalid in (None, [], "policy", 3):
        with pytest.raises(OrchestrationError) as caught:
            OrchestrationPolicy.from_dict(invalid)
        assert caught.value.code == "policy_invalid"


def test_secondary_route_accepts_each_declared_effort_without_rewriting_it():
    efforts = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")

    for effort in efforts:
        route = SecondaryRoute("provider", "model", reasoning_effort=effort)
        assert route.reasoning_effort == effort
        assert OrchestrationPolicy(secondary_route=route).to_dict()["secondary_route"][
            "reasoning_effort"
        ] == effort


def test_policy_direct_construction_rejects_truthy_non_boolean_permission():
    with pytest.raises(OrchestrationError) as caught:
        OrchestrationPolicy(allow_delegation=1)

    assert caught.value.code == "policy_invalid"
