"""Real host gates with actual Governor pressure state; SDK and compaction effects inert."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugins.context_engine._context_governor import ContextGovernorEngine
from run_agent import AIAgent


def response(prompt=None, tool=False, index=0):
    calls = [SimpleNamespace(id=f"call-{index}", type="function", function=SimpleNamespace(
        name="lookup", arguments=json.dumps({"query": f"step-{index}"})))] if tool else None
    msg = SimpleNamespace(content=None if tool else "done", reasoning_content=None,
                          reasoning=None, tool_calls=calls)
    usage = None if prompt is None else SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=1, total_tokens=prompt + 1)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg,
        finish_reason="tool_calls" if tool else "stop")], model="test-model", usage=usage)


@pytest.fixture
def agent(tmp_path):
    tools = [{"type": "function", "function": {"name": "lookup",
        "description": "inert lookup", "parameters": {"type": "object",
        "properties": {"conversation": {"type": "string"}}}}}]
    with (patch("run_agent.get_tool_definitions", return_value=tools),
          patch("run_agent.check_toolset_requirements", return_value={}),
          patch("tools.env_probe.warm_environment_probe_async"),
          patch("run_agent.OpenAI"), patch("hermes_cli.config.load_config", return_value={})):
        value = AIAgent(api_key="test-key-1234567890", base_url="https://example.invalid",
                        quiet_mode=True, skip_context_files=True, skip_memory=True,
                        max_iterations=20)
        engine = ContextGovernorEngine(binary=str(tmp_path / "no-native"),
                                       store_dir=str(tmp_path / "governor"))
    engine.update_model("test-model", 100_000, max_tokens=10_000,
                        provider="test", api_mode="chat_completions", threshold_percent=0.5)
    engine.last_real_prompt_tokens = 20_000
    engine.last_rough_tokens_when_real_prompt_fit = 46_000
    engine.last_compression_rough_tokens = 46_000
    value.context_compressor = engine
    value.model = "test-model"
    value.max_tokens = 10_000
    value.client = MagicMock()
    value._cached_system_prompt = "Helpful."
    value._use_prompt_caching = False
    value._disable_streaming = True
    value.save_trajectories = False
    value.compression_enabled = True
    value.tool_delay = 0
    value._environment_probe = False
    value.context_rebase_enabled = False
    return value


def run(agent, texts, history=None, *, responses=None, tool_result=None):
    compactions = []
    provider_payloads = []
    replies = iter(responses or [response() for _ in texts])
    def inert_create(**kwargs):
        provider_payloads.append(kwargs)
        return next(replies)
    def inert_compact(messages, system, **kwargs):
        compactions.append(kwargs.get("approx_tokens"))
        engine = agent.context_compressor
        engine.last_prompt_tokens = -1
        engine.awaiting_real_usage_after_compression = True
        compacted = [dict(m, content="[summary]")
                     if len(str(m.get("content") or "")) > 5000 else m for m in messages]
        return compacted, "Helpful."
    agent.client.chat.completions.create.side_effect = inert_create
    with (patch.object(agent, "_compress_context", side_effect=inert_compact),
          patch.object(agent, "_persist_session"), patch.object(agent, "_save_trajectory"),
          patch.object(agent, "_cleanup_task_resources"),
          patch("run_agent.handle_function_call", return_value=json.dumps({"result": tool_result or "ok"}))):
        results = []
        for text in texts:
            result = agent.run_conversation(text, conversation_history=history)
            results.append(result)
            history = result["messages"]
    return results, compactions, provider_payloads


def test_actual_plain_host_compacts_gradual_no_usage_growth(agent):
    history = [{"role": "user", "content": "h" * 180_000},
               {"role": "assistant", "content": "previous answer"}]
    results, compactions, calls = run(agent, ["x" * 12_000] * 20, history)
    print(json.dumps({"turns": len(results), "provider_calls": len(calls),
                      "compactions": len(compactions)}))
    assert all(not r["failed"] for r in results)
    assert len(calls) == 20
    assert compactions
    assert agent.context_compressor.last_rough_tokens_when_real_prompt_fit == 46_000


@pytest.mark.parametrize("prompt", [None, 20_000])
def test_actual_tool_host_rebuilds_request_with_usage_or_missing_usage(agent, prompt):
    history = [{"role": "user", "content": "h" * 180_000},
               {"role": "assistant", "content": "previous answer"}]
    results, compactions, calls = run(agent, ["look up"], history, responses=[
        *[response(prompt, tool=True, index=i) for i in range(5)],
        response(prompt)], tool_result="t" * 35_000)
    assert not results[0]["failed"]
    assert len(calls) == 6
    assert compactions
    # Late/custom schema remains on the actual request; counting separately
    # is not evidence that a native/provider request was authorized.
    assert calls[-1]["tools"][0]["function"]["parameters"]["properties"]["conversation"]
    if prompt:
        assert agent.context_compressor.last_rough_tokens_when_real_prompt_fit > 0
        assert agent.context_compressor.last_real_prompt_tokens == prompt
