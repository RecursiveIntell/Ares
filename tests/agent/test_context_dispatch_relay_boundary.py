"""Real caller/Relay adapter/continuity owner with an inert backend protocol.

The backend stands in for the optional managed Relay service. Its codec is
absent, so these tests qualify adapter composition, not native codec/ABI.
Provider SDK effects are capturing doubles; no live provider is used.
"""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import relay_runtime
from ares_runtime.continuity.runtime import context_dispatch_payload_digest
from tests.ares_runtime.test_continuity_dispatch import durable_agent  # noqa: F401
from tests.run_agent.test_run_agent import _mock_response


@pytest.fixture
def agent():
    from run_agent import AIAgent
    with (patch("run_agent.get_tool_definitions", return_value=[]),
          patch("run_agent.check_toolset_requirements", return_value={}),
          patch("hermes_cli.config.load_config", return_value={"model": {"context_length": 256_000}}),
          patch("agent.model_metadata._query_ollama_api_show", return_value=None),
          patch("run_agent.OpenAI")):
        value = AIAgent(model="test-model", provider="openai", api_mode="chat_completions",
            api_key="test-key", base_url="https://api.example.invalid", quiet_mode=True,
            skip_context_files=True, skip_memory=True)
        value.client = MagicMock()
        yield value


class _Backend:
    def __init__(self, rewrite):
        self.rewrite = rewrite
        self.calls = 0
        self.relay = SimpleNamespace(LLMRequest=lambda headers, content: SimpleNamespace(
            headers=headers, content=content), llm=self)

    def managed_execution_enabled(self):
        return True

    async def run_in_session_async(self, session, operation, *args, **kwargs):
        return await operation(*args, **kwargs)

    async def execute(self, name, request, callback, **kwargs):
        self.calls += 1
        return callback(SimpleNamespace(headers=request.headers,
            content=self.rewrite(deepcopy(request.content))))

    def acquire_operation_lease(self):
        return SimpleNamespace(release=lambda: None)

    async def stream_execute(self, name, request, callback, observe, finalize, **kwargs):
        self.calls += 1
        async def generate():
            async for chunk in callback(SimpleNamespace(headers=request.headers,
                    content=self.rewrite(deepcopy(request.content)))):
                observe(chunk)
                yield chunk
            finalize()
        return generate()


def _managed(monkeypatch, session_id, rewrite):
    backend = _Backend(rewrite)
    session = SimpleNamespace(session_id=session_id)
    monkeypatch.setattr(relay_runtime, "resolve_execution_context", lambda _sid: (backend, session, None))
    return backend


def _records(db, prefix):
    rows = db._conn.execute("SELECT value FROM state_meta WHERE key LIKE ? ORDER BY key", (prefix + "%",)).fetchall()
    return [json.loads(row[0]) for row in rows]


def _run(agent):
    agent.model = "test-model"
    agent.provider = "test"
    agent.base_url = "https://api.example.invalid"
    with (patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"),
          patch("agent.title_generator.maybe_auto_title"),
          patch.object(agent, "_create_request_openai_client", side_effect=lambda **kwargs: agent.client),
          patch.object(agent, "_close_request_openai_client")):
        return agent.run_conversation("Current correction")


