"""Pure, immutable session orchestration contracts (no routing or credentials)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

POLICY_SCHEMA = "orchestration.policy/v1"
_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})


class OrchestrationError(ValueError):
    """A typed refusal; callers must not convert it into permission."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _identifier(value: Any) -> bool:
    return (
        type(value) is str and 0 < len(value) <= 1024
        and not any(char.isspace() or ord(char) < 32 for char in value)
    )


@dataclass(frozen=True)
class SecondaryRoute:
    provider: str
    model: str
    reasoning_effort: str | None = None
    service_tier: str | None = None

    def __post_init__(self):
        if not _identifier(self.provider) or not _identifier(self.model):
            raise OrchestrationError("policy_invalid")
        if self.reasoning_effort is not None and (
            type(self.reasoning_effort) is not str or self.reasoning_effort not in _EFFORTS
        ):
            raise OrchestrationError("policy_invalid")
        # Transport-specific tier availability is checked by the route owner,
        # not guessed or rewritten by this provider-neutral stored contract.
        if self.service_tier is not None and not _identifier(self.service_tier):
            raise OrchestrationError("policy_invalid")

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model,
                "reasoning_effort": self.reasoning_effort, "service_tier": self.service_tier}

    @classmethod
    def from_dict(cls, value: Any) -> SecondaryRoute:
        allowed = {"provider", "model", "reasoning_effort", "service_tier"}
        if type(value) is not dict or not {"provider", "model"} <= value.keys() or value.keys() - allowed:
            raise OrchestrationError("policy_invalid")
        return cls(**value)


@dataclass(frozen=True)
class OrchestrationPolicy:
    mode: str = "prefer_workers"
    allow_delegation: bool = True
    allow_graph: bool = True
    secondary_route: SecondaryRoute | None = None

    def __post_init__(self):
        if type(self.mode) is not str or self.mode not in {"prefer_workers", "direct"}:
            raise OrchestrationError("policy_invalid")
        if type(self.allow_delegation) is not bool or type(self.allow_graph) is not bool:
            raise OrchestrationError("policy_invalid")
        if self.secondary_route is not None and type(self.secondary_route) is not SecondaryRoute:
            raise OrchestrationError("policy_invalid")

    def to_dict(self) -> dict[str, Any]:
        return {"schema": POLICY_SCHEMA, "mode": self.mode,
                "allow_delegation": self.allow_delegation, "allow_graph": self.allow_graph,
                "secondary_route": self.secondary_route.to_dict() if self.secondary_route else None}

    @classmethod
    def from_dict(cls, value: Any) -> OrchestrationPolicy:
        expected = {"schema", "mode", "allow_delegation", "allow_graph", "secondary_route"}
        if type(value) is not dict or value.keys() != expected or value["schema"] != POLICY_SCHEMA:
            raise OrchestrationError("policy_invalid")
        route = SecondaryRoute.from_dict(value["secondary_route"]) if value["secondary_route"] is not None else None
        return cls(value["mode"], value["allow_delegation"], value["allow_graph"], route)
