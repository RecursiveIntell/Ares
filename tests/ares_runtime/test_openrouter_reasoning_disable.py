"""OpenRouter disable intent through the real SDK and physical-send boundary."""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
from openai import OpenAI
import pytest

from ares_runtime.physical_dispatch import (
    bind_http_client,
    explicit_reasoning_request_scope,
    inference_call_scope,
)
from hermes_cli.middleware import EgressDispatchDenied
from providers import get_provider_profile

MODEL = "anthropic/claude-sonnet-4.5"
DISABLED = {"enabled": False}


def _typed_denial(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, EgressDispatchDenied):
            return error
        error = error.__cause__ or error.__context__
    return None


def _send(extra=None, *, expected=None, responses=False):
    expected = deepcopy(DISABLED if expected is None else expected)
    profile = get_provider_profile("openrouter")
    adapter_body, adapter_top = profile.build_api_kwargs_extras(
        reasoning_config=deepcopy(DISABLED), supports_reasoning=True, model=MODEL
    )
    assert adapter_body["reasoning"] == DISABLED
    wire_extra = deepcopy(adapter_body if extra is None else extra)
    sent = []

    def transport(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "offline-disable", "object": "chat.completion", "created": 1,
            "model": MODEL,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "fixture"}, "finish_reason": "stop"}],
        })

    owner = SimpleNamespace(session_id="offline-openrouter-disable", model=MODEL,
                            provider="openrouter", api_mode="chat_completions")
    logical = {"model": MODEL, "max_tokens": 16, "messages": [{"role": "user", "content": "synthetic fixture"}], **wire_extra}
    error = None
    result = None
    with bind_http_client(httpx.Client(transport=httpx.MockTransport(transport), trust_env=False)) as client:
        sdk = OpenAI(api_key="synthetic-offline", base_url="https://fixture.invalid/v1", http_client=client, max_retries=0)
        try:
            with explicit_reasoning_request_scope(owner, expected), inference_call_scope(owner, logical):
                if responses:
                    result = sdk.responses.create(model=MODEL, input="synthetic fixture", max_output_tokens=16, extra_body=wire_extra)
                else:
                    result = sdk.chat.completions.create(model=MODEL, messages=logical["messages"], max_tokens=16,
                                                         extra_body=wire_extra, **adapter_top)
        except Exception as caught:
            error = caught
        finally:
            sdk.close()
    return result, sent, error


@pytest.mark.parametrize("representation", [None, {"reasoning": {"enabled": False, "effort": "none"}}])
def test_openrouter_native_boolean_disable_reaches_mock_transport_unchanged(representation):
    result, sent, error = _send(representation)
    assert error is None, repr(error)
    assert result.choices[0].message.content == "fixture"
    assert len(sent) == 1
    expected = DISABLED if representation is None else representation["reasoning"]
    assert sent[0]["reasoning"] == expected


@pytest.mark.parametrize("extra,expected,code", [
    ({"reasoning": {"enabled": False, "effort": "high"}}, DISABLED, "EXPLICIT_REASONING_MISMATCH"),
    ({"reasoning": {"enabled": False}, "reasoning_effort": "high"}, DISABLED, "EXPLICIT_REASONING_MISMATCH"),
    ({"reasoning": {}}, DISABLED, "EXPLICIT_REASONING_MISMATCH"),
    ({"reasoning": {"enabled": True, "effort": "none"}}, DISABLED, "EXPLICIT_REASONING_MISMATCH"),
    ({"reasoning": {"enabled": False}}, {"enabled": True, "effort": "high"}, "EXPLICIT_REASONING_MISMATCH"),
    ({"reasoning": {"enabled": 0}}, DISABLED, "EXPLICIT_REASONING_UNQUALIFIED"),
    ({"reasoning": {"enabled": "false"}}, DISABLED, "EXPLICIT_REASONING_UNQUALIFIED"),
])
def test_openrouter_disable_contradictions_and_ambiguity_make_no_send(extra, expected, code):
    _, sent, error = _send(extra, expected=expected)
    assert error is not None
    denial = _typed_denial(error)
    assert denial is not None, repr(error)
    assert denial.code == code
    assert sent == []


def test_boolean_only_disable_does_not_widen_responses_protocol():
    _, sent, error = _send(responses=True)
    assert error is not None
    denial = _typed_denial(error)
    assert denial is not None, repr(error)
    assert denial.code == "EXPLICIT_REASONING_MISMATCH"
    assert sent == []