@pytest.mark.parametrize("mutation", ["oversize", "opaque", "changed_source", "route", "wire_override"])
@pytest.mark.parametrize("streaming", [False, True])
def test_real_caller_refuses_post_relay_source_mutation(durable_agent, monkeypatch, mutation, streaming):
    agent, db = durable_agent
    agent.tools = []
    agent.context_compressor.context_length = 40_000
    agent._disable_streaming = not streaming
    monkeypatch.setattr(agent, "_has_stream_consumers", lambda: streaming)
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **kwargs: agent.client)
    def rewrite(body):
        if mutation == "oversize":
            body["messages"].append({"role": "user", "content": "X" * 80_000})
        elif mutation == "opaque":
            body["previous_response_id"] = "retained-state"
        elif mutation == "changed_source":
            body["messages"][-1]["content"] = "A different instruction"
        elif mutation == "route":
            body["model"] = "other-model"
        else:
            body["extra_body"] = {"messages": []}
        return body
    backend = _managed(monkeypatch, agent.session_id, rewrite)
    agent.client.chat.completions.create.return_value = _mock_response(content="Forbidden", finish_reason="stop")
    result = _run(agent)
    expected = {"oversize": "CONTEXT_DISPATCH_FINAL_PAYLOAD_TOO_LARGE",
        "opaque": "FINAL_PAYLOAD_OPAQUE_ACCOUNTING_UNQUALIFIED",
        "changed_source": "CONTEXT_DISPATCH_MATERIALIZATION_CHANGED",
        "route": "CONTEXT_DISPATCH_ROUTE_CHANGED",
        "wire_override": "CONTEXT_DISPATCH_WIRE_OVERRIDE_UNQUALIFIED"}
    assert result.get("error") == expected[mutation], result
    assert backend.calls == 1
    agent.client.chat.completions.create.assert_not_called()
    assert _records(db, "context-dispatch:") == []
    assert agent._context_response_admission is None


@pytest.mark.parametrize("sealed", [False, True])
def test_real_caller_preserves_unchanged_request_and_no_seal_behavior(durable_agent, monkeypatch, sealed):
    agent, db = durable_agent
    agent.tools = []
    agent.context_rebase_enabled = sealed
    agent._disable_streaming = True
    _managed(monkeypatch, agent.session_id, lambda body: body if sealed else {**body, "temperature": 0.25})
    agent.client.chat.completions.create.return_value = _mock_response(content="Done", finish_reason="stop")
    result = _run(agent)
    assert result.get("error") is None, result
    assert agent.client.chat.completions.create.call_count == 1
    admissions = _records(db, "context-dispatch:")
    assert len(admissions) == int(sealed)
    if sealed:
        assert admissions[0]["payload_digest"] == context_dispatch_payload_digest(
            agent.client.chat.completions.create.call_args.kwargs)
        assert _records(db, "context-dispatch-result:")[0]["disposition"] == "response_received"
    else:
        assert agent.client.chat.completions.create.call_args.kwargs["temperature"] == 0.25


def test_physical_transport_derivation_cannot_replace_source_and_sdk_gets_isolated_body(tmp_path):
    from ares_runtime.continuity.runtime import (
        ContextDispatchError, ContextDispatchPhysicalScope, context_dispatch_route_identity,
    )
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "body.db")
    try:
        db.create_session("s", source="cli")
        db.append_message("s", "user", "hi")
        assert db.try_acquire_session_turn_lease("s", "holder", ttl_seconds=300)
        agent = SimpleNamespace(provider="test", api_mode="chat_completions", model="m", base_url="",
            client=object(), max_tokens=1000, context_compressor=SimpleNamespace(context_length=40_000),
            _session_db=db, session_id="s", _active_session_turn_lease_holder="holder")
        source = {"model": "m", "max_tokens": 1000, "messages": [{"role": "user", "content": "hi"}]}
        scope = ContextDispatchPhysicalScope(agent, db.read_context_rebase_snapshot("s"), attempt_id="body",
            materialization_digest=context_dispatch_payload_digest(source),
            route_identity=context_dispatch_route_identity(agent))
        with scope:
            replaced = {**source, "stream": True, "stream_options": {"include_usage": True},
                "messages": [{"role": "user", "content": "unapproved"}]}
            with pytest.raises(ContextDispatchError, match="CONTEXT_DISPATCH_MATERIALIZATION_CHANGED"):
                scope.call(replaced, lambda final: pytest.fail("must not dispatch"),
                    source_payload=source, transport_kind="chat_stream")
            assert _records(db, "context-dispatch:") == []
            expected_digest = context_dispatch_payload_digest(source)
            def effect(final):
                source["messages"][0]["content"] = "mutated original alias"
                assert final["messages"][0]["content"] == "hi"
                return final
            sent = scope.call(source, effect)
            scope.settle()
        assert context_dispatch_payload_digest(sent) == expected_digest
        assert scope.admission["payload_digest"] == expected_digest
    finally:
        db.close()


