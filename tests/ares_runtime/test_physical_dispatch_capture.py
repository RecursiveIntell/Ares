"""Opt-in per-run native-send evidence; no global plugin or credential changes."""
import json
from types import SimpleNamespace

import httpx
import pytest

from ares_runtime import physical_dispatch as physical
from hermes_cli import middleware


def client():
    return physical.bind_http_client(httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=httpx.ByteStream(b'{"model":"fixture","ok":true}'),
                                       headers={"content-type": "application/json"}))))


def send(sdk):
    return sdk.post("https://fixture.invalid/v1/responses", json={"model": "fixture", "reasoning": {"effort": "high"},
                    "input": "private-query", "max_output_tokens": 1})


def test_scoped_capture_has_native_fields_and_settlement_without_private_content():
    with physical.capture_physical_dispatch() as capture:
        with client() as sdk:
            send(sdk)
    rows = capture.snapshot()
    assert len(rows) == 1
    assert rows[0]["native_controls"]["reasoning"]["effort"] == "high"
    assert rows[0]["outcome"] == "ok"
    assert rows[0]["response"]["response_complete"] is True
    assert "private-query" not in json.dumps(rows)
    assert "materialization" not in rows[0]
    rows[0]["native_controls"]["reasoning"]["effort"] = "low"
    assert capture.snapshot()[0]["native_controls"]["reasoning"]["effort"] == "high"


def test_capture_is_context_local_and_nested_scope_restores_parent():
    with client() as sdk:
        with physical.capture_physical_dispatch() as outer:
            send(sdk)
            with physical.capture_physical_dispatch() as inner:
                send(sdk)
            send(sdk)
        send(sdk)
    assert len(outer.snapshot()) == 2
    assert len(inner.snapshot()) == 1


def test_denied_request_is_retained_without_claiming_it_was_sent(monkeypatch):
    def deny(**kw):
        raise middleware.EgressDispatchDenied("FIXTURE_DENIED")
    monkeypatch.setattr(middleware, "_get_middleware_callbacks", lambda _: [deny])
    with physical.capture_physical_dispatch() as capture:
        with client() as sdk:
            with pytest.raises(middleware.EgressDispatchDenied):
                send(sdk)
    row = capture.snapshot()[0]
    assert row["outcome"] == "denied_by_peer"
    assert row["response"] is None
    assert row["error_code"] == "FIXTURE_DENIED"


def test_deferred_body_settles_owned_capture_after_scope_exit():
    sdk = client()
    try:
        with physical.capture_physical_dispatch() as capture:
            request = sdk.build_request("POST", "https://fixture.invalid/v1/responses", json={"model": "fixture", "max_output_tokens": 1})
            response = sdk.send(request, stream=True)
        assert capture.snapshot()[0]["outcome"] is None
        response.read()
        response.close()
        assert capture.snapshot()[0]["outcome"] == "ok"
    finally:
        sdk.close()


def test_capture_cannot_replace_existing_governor_ticket(monkeypatch):
    outcomes = []
    monkeypatch.setattr(middleware, "_get_middleware_callbacks", lambda _: [lambda **kw: SimpleNamespace(
        settle=lambda **value: outcomes.append(value["outcome"]))])
    with physical.capture_physical_dispatch() as capture:
        with client() as sdk:
            send(sdk)
    assert outcomes == ["ok"]
    assert capture.snapshot()[0]["outcome"] == "ok"


def test_sdk_raw_response_model_is_observed_not_assumed_from_request():
    import openai
    payload = b'{"id":"fixture","object":"chat.completion","created":0,"model":"returned-other-model","choices":[{"index":0,"message":{"role":"assistant","content":"private-answer"},"finish_reason":"stop"}]}'
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=httpx.ByteStream(payload), headers={"content-type": "application/json"}))
    sdk = physical.bind_inference_client(openai.OpenAI(api_key="fixture-only", base_url="https://fixture.invalid/v1", max_retries=0,
                                                     http_client=httpx.Client(transport=transport)))
    try:
        with physical.capture_physical_dispatch() as capture:
            response = sdk.chat.completions.create(model="requested-model", messages=[], max_tokens=1)
        row = capture.snapshot()[0]
        assert row["resolved_route"]["model_ref"] == "requested-model"
        assert row["provider_response_model"] == response.model == "returned-other-model"
        assert "private-answer" not in json.dumps(row)
        assert "fixture-only" not in json.dumps(row)
    finally:
        sdk.close()


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_streamed_sdk_events_retain_actual_model_without_response_text(provider):
    if provider == "openai":
        import openai
        chunk = {"id": "fixture", "object": "chat.completion.chunk", "created": 0,
                 "model": "stream-returned-model", "choices": [{"index": 0, "delta": {"content": "private-answer"}, "finish_reason": "stop"}]}
        raw = ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
        factory = openai.OpenAI
    else:
        import anthropic
        start = {"type": "message_start", "message": {"id": "fixture", "type": "message", "role": "assistant",
                 "model": "stream-returned-model", "content": [], "stop_reason": None, "stop_sequence": None,
                 "usage": {"input_tokens": 1, "output_tokens": 0}}}
        raw = ("event: message_start\ndata: " + json.dumps(start) + "\n\nevent: message_stop\ndata: {\"type\":\"message_stop\"}\n\n").encode()
        factory = anthropic.Anthropic
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=httpx.ByteStream(raw),
                                      headers={"content-type": "text/event-stream"}))
    sdk = physical.bind_inference_client(factory(api_key="fixture-only", base_url="https://fixture.invalid/v1", max_retries=0,
                                                http_client=httpx.Client(transport=transport)))
    try:
        with physical.capture_physical_dispatch() as capture:
            if provider == "openai":
                stream = sdk.chat.completions.create(model="requested-model", messages=[], max_tokens=1, stream=True)
            else:
                stream = sdk.messages.create(model="requested-model", messages=[], max_tokens=1, stream=True)
            list(stream)
            stream.close()
        row = capture.snapshot()[0]
        assert row["provider_response_model"] == "stream-returned-model"
        # Per the sole-settlement-owner decision (2026-11-10): the SDK stream
        # iterator owns the outcome. At [DONE] the pinned SDK ends parsing
        # with an unread HTTP tail; the iterator settles this as an exhausted
        # SDK stream (completion_evidence=native_openai_sdk_stream_exhausted),
        # superseding the pre-iterator expectation of "abandoned" here.
        assert row["outcome"] == "ok"
        assert row["response"].get("completion_evidence") == (
            "native_openai_sdk_stream_exhausted" if provider == "openai" else None
        )
        assert "private-answer" not in json.dumps(row)
    finally:
        sdk.close()
