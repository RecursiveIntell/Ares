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
from .records import (
    EffectiveWatchState,
    WatchRecordV1,
    WorkItemRecordV1,
    WorkItemState,
)

__all__ = [
    "AttentionContractError",
    "EffectiveWatchState",
    "EventCoverageState",
    "EventDecisionV1",
    "EventFactV1",
    "EventReplayResultV1",
    "MatchDisposition",
    "ProviderEventV1",
    "SourceIdentityV1",
    "WatchRecordV1",
    "WatchSpecV1",
    "WatchState",
    "WorkItemRecordV1",
    "WorkItemState",
    "replay_events",
]
