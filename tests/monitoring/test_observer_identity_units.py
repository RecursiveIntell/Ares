"""Behavioral witnesses for metadata units and exact event ownership."""

from __future__ import annotations
import json
import threading
import pytest
from hermes_cli.observability import stack_monitor as monitor


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({"api_duration": 1.234}, 1234),
        ({"api_duration": 0, "duration_ms": 999}, 0),
        ({"duration_ms": 432}, 432),
    ],
)
def test_duration_preserves_declared_unit_and_zero(fields, expected):
    event = monitor.build_envelope("post_api_request", fields, sequence=1)
    assert event["timing"]["duration_ms"] == expected


@pytest.mark.parametrize(
    "bad", [True, -1, float("inf"), float("nan"), "not-a-duration"]
)
def test_invalid_duration_does_not_become_measurement(bad):
    event = monitor.build_envelope(
        "post_api_request", {"api_duration": bad}, sequence=1
    )
    assert "duration_ms" not in event["timing"]


def test_provider_usage_is_not_chars_or_calls():
    event = monitor.build_envelope(
        "post_api_request",
        {
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            "approx_input_tokens": 99,
            "assistant_content_chars": 333,
            "api_call_count": 3,
        },
        sequence=1,
    )
    assert {
        key: event["timing"][key]
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    } == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    assert event["payload"]["assistant_content_chars"] == 333
    assert event["payload"]["api_call_count"] == 3
    assert event["payload"]["approx_input_tokens"] == 99


def test_missing_real_usage_stays_unknown():
    event = monitor.build_envelope(
        "post_api_request",
        {
            "approx_input_tokens": 99,
            "assistant_content_chars": 333,
            "api_call_count": 3,
        },
        sequence=1,
    )
    assert not {"prompt_tokens", "completion_tokens", "total_tokens"} & set(
        event["timing"]
    )


def test_reported_zero_tokens_are_not_missing():
    event = monitor.build_envelope(
        "post_api_request",
        {"usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}},
        sequence=1,
    )
    assert event["timing"]["total_tokens"] == 0


def test_finish_reason_is_not_an_error_category():
    event = monitor.build_envelope(
        "post_api_request", {"finish_reason": "stop"}, sequence=1
    )
    assert "error_category" not in event["timing"]
    assert event["payload"]["finish_reason"] == "stop"


class MemoryProducer:
    def __init__(self):
        self.events = []
        self.sequence = 0
        self.lock = threading.Lock()

    def next_sequence(self):
        with self.lock:
            self.sequence += 1
            return self.sequence

    def emit(self, event):
        with self.lock:
            self.events.append(event)

    def flush(self):
        pass


@pytest.fixture
def producer(monkeypatch):
    p = MemoryProducer()
    monkeypatch.setattr(monitor, "_producer", lambda: p)
    monitor._OPEN_EVENTS.clear()
    monitor._SESSION_COUNTS.clear()
    yield p
    monitor._OPEN_EVENTS.clear()
    monitor._SESSION_COUNTS.clear()


def gaps(producer):
    return [
        e for e in producer.events if e["payload"]["hook"] == "terminal_observation_gap"
    ]


def test_unmatched_session_end_cannot_close_another_session(producer):
    monitor.observe_lifecycle("pre_tool_call", session_id="B", tool_call_id="tool-B")
    monitor.observe_lifecycle("on_session_end", session_id="A")
    assert gaps(producer) == []
    monitor.observe_lifecycle("post_tool_call", session_id="B", tool_call_id="tool-B")
    monitor.observe_lifecycle("on_session_end", session_id="B")
    assert gaps(producer) == []


def test_unidentified_end_does_not_borrow_known_event(producer):
    monitor.observe_lifecycle("pre_api_request", session_id="B", api_request_id="req-B")
    monitor.observe_lifecycle("on_session_end")
    assert gaps(producer) == []
    monitor.observe_lifecycle("on_session_end", session_id="B")
    assert len(gaps(producer)) == 1
    assert gaps(producer)[0]["correlation"]["session_id"] == "B"
    assert gaps(producer)[0]["correlation"]["request_id"] == "req-B"


def test_end_is_scoped_to_explicit_run(producer):
    monitor.observe_lifecycle(
        "pre_tool_call",
        session_id="same-session",
        run_id="run-A",
        tool_call_id="same-request",
    )
    monitor.observe_lifecycle(
        "pre_tool_call",
        session_id="same-session",
        run_id="run-B",
        tool_call_id="same-request",
    )
    monitor.observe_lifecycle(
        "on_session_end", session_id="same-session", run_id="run-A"
    )
    assert len(gaps(producer)) == 1
    assert gaps(producer)[0]["correlation"]["run_id"] == "run-A"
    monitor.observe_lifecycle(
        "post_tool_call",
        session_id="same-session",
        run_id="run-B",
        tool_call_id="same-request",
    )
    monitor.observe_lifecycle(
        "on_session_end", session_id="same-session", run_id="run-B"
    )
    assert len(gaps(producer)) == 1
    end = producer.events[-1]
    assert end["payload"]["coverage"]["started_tool"] == 1
    assert end["payload"]["coverage"]["terminal_tool"] == 1


def test_content_and_secret_sentinels_do_not_enter_timing(producer):
    monitor.observe_lifecycle(
        "post_api_request",
        session_id="A",
        usage={"prompt_tokens": 4, "completion_tokens": 3},
        prompt="DO_NOT_SERIALIZE_PROMPT",
        response="DO_NOT_SERIALIZE_RESPONSE",
        api_key="DO_NOT_SERIALIZE_KEY",
    )
    data = json.dumps(producer.events)
    assert "DO_NOT_SERIALIZE" not in data