def test_real_stream_factory_seals_final_kwargs_before_buffered_delivery(durable_agent, monkeypatch):
    agent, db = durable_agent
    agent.tools = []
    agent._disable_streaming = False
    monkeypatch.setattr(agent, "_has_stream_consumers", lambda: True)
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **kwargs: agent.client)
    monkeypatch.setattr(agent, "_close_request_openai_client", lambda *args, **kwargs: None)
    _managed(monkeypatch, agent.session_id, lambda body: body)
    chunk = SimpleNamespace(id="chunk", model=agent.model, usage=None, choices=[SimpleNamespace(
        index=0, finish_reason="stop", delta=SimpleNamespace(role="assistant", content="Done",
            tool_calls=None, reasoning_content=None, reasoning=None))])
    delivered = []
    def observe(text):
        assert agent._context_response_admission is not None
        assert _records(db, "context-dispatch-result:")[0]["disposition"] == "response_received"
        delivered.append(text)
    agent.stream_delta_callback = observe
    agent.client.chat.completions.create.return_value = iter([chunk])
    result = _run(agent)
    assert result.get("error") is None, result
    assert delivered, "The canonical stream consumer must actually run"
    sent = agent.client.chat.completions.create.call_args.kwargs
    assert sent["stream"] is True
    assert _records(db, "context-dispatch:")[0]["payload_digest"] == context_dispatch_payload_digest(sent)


def test_physical_retry_custody_discards_old_buffer_and_preserves_spent_attempts(tmp_path):
    from ares_runtime.continuity.runtime import (
        ContextDispatchPhysicalScope, ContextDispatchStreamBuffer, context_dispatch_route_identity,
    )
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "retry.db")
    try:
        db.create_session("s", source="cli")
        db.append_message("s", "user", "hi")
        assert db.try_acquire_session_turn_lease("s", "holder", ttl_seconds=300)
        events = []
        agent = SimpleNamespace(provider="test", api_mode="chat_completions", model="m", base_url="",
            client=object(), max_tokens=1000, context_compressor=SimpleNamespace(context_length=40_000),
            _session_db=db, session_id="s", _active_session_turn_lease_holder="holder",
            stream_delta_callback=events.append)
        source = {"model": "m", "max_tokens": 1000, "messages": [{"role": "user", "content": "hi"}]}
        scope = ContextDispatchPhysicalScope(agent, db.read_context_rebase_snapshot("s"), attempt_id="a",
            materialization_digest=context_dispatch_payload_digest(source),
            route_identity=context_dispatch_route_identity(agent))
        with scope:
            with ContextDispatchStreamBuffer(agent, scope) as delivery:
                scope.call(source, lambda final: None)
                agent.stream_delta_callback("old failed stream")
                scope.call(source, lambda final: None)
                agent.stream_delta_callback("accepted stream")
            assert events == []
            scope.settle()
            delivery.deliver()
        assert events == ["accepted stream"]
        assert [r["disposition"] for r in _records(db, "context-dispatch-result:")] == [
            "response_discarded", "response_received"]
        assert len(_records(db, "context-dispatch:")) == 2
    finally:
        db.close()


