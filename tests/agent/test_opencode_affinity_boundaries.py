"""Behavioral boundaries and SDK forwarding for OpenCode session affinity."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from openai import OpenAI

from agent import auxiliary_client as aux
from agent.opencode_affinity import (
    is_opencode_target,
    merge_opencode_session_headers,
    opencode_session_headers,
)
from agent.portal_tags import reset_conversation_context, set_conversation_context

URL = "https://opencode.ai/zen/go/v1"
MESSAGES = [{"role": "user", "content": "hi"}]
HEADER = "x-opencode-session"


@pytest.mark.parametrize("provider,url,expected", [
    ("opencode-go", None, True),
    ("opencode-zen", None, True),
    ("opencode-free", None, True),
    ("opencode-go-bridge", None, True),
    ("custom:opencode-go", URL, True),
    ("custom", URL, True),
    ("custom", "https://opencode.ai.evil.invalid/v1", False),
    ("custom", "https://opencode.ai@evil.invalid/v1", False),
    ("openai-codex", "https://chatgpt.com/backend-api/codex", False),
])
def test_target_detection_uses_provider_owner_and_real_url_host(provider, url, expected):
    assert is_opencode_target(provider, url) is expected


def test_distinct_sessions_root_precedence_and_no_global_fallback():
    assert opencode_session_headers("opencode-go", URL, "a") != opencode_session_headers("opencode-go", URL, "b")
    assert opencode_session_headers("opencode-go", URL, None) == {}
    token = set_conversation_context("lineage-root")
    try:
        assert opencode_session_headers("opencode-go", URL, "segment-1")[HEADER] == "lineage-root"
        assert opencode_session_headers("opencode-go", URL, "segment-2")[HEADER] == "lineage-root"
    finally:
        reset_conversation_context(token)


def test_existing_cron_scope_normalization_is_reused():
    first = opencode_session_headers("opencode-go", URL, "cron_job-a_20261014_010203")
    second = opencode_session_headers("opencode-go", URL, "cron_job-a_20261015_040506")
    assert first == second == {HEADER: "cron_job-a"}


def test_merge_preserves_explicit_header_and_other_fields():
    original = {"model": "m", "extra_headers": {HEADER: "pinned", "X-Trace": "trace"}}
    assert merge_opencode_session_headers(original, "opencode-go", URL, "generated") is original
    assert original["extra_headers"] == {HEADER: "pinned", "X-Trace": "trace"}
    unrelated = {"model": "m", "extra_headers": {"X-Trace": "trace"}}
    merge_opencode_session_headers(unrelated, "openai-codex", "https://chatgpt.com", "a")
    assert unrelated == {"model": "m", "extra_headers": {"X-Trace": "trace"}}


def test_concurrent_contexts_do_not_borrow_other_conversation_identity():
    barrier = Barrier(2)

    def worker(scope):
        token = set_conversation_context(scope)
        try:
            barrier.wait(timeout=5)
            return opencode_session_headers("opencode-go", URL, "physical")[HEADER]
        finally:
            reset_conversation_context(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker, "conversation-a")
        second = pool.submit(worker, "conversation-b")
        assert [first.result(), second.result()] == ["conversation-a", "conversation-b"]


def test_auxiliary_worker_uses_explicit_rotation_stable_scope():
    token = aux.set_runtime_main(
        "opencode-go", "glm-5", base_url=URL,
        session_id="physical-segment", cache_scope="lineage-root",
    )
    try:
        kwargs = aux._build_call_kwargs("opencode-go", "glm-5", MESSAGES, base_url=URL)
        assert kwargs["extra_headers"][HEADER] == "lineage-root"
    finally:
        aux.reset_runtime_main(token)


def test_chat_sdk_serializes_affinity_as_a_header_not_a_body_field():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={
            "id": "chat-test", "object": "chat.completion", "created": 0, "model": "glm-5",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        })

    with OpenAI(api_key="test-key", base_url=URL, http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        kwargs = merge_opencode_session_headers({"model": "glm-5", "messages": MESSAGES}, "opencode-go", URL, "sdk-session")
        assert client.chat.completions.create(**kwargs).choices[0].message.content == "ok"
    assert captured[0].headers[HEADER] == "sdk-session"
    assert HEADER.encode() not in captured[0].content


def test_codex_auxiliary_adapter_forwards_session_header_to_sdk():
    final = SimpleNamespace(output=[{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}], usage=None)
    create = Mock(return_value=final)
    sdk = SimpleNamespace(api_key="test-key", base_url=URL, responses=SimpleNamespace(create=create))
    client = aux.CodexAuxiliaryClient(sdk, "gpt-5.6-luna")
    kwargs = merge_opencode_session_headers({"model": "gpt-5.6-luna", "messages": MESSAGES}, "opencode-go", URL, "responses-session")
    assert client.chat.completions.create(**kwargs).choices[0].message.content == "ok"
    assert create.call_args.kwargs["extra_headers"][HEADER] == "responses-session"


def test_anthropic_auxiliary_adapter_forwards_session_header_to_sdk(monkeypatch):
    final = SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], stop_reason="end_turn", usage=None)
    create = Mock(return_value=final)
    monkeypatch.setattr("agent.anthropic_adapter.create_anthropic_message", create)
    sdk = SimpleNamespace()
    client = aux.AnthropicAuxiliaryClient(sdk, "minimax-m2.7", "test-key", URL)
    kwargs = merge_opencode_session_headers({"model": "minimax-m2.7", "messages": MESSAGES, "max_tokens": 16}, "opencode-go", URL, "messages-session")
    assert client.chat.completions.create(**kwargs).choices[0].message.content == "ok"
    assert create.call_args.args[1]["extra_headers"][HEADER] == "messages-session"
