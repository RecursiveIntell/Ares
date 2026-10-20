"""Regression test for MoA primary-call routing on persisted preset switches.

Issue #54259 / #54669: switching a live agent to a MoA preset (the gateway
``/model <preset>`` path) built the MoAClient facade but left ``agent.api_mode``
set to whatever ``determine_api_mode`` / the resolved aggregator transport
produced (e.g. ``codex_responses`` or ``anthropic_messages``). The conversation
loop dispatches on ``agent.api_mode``, so a non-chat_completions value made it
call ``client.responses.create`` — which the MoAClient facade has no
``.responses`` for — and the call fell through to the ``moa://local``
placeholder, 404'd three times, then fell back to a reference model.

``agent_init.py`` already pins ``api_mode = "chat_completions"`` for
``provider == "moa"``; ``switch_model`` (the live in-place swap) must do the
same so the primary call always routes through ``MoAClient.chat.completions``.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest


def _make_fake_agent(primary_api_mode):
    """Use the real host methods with inert client and no context engine."""
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent.model = "minimax-m3"
    agent.provider = "opencode-go"
    agent.api_mode = primary_api_mode
    agent.api_key = "old-key"
    agent.base_url = "https://old.example/v1"
    agent.client = object()
    agent._client_kwargs = {"base_url": "https://old.example/v1"}
    agent._config_context_length = 123456
    agent._transport_cache = {}
    agent.context_compressor = None
    agent.quiet_mode = True
    # switch_model re-reads reasoning_echo for the incoming model as part of the
    # core field swap, before the moa branch runs. On a real AIAgent this is a
    # method; without it the swap raises and the rollback undoes every field
    # this test asserts on.
    agent._reasoning_echo_flag = False
    agent._read_reasoning_echo_from_config = lambda: False
    if primary_api_mode == "anthropic_messages":
        agent._anthropic_api_key = "old-anthropic-key"
        agent._anthropic_base_url = "https://old.example"
        agent._is_anthropic_oauth = False
    return agent


@pytest.mark.parametrize(
    "incoming_api_mode",
    ["codex_responses", "anthropic_messages", "chat_completions", ""],
)
@pytest.mark.parametrize("primary_api_mode", ["chat_completions", "anthropic_messages"])
def test_switch_to_moa_pins_chat_completions(monkeypatch, incoming_api_mode, primary_api_mode):
    """Switching to provider=moa must force api_mode=chat_completions.

    No matter what transport the resolver/aggregator implies for the preset,
    the outer agent.api_mode must end up chat_completions so the conversation
    loop dispatches through the MoAClient chat.completions facade rather than
    .responses.create against the moa://local placeholder.
    """
    from agent import agent_runtime_helpers as arh

    # Keep profile/config and credential reads offline while exercising the
    # real host helpers and lazy MoA facade through a complete successful swap.
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda *a, **k: None)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {})

    agent = _make_fake_agent(primary_api_mode)
    cache_policy = Mock(wraps=agent._anthropic_prompt_cache_policy)
    monkeypatch.setattr(agent, "_anthropic_prompt_cache_policy", cache_policy)
    arh.switch_model(
        agent,
        new_model="frontier",
        new_provider="moa",
        api_key="moa-virtual-provider",
        base_url="moa://local",
        api_mode=incoming_api_mode,
    )

    assert agent.provider == "moa"
    assert agent.base_url == "moa://local"
    assert agent.api_mode == "chat_completions", (
        f"MoA switch left api_mode={agent.api_mode!r}; the primary call would "
        "dispatch .responses.create / anthropic_messages against moa://local "
        "instead of MoAClient.chat.completions (issue #54259)."
    )
    # The MoAClient facade should be installed as the client.
    assert type(agent.client).__name__ == "MoAClient"
    assert cache_policy.call_args.kwargs["api_mode"] == agent.api_mode
    assert agent._primary_runtime["provider"] == agent.provider
    assert agent._primary_runtime["api_mode"] == agent.api_mode
    assert "anthropic_api_key" not in agent._primary_runtime
    assert "anthropic_base_url" not in agent._primary_runtime
