"""Deterministic offline replay and Watch matching.

The caller supplies both source-coverage state and evaluation time. This module
has no provider, database, model, tool, network, or effect path.
"""

from __future__ import annotations

from collections import defaultdict

from .contracts import (
    AttentionContractError,
    EventCoverageState,
    EventDecisionV1,
    EventReplayResultV1,
    MatchDisposition,
    ProviderEventV1,
    SourceIdentityV1,
    WatchSpecV1,
    WatchState,
    _timestamp,
)


def _identity_key(identity: SourceIdentityV1) -> tuple[str, str, str, str, str]:
    return (
        identity.provider_id,
        identity.account_id,
        identity.object_type,
        identity.remote_id,
        identity.revision,
    )


def _matches_watch(
    event: ProviderEventV1,
    watch: WatchSpecV1,
    *,
    evaluated_at_ms: int,
) -> bool:
    if watch.state is not WatchState.ACTIVE:
        return False
    if not watch.not_before_ms <= evaluated_at_ms < watch.expires_at_ms:
        return False
    if not watch.not_before_ms <= event.occurred_at_ms < watch.expires_at_ms:
        return False
    identity = event.identity
    if (
        identity.provider_id != watch.provider_id
        or identity.account_id != watch.account_id
        or identity.object_type != watch.object_type
        or event.event_type != watch.event_type
    ):
        return False
    facts = {item.key: item.value for item in event.facts}
    return all(facts.get(item.key) == item.value for item in watch.required_facts)


def replay_events(
    events: tuple[ProviderEventV1, ...],
    watches: tuple[WatchSpecV1, ...],
    *,
    evaluated_at_ms: int,
    coverage: EventCoverageState,
) -> EventReplayResultV1:
    """Replay a synthetic event batch against explicit Watches.

    Exact duplicate deliveries are coalesced only within this supplied batch.
    Inputs are immutable finite tuples, not caller-controlled streams. Same full
    identity with differing payload facts is a conflict, not a silent duplicate.
    A changed revision has a distinct identity. Incomplete coverage returns
    reconciliation-required with no match decisions; an empty provider result
    is never inferred from an unknown/gapped source.
    """
    _timestamp(evaluated_at_ms, "evaluated_at_ms")
    if type(coverage) is not EventCoverageState:
        raise AttentionContractError("INVALID_COVERAGE_STATE")
    if type(events) is not tuple or type(watches) is not tuple:
        raise AttentionContractError("INVALID_REPLAY_INPUT")
    event_values = events
    watch_values = watches
    if any(type(event) is not ProviderEventV1 for event in event_values):
        raise AttentionContractError("INVALID_EVENT")
    if any(type(watch) is not WatchSpecV1 for watch in watch_values):
        raise AttentionContractError("INVALID_WATCH")
    watch_ids = [watch.watch_id for watch in watch_values]
    if len(set(watch_ids)) != len(watch_ids):
        raise AttentionContractError("DUPLICATE_WATCH_ID")

    if coverage is not EventCoverageState.COMPLETE:
        return EventReplayResultV1(
            coverage=coverage,
            reconciliation_required=True,
            decisions=(),
        )

    grouped: dict[SourceIdentityV1, list[ProviderEventV1]] = defaultdict(list)
    for event in event_values:
        grouped[event.identity].append(event)

    decisions: list[EventDecisionV1] = []
    for identity in sorted(grouped, key=_identity_key):
        deliveries = grouped[identity]
        event = deliveries[0]
        if any(item != event for item in deliveries[1:]):
            decisions.append(
                EventDecisionV1(
                    identity=identity,
                    disposition=MatchDisposition.IDENTITY_CONFLICT,
                    matched_watch_ids=(),
                    delivery_count=len(deliveries),
                    reason_code="SAME_IDENTITY_DIFFERENT_PAYLOAD",
                )
            )
            continue

        matches = tuple(
            sorted(
                watch.watch_id
                for watch in watch_values
                if _matches_watch(event, watch, evaluated_at_ms=evaluated_at_ms)
            )
        )
        if len(matches) > 1:
            disposition = MatchDisposition.AMBIGUOUS
            reason = "MULTIPLE_WATCHES_MATCH"
        elif matches and event.occurred_at_ms > evaluated_at_ms:
            disposition = MatchDisposition.NEEDS_REVIEW
            reason = "EVENT_AFTER_EVALUATION_TIME"
        elif matches:
            disposition = MatchDisposition.MATCHED
            reason = "EXACT_WATCH_MATCH"
        else:
            disposition = MatchDisposition.UNMATCHED
            reason = "NO_EXACT_WATCH_MATCH"
        decisions.append(
            EventDecisionV1(
                identity=identity,
                disposition=disposition,
                matched_watch_ids=matches,
                delivery_count=len(deliveries),
                reason_code=reason,
            )
        )

    return EventReplayResultV1(
        coverage=coverage,
        reconciliation_required=False,
        decisions=tuple(decisions),
    )


__all__ = ["replay_events"]
