import json

from ares_runtime.continuity.budget import stateless_payload_token_upper_bound
from agent.transports.anthropic import AnthropicTransport
from agent.transports.bedrock import BedrockTransport
from agent.transports.chat_completions import ChatCompletionsTransport
from agent.transports.codex import ResponsesApiTransport


SYSTEM = "You are a bounded test agent."
MESSAGES = [
    {"role": "user", "content": "Inspect queue.py and report the failing invariant."},
    {
        "role": "assistant",
        "content": "The queue state is inconsistent.",
        "_compressed_summary": True,
        "timestamp": "2026-09-24T00:00:00Z",
    },
    {"role": "user", "content": "Continue without pushing or merging."},
]
FULL_MESSAGES = [{"role": "system", "content": SYSTEM}, *MESSAGES]

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a bounded file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }
]


def _wire_bytes(value) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )


def _bound(mode: str):
    return stateless_payload_token_upper_bound(
        route_ref=f"test:{mode}:model",
        system_prompt=SYSTEM,
        messages=MESSAGES,
        tools=TOOLS,
    )


def test_chat_completions_conversion_stays_below_qualified_bound():
    transport = ChatCompletionsTransport()
    converted_messages = transport.convert_messages(MESSAGES, model="test-model")
    converted_tools = transport.convert_tools(TOOLS)
    assert _wire_bytes(
        {"system": SYSTEM, "messages": converted_messages, "tools": converted_tools}
    ) < _bound("chat_completions").tokens


def test_responses_conversion_stays_below_qualified_bound():
    transport = ResponsesApiTransport()
    converted_messages = transport.convert_messages(
        MESSAGES,
        base_url="https://api.openai.com/v1",
        replay_encrypted_reasoning=False,
    )
    converted_tools = transport.convert_tools(TOOLS)
    assert _wire_bytes(
        {"instructions": SYSTEM, "input": converted_messages, "tools": converted_tools}
    ) < _bound("codex_responses").tokens


def test_anthropic_conversion_stays_below_qualified_bound():
    transport = AnthropicTransport()
    converted_system, converted_messages = transport.convert_messages(MESSAGES)
    converted_tools = transport.convert_tools(TOOLS)
    assert _wire_bytes(
        {
            "system": SYSTEM + str(converted_system or ""),
            "messages": converted_messages,
            "tools": converted_tools,
        }
    ) < _bound("anthropic_messages").tokens


def test_bedrock_conversion_stays_below_qualified_bound():
    transport = BedrockTransport()
    converted_messages = transport.convert_messages(MESSAGES)
    converted_tools = transport.convert_tools(TOOLS)
    assert _wire_bytes(
        {"system": SYSTEM, "messages": converted_messages, "tools": converted_tools}
    ) < _bound("bedrock_converse").tokens


def test_chat_completions_final_kwargs_stay_below_qualified_bound():
    transport = ChatCompletionsTransport()
    kwargs = transport.build_kwargs(
        "test-model",
        FULL_MESSAGES,
        TOOLS,
        max_tokens=4096,
        max_tokens_param_fn=lambda value: {"max_tokens": value},
        reasoning_config={"enabled": False},
        base_url="https://example.invalid/v1",
        session_id="session-test",
    )
    assert _wire_bytes(kwargs) < _bound("chat_completions").tokens


def test_responses_final_kwargs_stay_below_qualified_bound():
    transport = ResponsesApiTransport()
    kwargs = transport.build_kwargs(
        "gpt-5.6",
        MESSAGES,
        TOOLS,
        instructions=SYSTEM,
        reasoning_config={"enabled": True, "effort": "medium"},
        max_tokens=4096,
        base_url="https://api.openai.com/v1",
        session_id="session-test",
        cache_scope_id="conversation-root",
        replay_encrypted_reasoning=False,
    )
    assert _wire_bytes(kwargs) < _bound("codex_responses").tokens


def test_anthropic_final_kwargs_stay_below_qualified_bound():
    transport = AnthropicTransport()
    kwargs = transport.build_kwargs(
        "claude-sonnet-4-6",
        FULL_MESSAGES,
        TOOLS,
        max_tokens=4096,
        reasoning_config={"enabled": False},
        base_url="https://api.anthropic.com",
    )
    assert _wire_bytes(kwargs) < _bound("anthropic_messages").tokens


def test_bedrock_final_kwargs_stay_below_qualified_bound():
    transport = BedrockTransport()
    kwargs = transport.build_kwargs(
        "anthropic.claude-test",
        FULL_MESSAGES,
        TOOLS,
        max_tokens=4096,
        temperature=0.2,
        region="us-east-1",
    )
    # Dispatch-only sentinel fields are serialized here too, making this
    # stricter than the boto3 payload that eventually leaves the process.
    assert _wire_bytes(kwargs) < _bound("bedrock_converse").tokens
