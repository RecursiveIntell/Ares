"""Review regressions: late headers, summary exits, and wire-safe identity."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from openai import OpenAI

from agent import auxiliary_client as aux
from agent.chat_completion_helpers import handle_max_iterations
from agent.opencode_affinity import opencode_session_headers
from agent.portal_tags import reset_conversation_context, set_conversation_context
from run_agent import AIAgent

URL = "https://opencode.ai/zen/go/v1"
HEADER = "x-opencode-session"
MSGS = [{"role": "user", "content": "hello"}]


def response(text="ok"):
    return SimpleNamespace(choices=[SimpleNamespace(
        index=0, message=SimpleNamespace(content=text, tool_calls=None, reasoning=None),
        finish_reason="stop")], usage=None, model="glm-5")


def test_call_llm_keeps_affinity_when_caller_supplies_tracing_headers(monkeypatch):
    create = Mock(return_value=response())
    client = SimpleNamespace(base_url=URL, chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(aux, "_get_cached_client", lambda *a, **k: (client, "glm-5"))
    token = set_conversation_context("header-session")
    try:
        aux.call_llm(provider="opencode-go", model="glm-5", base_url=URL,
                     messages=MSGS, extra_headers={"X-Trace": "trace"})
        headers = create.call_args.kwargs["extra_headers"]
        assert headers[HEADER] == opencode_session_headers("opencode-go", URL)[HEADER]
        assert headers["X-Trace"] == "trace"
    finally:
        reset_conversation_context(token)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_rebuilt_client_retry_merges_caller_headers(monkeypatch, asynchronous):
    create = AsyncMock(return_value=response()) if asynchronous else Mock(return_value=response())
    client = SimpleNamespace(base_url=URL, chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(aux, "_get_cached_client", lambda *a, **k: (client, "glm-5"))
    kwargs = dict(task=None, resolved_provider="opencode-go", resolved_model="glm-5",
                  resolved_base_url=URL, resolved_api_key=None, resolved_api_mode="chat_completions",
                  final_model="glm-5", messages=MSGS, temperature=None, max_tokens=16,
                  tools=None, effective_timeout=5, effective_extra_body={}, reasoning_config=None,
                  extra_headers={"X-Trace": "trace"})
    token = set_conversation_context("retry-session")
    try:
        if asynchronous:
            asyncio.run(aux._retry_same_provider_async(**kwargs))
        else:
            aux._retry_same_provider_sync(main_runtime=None, **kwargs)
        headers = create.call_args.kwargs["extra_headers"]
        assert headers[HEADER] == opencode_session_headers("opencode-go", URL)[HEADER]
        assert headers["X-Trace"] == "trace"
    finally:
        reset_conversation_context(token)


@pytest.mark.parametrize("api_mode", ["chat_completions", "anthropic_messages"])
def test_iteration_limit_summary_and_empty_response_retry_have_affinity(monkeypatch, api_mode):
    agent = AIAgent(api_key="test-key", provider="opencode-go", model="glm-5",
                    base_url=URL, session_id="summary-session", enabled_toolsets=[],
                    quiet_mode=True, skip_context_files=True, skip_memory=True,
                    max_iterations=1)
    agent.api_mode = api_mode
    agent._transport = None
    agent._anthropic_base_url = URL
    agent._cached_system_prompt = "You are helpful."
    agent.suppress_status_output = True
    if api_mode == "chat_completions":
        create = Mock(side_effect=[response(""), response("summary")])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        monkeypatch.setattr(agent, "_ensure_primary_openai_client", lambda **k: client)
    else:
        create = Mock(side_effect=[
            SimpleNamespace(content=[], stop_reason="end_turn", usage=None),
            SimpleNamespace(content=[SimpleNamespace(type="text", text="summary")], stop_reason="end_turn", usage=None),
        ])
        monkeypatch.setattr(agent, "_anthropic_messages_create", create)
    monkeypatch.setattr("agent.relay_llm.execute_current", lambda request, callback, **kw: callback(request))
    assert handle_max_iterations(agent, list(MSGS), 1) == "summary"
    assert create.call_count == 2
    expected = opencode_session_headers("opencode-go", URL, "summary-session")[HEADER]
    requests = [call.kwargs if api_mode == "chat_completions" else call.args[0]
                for call in create.call_args_list]
    assert all(request["extra_headers"][HEADER] == expected for request in requests)


@pytest.mark.parametrize("session_id", ["会話-1", "x" * 5000, "line\nbreak"], ids=["unicode", "long", "control"])
def test_session_identity_is_encoded_as_bounded_ascii_on_real_sdk_wire(session_id):
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
            "model": "glm-5", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})

    headers = opencode_session_headers("opencode-go", URL, session_id)
    value = headers[HEADER]
    assert value.isascii() and len(value) <= 128
    assert all(32 < ord(char) < 127 for char in value)
    with OpenAI(api_key="test-key", base_url=URL, http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        assert client.chat.completions.create(model="glm-5", messages=MSGS, extra_headers=headers).choices[0].message.content == "ok"
    assert captured[0].headers[HEADER] == value
    assert headers == opencode_session_headers("opencode-go", URL, session_id)
