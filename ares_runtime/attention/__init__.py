"""Offline synthetic attention-event contract and deterministic matcher."""

from .contracts import (
    AttentionContractError,
    EventCoverageState,
    EventDecisionV1,
    EventFactV1,
    EventReplayResultV1,
    MatchDisposition,
    ProviderEventV1,
    SourceIdentityV1,
    WatchSpecV1,
    WatchState,
)
from .matcher import replay_events

__all__ = [
    "AttentionContractError",
    "EventCoverageState",
    "EventDecisionV1",
    "EventFactV1",
    "EventReplayResultV1",
    "MatchDisposition",
    "ProviderEventV1",
    "SourceIdentityV1",
    "WatchSpecV1",
    "WatchState",
    "replay_events",
]
