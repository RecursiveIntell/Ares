"""Behavior contract for the pure durable WorkItem/Watch records (P02).

Decisions under test (operator-approved 2026-10-24):
* a WorkItem is provider-neutral; provider/account scope lives only on a Watch;
* only the current record plus a monotonic revision is kept (no history);
* expiry is derived at read time from ``expires_at_ms``; it is never persisted
  and resume never extends the original window.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ares_runtime.attention import (
    AttentionContractError,
    EventFactV1,
    WatchSpecV1,
    WatchState,
)
from ares_runtime.attention.records import (
    EffectiveWatchState,
    WatchRecordV1,
    WorkItemRecordV1,
    WorkItemState,
    canonical_json,
)

T0 = 1_000
T1 = 2_000
EXPIRES = 10_000


def work_item(**over: Any) -> WorkItemRecordV1:
    args: dict[str, Any] = {
        "work_item_id": "wi-1",
        "goal": "Compare Alex's revised agreement and prepare a reply",
        "criteria": ("payment terms unchanged", "term length is 12 months"),
        "now_ms": T0,
    }
    args.update(over)
    return WorkItemRecordV1.create(**args)


def spec(**over: Any) -> WatchSpecV1:
    args: dict[str, Any] = {
        "watch_id": "w-1",
        "expectation_id": "wi-1",
        "provider_id": "synthetic",
        "account_id": "acct",
        "object_type": "message",
        "event_type": "arrived",
        "required_facts": (EventFactV1("sender", "alex@example.test"),),
        "not_before_ms": T0,
        "expires_at_ms": EXPIRES,
        "state": WatchState.ACTIVE,
    }
    args.update(over)
    return WatchSpecV1(**args)


def watch(**over: Any) -> WatchRecordV1:
    return WatchRecordV1.create(spec(**over), now_ms=T0)


# --- creation -------------------------------------------------------------


def test_work_item_is_provider_neutral_and_starts_active_at_revision_one():
    record = work_item()
    assert record.state is WorkItemState.ACTIVE
    assert record.revision == 1
    assert record.created_at_ms == record.updated_at_ms == T0
    assert not any("provider" in key or "account" in key for key in record.to_dict())


def test_watch_record_carries_the_p01_spec_without_changing_it():
    record = watch()
    assert record.to_spec() == spec()
    assert record.revision == 1


@pytest.mark.parametrize(
    "over, code",
    [
        ({"work_item_id": ""}, "INVALID_TEXT"),
        ({"work_item_id": " "}, "INVALID_TEXT"),
        ({"work_item_id": "a\nb"}, "INVALID_IDENTIFIER"),
        ({"work_item_id": "x" * 257}, "INVALID_IDENTIFIER"),
        ({"goal": ""}, "INVALID_TEXT"),
        ({"criteria": ()}, "INVALID_CRITERIA"),
        ({"criteria": ["list-not-tuple"]}, "INVALID_CRITERIA"),
        ({"criteria": ("a", "a")}, "DUPLICATE_CRITERION"),
        ({"criteria": ("ok", "")}, "INVALID_TEXT"),
        ({"now_ms": -1}, "INVALID_TIMESTAMP"),
        ({"now_ms": True}, "INVALID_TIMESTAMP"),
    ],
)
def test_work_item_creation_refuses_malformed_input(over, code):
    with pytest.raises(AttentionContractError) as caught:
        work_item(**over)
    assert caught.value.code == code


def test_watch_must_start_active():
    with pytest.raises(AttentionContractError) as caught:
        watch(state=WatchState.PAUSED)
    assert caught.value.code == "WATCH_MUST_START_ACTIVE"


def test_watch_creation_refuses_an_already_expired_window():
    with pytest.raises(AttentionContractError) as caught:
        WatchRecordV1.create(spec(), now_ms=EXPIRES)
    assert caught.value.code == "WATCH_ALREADY_EXPIRED"


# --- strict serialization -------------------------------------------------


@pytest.mark.parametrize("make", [work_item, watch])
def test_round_trip_is_exact_and_canonical(make):
    record = make()
    text = canonical_json(record.to_dict())
    assert text == canonical_json(json.loads(text))
    assert type(record).parse(json.loads(text)) == record
    # Re-serializing the parsed record yields byte-identical text.
    assert canonical_json(type(record).parse(json.loads(text)).to_dict()) == text


@pytest.mark.parametrize("make", [work_item, watch])
def test_parse_rejects_missing_extra_and_retyped_fields(make):
    good = make().to_dict()
    cls = type(make())
    for key in good:
        broken = dict(good)
        del broken[key]
        with pytest.raises(AttentionContractError):
            cls.parse(broken)
    with pytest.raises(AttentionContractError):
        cls.parse({**good, "unexpected": 1})
    with pytest.raises(AttentionContractError):
        cls.parse({**good, "schema": "ares.attention.other.v1"})
    with pytest.raises(AttentionContractError):
        cls.parse({**good, "revision": "1"})
    with pytest.raises(AttentionContractError):
        cls.parse({**good, "revision": 0})
    with pytest.raises(AttentionContractError):
        cls.parse({**good, "state": "expired"})  # expiry is derived, never stored
    with pytest.raises(AttentionContractError):
        cls.parse({**good, "updated_at_ms": good["created_at_ms"] - 1})


def test_parse_rejects_duplicate_fact_keys_and_bad_window():
    good = watch().to_dict()
    dup = [{"key": "k", "value": "1"}, {"key": "k", "value": "2"}]
    with pytest.raises(AttentionContractError) as caught:
        WatchRecordV1.parse({**good, "required_facts": dup})
    assert caught.value.code == "DUPLICATE_FACT_KEY"
    with pytest.raises(AttentionContractError) as caught:
        WatchRecordV1.parse({**good, "expires_at_ms": good["not_before_ms"]})
    assert caught.value.code == "INVALID_WATCH_WINDOW"


# --- lifecycle ------------------------------------------------------------


def test_transition_bumps_revision_and_updated_time_but_nothing_authored():
    paused = work_item().transition(WorkItemState.PAUSED, now_ms=T1)
    assert (paused.state, paused.revision, paused.updated_at_ms) == (
        WorkItemState.PAUSED,
        2,
        T1,
    )
    original = work_item()
    assert paused.goal == original.goal
    assert paused.criteria == original.criteria
    assert paused.created_at_ms == original.created_at_ms


@pytest.mark.parametrize("cls_make", [work_item, watch])
def test_cancelled_is_terminal_and_same_state_is_refused(cls_make):
    record = cls_make()
    state_type = type(record.state)
    cancelled = record.transition(state_type.CANCELLED, now_ms=T1)
    for target in state_type:
        with pytest.raises(AttentionContractError) as caught:
            cancelled.transition(target, now_ms=T1 + 1)
        assert caught.value.code in {"ILLEGAL_TRANSITION", "ALREADY_IN_STATE"}
    with pytest.raises(AttentionContractError) as caught:
        record.transition(record.state, now_ms=T1)
    assert caught.value.code == "ALREADY_IN_STATE"


def test_transition_refuses_time_moving_backwards():
    record = work_item()
    with pytest.raises(AttentionContractError) as caught:
        record.transition(WorkItemState.PAUSED, now_ms=T0 - 1)
    assert caught.value.code == "TIME_REGRESSION"


def test_transition_refuses_time_before_the_last_update():
    updated = work_item().transition(WorkItemState.PAUSED, now_ms=T1)
    # Later than created_at but earlier than updated_at: only the explicit
    # check catches this (the dataclass validator would accept it).
    with pytest.raises(AttentionContractError) as caught:
        updated.transition(WorkItemState.ACTIVE, now_ms=T0 + 1)
    assert caught.value.code == "TIME_REGRESSION"
    watch_updated = watch().transition(WatchState.PAUSED, now_ms=T1)
    with pytest.raises(AttentionContractError) as caught:
        watch_updated.transition(WatchState.ACTIVE, now_ms=T0 + 1)
    assert caught.value.code == "TIME_REGRESSION"


def test_expiry_is_derived_from_the_window_and_never_persisted():
    record = watch()
    assert record.effective_state(EXPIRES - 1) is EffectiveWatchState.ACTIVE
    assert record.effective_state(EXPIRES) is EffectiveWatchState.EXPIRED
    assert record.to_dict()["state"] == "active"
    paused = record.transition(WatchState.PAUSED, now_ms=T1)
    assert paused.effective_state(EXPIRES + 5) is EffectiveWatchState.EXPIRED
    cancelled = record.transition(WatchState.CANCELLED, now_ms=T1)
    assert cancelled.effective_state(EXPIRES + 5) is EffectiveWatchState.CANCELLED


def test_resume_never_extends_the_window_and_refuses_after_expiry():
    paused = watch().transition(WatchState.PAUSED, now_ms=T1)
    resumed = paused.transition(WatchState.ACTIVE, now_ms=EXPIRES - 1)
    assert resumed.to_spec().expires_at_ms == EXPIRES
    with pytest.raises(AttentionContractError) as caught:
        paused.transition(WatchState.ACTIVE, now_ms=EXPIRES)
    assert caught.value.code == "WATCH_EXPIRED"
    # Cancelling an expired watch remains possible (cleanup).
    assert paused.transition(WatchState.CANCELLED, now_ms=EXPIRES + 1).revision == 3


def test_authored_fields_cannot_be_replaced_through_the_dataclass():
    record = work_item()
    with pytest.raises(AttributeError):
        record.goal = "rewritten by an event"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        watch().expires_at_ms = 1  # type: ignore[misc]
