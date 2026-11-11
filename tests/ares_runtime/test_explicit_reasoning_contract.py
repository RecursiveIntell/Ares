"""Explicit oneshot intent reaches a real serialized-send boundary, offline."""
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest

from ares_runtime.physical_dispatch import bind_http_client, inference_call_scope
from hermes_cli.middleware import EgressDispatchDenied


def native(model, provider, controls):
    messages = [{"role": "user", "content": "private offline fixture"}]
    if provider == "anthropic":
        from agent.anthropic_adapter import build_anthropic_kwargs
        return "/v1/messages", build_anthropic_kwargs(model=model, messages=messages, tools=[], max_tokens=40000,
                                                       reasoning_config=controls, is_oauth=False)
    from agent.transports.codex import ResponsesApiTransport
    body = ResponsesApiTransport().build_kwargs(model=model, messages=messages, tools=[], reasoning_config=controls,
        provider="openai-codex", is_codex_backend=True, session_id="offline", base_url="https://fixture.invalid")
    return "/v1/responses", body


@pytest.fixture
def turn(monkeypatch):
    import hermes_cli.oneshot as oneshot
    import hermes_cli.config as config
    import hermes_cli.runtime_provider as providers
    import run_agent
    cfg = {"model": {"default": "gpt-6-luna", "provider": "openai-codex"},
           "agent": {"reasoning_effort": "low"}}
    monkeypatch.setattr(config, "load_config", lambda: deepcopy(cfg))
    monkeypatch.setattr(providers, "resolve_runtime_provider", lambda **kw: {
        "provider": kw["requested"], "requested_provider": kw["requested"], "api_key": "synthetic",
        "api_mode": "anthropic_messages" if kw["requested"] == "anthropic" else "codex_responses",
        "base_url": "https://fixture.invalid"})
    monkeypatch.setattr(oneshot, "_create_session_db_for_oneshot", lambda: None)
    monkeypatch.setattr("tools.process_registry.process_registry.wait_for_pending_completions", lambda *a: None)
    observations = SimpleNamespace(sends=[], built=[], transform=lambda value: value, agent=None, cfg=cfg, other_agent=False)
    class Agent:
        def __init__(self, **kwargs):
            self.model, self.provider = kwargs["model"], kwargs["provider"]
            self.reasoning_config, self.api_mode = kwargs["reasoning_config"], kwargs["api_mode"]
            self.session_id = "synthetic-explicit-effort"
            self.closed = False
            observations.agent = self
        def run_conversation(self, prompt):
            endpoint, built = native(self.model, self.provider, self.reasoning_config)
            body = observations.transform(built)
            observations.built.append(deepcopy(body))
            owner = SimpleNamespace(session_id="separate-auxiliary") if observations.other_agent else self
            def transport(request):
                observations.sends.append(bytes(request.content))
                return httpx.Response(200, json={"ok": True})
            with bind_http_client(httpx.Client(transport=httpx.MockTransport(transport), trust_env=False)) as client:
                with inference_call_scope(owner, body):
                    client.post("https://fixture.invalid" + endpoint, json=body)
            return {"final_response": "offline fixture", "completed": True}
        def shutdown_memory_provider(self):
            pass
        def close(self):
            self.closed = True
    monkeypatch.setattr(run_agent, "AIAgent", Agent)
    return observations


def invoke(model, provider, effort):
    from hermes_cli.oneshot import _run_agent
    return _run_agent("private fixture", model=model, provider=provider, reasoning=effort,
                      toolsets=[], use_config_toolsets=False)


@pytest.mark.parametrize("model,provider,effort", [
    ("gpt-6-luna", "openai-codex", "high"),
    ("claude-opus-5-5", "anthropic", "high"),
    ("claude-sonnet-4-5", "anthropic", "high"),
])
def test_supported_explicit_requests_preserve_native_body(turn, model, provider, effort):
    before = deepcopy(turn.cfg)
    answer, _ = invoke(model, provider, effort)
    assert answer == "offline fixture" and len(turn.sends) == 1
    assert json.loads(turn.sends[0]) == turn.built[0]
    assert turn.cfg == before and turn.agent.closed


@pytest.mark.parametrize("model,provider,effort", [
    ("claude-haiku-5-5", "anthropic", "high"),
    ("claude-haiku-4-5", "anthropic", "high"),
    ("claude-opus-4-6", "anthropic", "xhigh"),
    ("claude-opus-5-5", "anthropic", "ultra"),
    ("gpt-6-luna", "openai-codex", "ultra"),
    ("gpt-6-luna", "openai-codex", "minimal"),
])
def test_omitted_or_clamped_explicit_requests_deny_before_send(turn, model, provider, effort):
    with pytest.raises(EgressDispatchDenied) as error:
        invoke(model, provider, effort)
    assert error.value.code.startswith("EXPLICIT_REASONING_")
    assert turn.sends == [] and turn.agent.closed


@pytest.mark.parametrize("bad", ["missing", "changed", "contradictory", "malformed"])
def test_actual_serialized_controls_not_constructor_claims_are_enforced(turn, bad):
    def transform(body):
        body = deepcopy(body)
        if bad == "missing":
            body.pop("reasoning", None)
        elif bad == "changed":
            body["reasoning"]["effort"] = "medium"
        elif bad == "contradictory":
            body["reasoning"]["enabled"] = False
        else:
            body["reasoning"] = "high"
        return body
    turn.transform = transform
    with pytest.raises(EgressDispatchDenied):
        invoke("gpt-6-luna", "openai-codex", "high")
    assert turn.sends == []


def test_omitted_invocation_retains_legacy_config_behavior(turn):
    turn.transform = lambda body: {k: v for k, v in body.items() if k != "reasoning"}
    answer, _ = invoke("gpt-6-luna", "openai-codex", None)
    assert answer == "offline fixture" and len(turn.sends) == 1
    assert turn.agent.reasoning_config == {"enabled": True, "effort": "low"}


def test_unrelated_auxiliary_owner_does_not_inherit_parent_exact_request(turn):
    turn.other_agent = True
    turn.transform = lambda body: {k: v for k, v in body.items() if k != "reasoning"}
    answer, _ = invoke("gpt-6-luna", "openai-codex", "high")
    assert answer == "offline fixture" and len(turn.sends) == 1


def test_request_binding_is_removed_even_when_the_turn_fails(turn):
    turn.transform = lambda body: {k: v for k, v in body.items() if k != "reasoning"}
    with pytest.raises(EgressDispatchDenied):
        invoke("gpt-6-luna", "openai-codex", "high")
    answer, _ = invoke("gpt-6-luna", "openai-codex", None)
    assert answer == "offline fixture" and len(turn.sends) == 1
