"""Real caller + transport/Relay/SessionDB; SDK effects are inert captures.

Responses uses the actual installed OpenAI SDK with httpx.MockTransport.
Its serialized JSON body, not a test reimplementation of extra_body merge,
is the receipt witness. Bedrock's capturing client never calls boto3/native.
"""
from copy import deepcopy
from collections import Counter
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from openai import OpenAI

from ares_runtime.continuity.runtime import context_dispatch_payload_digest
from tests.agent.test_context_dispatch_relay_boundary import (
    _Backend, _managed, _records, agent, durable_agent,  # noqa: F401
)


def _run_route(item, prompt="Current correction", history=None):
    with (patch.object(item, "_save_trajectory"), patch.object(item, "_cleanup_task_resources"),
          patch("agent.title_generator.maybe_auto_title"),
          patch.object(item, "_close_request_openai_client")):
        return item.run_conversation(prompt, conversation_history=history)


def _codex(item, monkeypatch, *, outer_streaming, sdk_transform=False, sealed=True):
    item.api_mode, item.provider = "codex_responses", "openai-codex"
    item.base_url = "https://chatgpt.com/backend-api/codex"
    item.tools = []
    item.context_rebase_enabled = sealed
    item.context_compressor.context_length = 40_000
    item._disable_streaming = not outer_streaming
    monkeypatch.setattr(item, "_has_stream_consumers", lambda: outer_streaming)
    monkeypatch.setenv("HERMES_CODEX_SDK_TRANSFORM", "1" if sdk_transform else "0")
    wire, kwargs = [], []
    def respond(request):
        wire.append(json.loads(request.content))
        response = {"id": "inert-response", "object": "response", "created_at": 1,
            "model": item.model, "status": "completed", "output": [{"id": "inert-message",
            "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "Done", "annotations": []}]}],
            "usage": {"input_tokens": 11, "output_tokens": 3, "total_tokens": 14,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0}}}
        events = [
            {"type": "response.output_item.done", "output_index": 0, "item": response["output"][0]},
            {"type": "response.completed", "response": response},
        ]
        data = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)
    sdk = OpenAI(api_key="inert-key", base_url="https://inert.invalid/v1",
        max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(respond)))
    original = sdk.responses.create
    def create(**payload):
        kwargs.append(deepcopy(payload))
        return original(**payload)
    monkeypatch.setattr(sdk.responses, "create", create)
    monkeypatch.setattr(item, "_create_request_openai_client", lambda **kw: sdk)
    return SimpleNamespace(wire=wire, kwargs=kwargs, client=sdk)


def _assert_accepted(item, db, result, body, *, admissions=1, spent=1):
    assert result.get("error") is None, result
    assert result["final_response"] == "Done"
    assert item.iteration_budget.used == spent
    assert item.session_api_calls == 1
    assert item.session_input_tokens == 11
    assert item.session_output_tokens == 3
    assert item._last_turn_usage["input_tokens"] == 11
    assert item._last_turn_usage["output_tokens"] == 3
    admitted = _records(db, "context-dispatch:")
    assert len(admitted) == admissions
    assert admitted[-1]["payload_digest"] == context_dispatch_payload_digest(body)
    assert _records(db, "context-dispatch-result:")[-1]["disposition"] == "response_received"
    assert db.read_context_input_work(item.session_id)["phase"]["state"] == "answered"


@pytest.mark.parametrize("outer_streaming", [False, True])
@pytest.mark.parametrize("sdk_transform", [False, True])
def test_codex_real_caller_final_sdk_body_matches_admission_and_usage(
        durable_agent, monkeypatch, outer_streaming, sdk_transform):
    item, db = durable_agent
    capture = _codex(item, monkeypatch, outer_streaming=outer_streaming, sdk_transform=sdk_transform)
    source = []
    _managed(monkeypatch, item.session_id, lambda body: source.append(deepcopy(body)) or body)
    try:
        result = _run_route(item)
        assert len(capture.wire) == len(capture.kwargs) == 1
        assert capture.wire[0]["stream"] is True
        assert ("input" in capture.kwargs[0]) is sdk_transform
        assert ("input" in capture.kwargs[0].get("extra_body", {})) is not sdk_transform
        # The non-stream outer dispatch and Codex's canonical stream both
        # intercept Relay; only the final SDK factory spends admission.
        assert len(source) == (1 if outer_streaming else 2)
        _assert_accepted(item, db, result, capture.wire[0])
    finally:
        capture.client.close()