@pytest.mark.parametrize("denied_stream", [False, True])
def test_real_bedrock_factory_and_iam_fallback_have_exact_physical_receipts(durable_agent, monkeypatch, denied_stream):
    from agent.chat_completion_helpers import build_api_kwargs
    from ares_runtime.continuity.runtime import ContextDispatchPhysicalScope, context_dispatch_route_identity
    agent, db = durable_agent
    agent.api_mode, agent.provider, agent.max_tokens = "bedrock_converse", "bedrock", None
    db.append_message(agent.session_id, "user", "hi")
    assert db.try_acquire_session_turn_lease(agent.session_id, "fixture", ttl_seconds=300)
    agent._active_session_turn_lease_holder = "fixture"
    monkeypatch.setattr(agent, "_has_stream_consumers", lambda: False)
    sent = []
    class Client:
        def converse_stream(self, **kwargs):
            sent.append(deepcopy(kwargs))
            if denied_stream:
                raise RuntimeError("not authorized: bedrock:InvokeModelWithResponseStream")
            return {"stream": iter([{"messageStart": {"role": "assistant"}},
                {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Done"}}},
                {"messageStop": {"stopReason": "end_turn"}},
                {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}}}])}
        def converse(self, **kwargs):
            sent.append(deepcopy(kwargs))
            return {"output": {"message": {"role": "assistant", "content": [{"text": "Done"}]}},
                "stopReason": "end_turn", "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}}
    monkeypatch.setattr("agent.bedrock_adapter._get_bedrock_runtime_client", lambda region: Client())
    _managed(monkeypatch, agent.session_id, lambda body: body)
    source = build_api_kwargs(agent, [{"role": "user", "content": "hi"}], [])
    scope = ContextDispatchPhysicalScope(agent, db.read_context_rebase_snapshot(agent.session_id), attempt_id="bedrock",
        materialization_digest=context_dispatch_payload_digest(source),
        route_identity=context_dispatch_route_identity(agent))
    with scope:
        response = agent._interruptible_streaming_api_call(source)
        scope.settle()
    assert response.choices[0].message.content == "Done"
    admissions = _records(db, "context-dispatch:")
    assert len(admissions) == len(sent) == (2 if denied_stream else 1)
    assert all(a["payload_digest"] == context_dispatch_payload_digest(body) for a, body in zip(admissions, sent))
    assert all("__bedrock_region__" not in body and "__bedrock_converse__" not in body for body in sent)
    dispositions = [r["disposition"] for r in _records(db, "context-dispatch-result:")]
    assert dispositions == (["response_discarded", "response_received"] if denied_stream else ["response_received"])


def test_real_anthropic_factory_seals_sanitized_payload(durable_agent, monkeypatch):
    from agent.chat_completion_helpers import build_api_kwargs
    from ares_runtime.continuity.runtime import ContextDispatchPhysicalScope, context_dispatch_route_identity
    agent, db = durable_agent
    agent.api_mode, agent.provider = "anthropic_messages", "anthropic"
    db.append_message(agent.session_id, "user", "hi")
    assert db.try_acquire_session_turn_lease(agent.session_id, "fixture", ttl_seconds=300)
    agent._active_session_turn_lease_holder = "fixture"
    monkeypatch.setattr(agent, "_has_stream_consumers", lambda: False)
    sent = []
    class Stream:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def close(self):
            return None
        def get_final_message(self):
            return SimpleNamespace(id="m", role="assistant", model=agent.model,
                content=[SimpleNamespace(type="text", text="Done")], stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                    cache_creation_input_tokens=0, cache_read_input_tokens=0))
        def __iter__(self):
            return iter([SimpleNamespace(type="message_start", message=SimpleNamespace(
                    id="m", role="assistant", model=agent.model, usage={"input_tokens": 1})),
                SimpleNamespace(type="content_block_start", index=0,
                    content_block=SimpleNamespace(type="text", text="")),
                SimpleNamespace(type="content_block_delta", index=0,
                    delta=SimpleNamespace(type="text_delta", text="Done")),
                SimpleNamespace(type="message_delta", delta=SimpleNamespace(stop_reason="end_turn"),
                    usage={"output_tokens": 1})])
    def open_stream(**kwargs):
        sent.append(deepcopy(kwargs))
        return Stream()
    client = SimpleNamespace(messages=SimpleNamespace(stream=open_stream))
    monkeypatch.setattr(agent, "_create_request_anthropic_client", lambda **kwargs: client)
    monkeypatch.setattr(agent, "_close_request_anthropic_client", lambda *args, **kwargs: None)
    _managed(monkeypatch, agent.session_id, lambda body: body)
    source = build_api_kwargs(agent, [{"role": "user", "content": "hi"}], [])
    scope = ContextDispatchPhysicalScope(agent, db.read_context_rebase_snapshot(agent.session_id), attempt_id="anthropic",
        materialization_digest=context_dispatch_payload_digest(source),
        route_identity=context_dispatch_route_identity(agent))
    with scope:
        response = agent._interruptible_streaming_api_call(source)
        scope.settle()
    assert response.content[0].text == "Done"
    assert len(sent) == 1
    assert scope.admission["payload_digest"] == context_dispatch_payload_digest(sent[0])