@pytest.mark.parametrize("outer_streaming", [False, True])
@pytest.mark.parametrize("sdk_transform", [False, True])
@pytest.mark.parametrize("mutation", [
    "input", "oversize", "model", "reserve", "retained", "wire_override", "hidden_marker",
])
def test_codex_real_caller_rejects_late_relay_mutation_before_sdk(
        durable_agent, monkeypatch, outer_streaming, sdk_transform, mutation):
    item, db = durable_agent
    capture = _codex(item, monkeypatch, outer_streaming=outer_streaming, sdk_transform=sdk_transform)
    def rewrite(body):
        if mutation == "input":
            body["input"] = [{"role": "user", "content": [{"type": "input_text", "text": "Redirected"}]}]
        elif mutation == "oversize":
            body["input"].append({"role": "user", "content": [{"type": "input_text", "text": "X" * 80_000}]})
        elif mutation == "model":
            body["model"] = "other-model"
        elif mutation == "reserve":
            body["max_output_tokens"] = 39_999
        elif mutation == "retained":
            body["previous_response_id"] = "opaque-prior-response"
        elif mutation == "wire_override":
            body["extra_body"] = {"input": []}
        else:
            body["__bedrock_region__"] = "other-region"
        return body
    backend = _managed(monkeypatch, item.session_id, rewrite)
    expected = {"input": "CONTEXT_DISPATCH_MATERIALIZATION_CHANGED",
        "oversize": "CONTEXT_DISPATCH_FINAL_PAYLOAD_TOO_LARGE",
        "model": "CONTEXT_DISPATCH_ROUTE_CHANGED",
        "reserve": "CONTEXT_DISPATCH_FINAL_PAYLOAD_TOO_LARGE",
        "retained": "FINAL_PAYLOAD_OPAQUE_ACCOUNTING_UNQUALIFIED",
        "wire_override": "CONTEXT_DISPATCH_WIRE_OVERRIDE_UNQUALIFIED",
        "hidden_marker": "CONTEXT_DISPATCH_MATERIALIZATION_CHANGED"}
    try:
        result = _run_route(item)
        assert capture.kwargs == capture.wire == []
        assert result.get("error") == expected[mutation], result
        assert backend.calls == 1
        assert _records(db, "context-dispatch:") == []
        assert item.iteration_budget.used == 0
        assert item._api_call_count == item.session_api_calls == 0
        assert item._last_turn_usage is None
        assert item._context_response_admission is None
        assert not any(row["role"] in {"assistant", "tool"} for row in db.get_messages(item.session_id))
    finally:
        capture.client.close()


@pytest.mark.parametrize("sdk_transform", [False, True])
def test_codex_sdk_bypass_restores_tool_schema_positions_before_receipt(durable_agent, monkeypatch, sdk_transform):
    item, db = durable_agent
    capture = _codex(item, monkeypatch, outer_streaming=True, sdk_transform=sdk_transform)
    item.tools = [{"type": "function", "function": {"name": "inert_schema",
        "description": "No execution is requested", "parameters": {"type": "object",
            "properties": {"previous_response_id": {"type": "string"},
                "conversation": {"type": "string"}}, "additionalProperties": False}}}]
    _managed(monkeypatch, item.session_id, lambda body: body)
    try:
        result = _run_route(item)
        assert len(capture.wire) == 1
        assert set(capture.wire[0]["tools"][0]["parameters"]["properties"]) == {
            "previous_response_id", "conversation"}
        _assert_accepted(item, db, result, capture.wire[0])
    finally:
        capture.client.close()


@pytest.mark.parametrize("sdk_transform", [False, True])
def test_codex_real_caller_lawful_normalization_and_sdk_body_merge(durable_agent, monkeypatch, sdk_transform):
    item, db = durable_agent
    capture = _codex(item, monkeypatch, outer_streaming=True, sdk_transform=sdk_transform)
    item.request_overrides = {"prompt_cache_retention": "24h", "max_output_tokens": 1000.0,
        "service_tier": " priority ", "extra_body": {"prompt_cache_retention": "24h",
            "reasoning": {"summary": "auto"}}}
    original = []
    _managed(monkeypatch, item.session_id, lambda body: original.append(deepcopy(body)) or body)
    try:
        result = _run_route(item)
        assert len(capture.wire) == 1
        assert "prompt_cache_retention" in original[0]
        assert "prompt_cache_retention" not in capture.wire[0]
        assert capture.wire[0]["max_output_tokens"] == 1000
        assert capture.wire[0]["service_tier"] == "priority"
        assert "effort" in original[0]["reasoning"]
        # The installed SDK's own shallow merge preserves explicit override
        # precedence; the extra_body reasoning object replaces this field.
        assert capture.wire[0]["reasoning"] == {"summary": "auto"}
        _assert_accepted(item, db, result, capture.wire[0])
    finally:
        capture.client.close()


@pytest.mark.parametrize("outer_streaming", [False, True])
def test_codex_ordinary_no_seal_retains_supported_relay_and_sanitizer_behavior(
        durable_agent, monkeypatch, outer_streaming):
    item, db = durable_agent
    capture = _codex(item, monkeypatch, outer_streaming=outer_streaming, sealed=False)
    _managed(monkeypatch, item.session_id, lambda body: {**body, "prompt_cache_retention": "24h"})
    try:
        result = _run_route(item)
        assert result.get("error") is None, result
        assert result["final_response"] == "Done"
        assert len(capture.wire) == 1 and "prompt_cache_retention" not in capture.wire[0]
        assert _records(db, "context-dispatch:") == []
        assert item.session_input_tokens == 11 and item.session_output_tokens == 3
    finally:
        capture.client.close()


def _bedrock(item, monkeypatch, *, deny_stream=False):
    item.api_mode, item.provider, item.max_tokens = "bedrock_converse", "bedrock", None
    item.tools = []
    calls = []
    def answer():
        return {"output": {"message": {"role": "assistant", "content": [{"text": "Done"}]}},
            "stopReason": "end_turn", "usage": {"inputTokens": 11, "outputTokens": 3, "totalTokens": 14}}
    class Client:
        def converse(self, **kwargs):
            calls.append(("converse", deepcopy(kwargs)))
            return answer()
        def converse_stream(self, **kwargs):
            calls.append(("converse_stream", deepcopy(kwargs)))
            if deny_stream:
                raise RuntimeError("not authorized: bedrock:InvokeModelWithResponseStream")
            return {"stream": iter([{"messageStart": {"role": "assistant"}},
                {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Done"}}},
                {"messageStop": {"stopReason": "end_turn"}},
                {"metadata": {"usage": {"inputTokens": 11, "outputTokens": 3, "totalTokens": 14}}}])}
    monkeypatch.setattr("agent.bedrock_adapter._get_bedrock_runtime_client", lambda region: Client())
    return calls


def test_bedrock_real_nonstream_caller_final_sdk_body_matches_admission(durable_agent, monkeypatch):
    item, db = durable_agent
    calls = _bedrock(item, monkeypatch)
    item._disable_streaming = True
    source = []
    _managed(monkeypatch, item.session_id, lambda body: source.append(deepcopy(body)) or body)
    result = _run_route(item)
    assert [route for route, body in calls] == ["converse"]
    assert source[0]["__bedrock_converse__"] is True
    assert "__bedrock_region__" in source[0]
    assert not any(name.startswith("__bedrock_") for name in calls[0][1])
    assert calls[0][1]["inferenceConfig"]["maxTokens"] == 4096
    _assert_accepted(item, db, result, calls[0][1])


@pytest.mark.parametrize("marker", ["__bedrock_region__", "__bedrock_converse__"])
def test_bedrock_real_nonstream_caller_rejects_late_hidden_marker_mutation(durable_agent, monkeypatch, marker):
    item, db = durable_agent
    calls = _bedrock(item, monkeypatch)
    item._disable_streaming = True
    _managed(monkeypatch, item.session_id, lambda body: {**body, marker: "mutated"})
    result = _run_route(item)
    assert result.get("error") == "CONTEXT_DISPATCH_MATERIALIZATION_CHANGED", result
    assert calls == []
    assert _records(db, "context-dispatch:") == []
    assert item.iteration_budget.used == 0


def test_bedrock_later_disabled_call_after_iam_fallback_has_exact_new_receipt(durable_agent, monkeypatch):
    item, db = durable_agent
    calls = _bedrock(item, monkeypatch, deny_stream=True)
    item._disable_streaming = False
    monkeypatch.setattr(item, "_has_stream_consumers", lambda: True)
    _managed(monkeypatch, item.session_id, lambda body: body)
    first = _run_route(item, "First correction")
    assert first.get("error") is None, first
    assert item._disable_streaming is True
    assert [route for route, body in calls] == ["converse_stream", "converse"]
    assert len(_records(db, "context-dispatch:")) == 2
    assert [row["disposition"] for row in _records(db, "context-dispatch-result:")] == [
        "response_discarded", "response_received"]
    assert item.iteration_budget.used == 1
    second = _run_route(item, "Second correction", first["messages"])
    assert second.get("error") is None, second
    assert second["final_response"] == "Done"
    assert [route for route, body in calls] == ["converse_stream", "converse", "converse"]
    assert len(_records(db, "context-dispatch:")) == 3
    # Receipt keys contain turn identities, so lexicographic order is not
    # chronological across turns. Match every physical body with multiplicity.
    assert Counter(row["payload_digest"] for row in _records(db, "context-dispatch:")) == Counter(
        context_dispatch_payload_digest(body) for _, body in calls)
    assert item.session_api_calls == 2 and item.session_input_tokens == 22 and item.session_output_tokens == 6
    assert db.read_context_input_work(item.session_id)["phase"]["state"] == "answered"
