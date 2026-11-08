"""Offline contracts for one-shot effort propagation (no provider requests)."""
from copy import deepcopy
from unittest.mock import MagicMock
import sys

import pytest


@pytest.fixture
def isolated_agent(monkeypatch):
    import hermes_cli.oneshot as oneshot
    import hermes_cli.config as config
    import hermes_cli.runtime_provider as providers
    import hermes_cli.mcp_startup as mcp
    import run_agent
    cfg = {"model": {"model": "gpt-6-luna", "provider": "openai-codex"},
           "agent": {"reasoning_effort": "low"}}
    monkeypatch.setattr(config, "load_config", lambda: deepcopy(cfg))
    runtime = {"provider": "openai-codex", "requested_provider": "openai-codex",
               "api_mode": "codex_responses", "api_key": None,
               "base_url": "https://chatgpt.com/backend-api/codex"}
    resolver = MagicMock(return_value=runtime)
    monkeypatch.setattr(providers, "resolve_runtime_provider", resolver)
    monkeypatch.setattr(mcp, "ensure_mcp_discovery_before_agent_build", lambda **kw: None)
    monkeypatch.setattr(oneshot, "_create_session_db_for_oneshot", lambda: None)
    agent = MagicMock()
    agent.run_conversation.return_value = {"final_response": "offline", "completed": True}
    factory = MagicMock(return_value=agent)
    monkeypatch.setattr(run_agent, "AIAgent", factory)
    monkeypatch.setattr("tools.process_registry.process_registry.wait_for_pending_completions", lambda *a: None)
    return cfg, factory, resolver


def test_configured_effort_reaches_agent(isolated_agent):
    from hermes_cli.oneshot import _run_agent
    _, factory, _ = isolated_agent
    _run_agent("offline", model="gpt-6-luna", provider="openai-codex", use_config_toolsets=False)
    assert factory.call_args.kwargs["reasoning_config"] == {"enabled": True, "effort": "low"}


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "none"])
def test_explicit_effort_overrides_profile_without_mutation(isolated_agent, effort):
    from hermes_cli.oneshot import _run_agent
    cfg, factory, _ = isolated_agent
    before = deepcopy(cfg)
    _run_agent("offline", model="gpt-6-luna", provider="openai-codex",
               use_config_toolsets=False, reasoning=effort)
    expected = {"enabled": False} if effort == "none" else {"enabled": True, "effort": effort}
    assert factory.call_args.kwargs["reasoning_config"] == expected
    assert cfg == before


def test_model_override_uses_its_canonical_reasoning_override(isolated_agent):
    from hermes_cli.oneshot import _run_agent
    cfg, factory, _ = isolated_agent
    cfg["agent"]["reasoning_overrides"] = {"gpt-6-luna": "high"}
    _run_agent("offline", model="gpt-6-luna", provider="openai-codex", use_config_toolsets=False)
    assert factory.call_args.kwargs["reasoning_config"] == {"enabled": True, "effort": "high"}


def test_unset_reasoning_stays_unset(isolated_agent):
    from hermes_cli.oneshot import _run_agent
    cfg, factory, _ = isolated_agent
    cfg["agent"].clear()
    _run_agent("offline", model="gpt-6-luna", provider="openai-codex", use_config_toolsets=False)
    assert factory.call_args.kwargs["reasoning_config"] is None


@pytest.mark.parametrize("effort", ["invalid-effort", "", "   "])
def test_invalid_explicit_effort_stops_before_runtime_resolution(isolated_agent, effort):
    from hermes_cli.oneshot import _run_agent
    _, factory, resolver = isolated_agent
    with pytest.raises(ValueError, match="reasoning"):
        _run_agent("offline", model="gpt-6-luna", provider="openai-codex",
                   use_config_toolsets=False, reasoning=effort)
    resolver.assert_not_called()
    factory.assert_not_called()


def test_run_oneshot_passes_effort_to_agent_chain(monkeypatch):
    import hermes_cli.oneshot as oneshot
    runner = MagicMock(return_value=("offline", {}))
    monkeypatch.setattr(oneshot, "_run_agent", runner)
    assert oneshot.run_oneshot("offline", model="gpt-6-luna", provider="openai-codex", reasoning="high") == 0
    assert runner.call_args.kwargs["reasoning"] == "high"


@pytest.mark.parametrize("fast", [True, False])
def test_actual_cli_parser_and_dispatch_preserve_high(monkeypatch, fast, isolated_agent):
    import hermes_cli.main as main
    from agent.transports.codex import ResponsesApiTransport
    _, factory, _ = isolated_agent
    monkeypatch.setattr(sys, "argv", ["hermes", "--cli", "--safe-mode", "--provider", "openai-codex",
                                      "--model", "gpt-6-luna", "--reasoning", "high", "--oneshot", "offline"])
    monkeypatch.setattr(main, "_prepare_agent_startup", lambda *a: None)
    monkeypatch.setattr(main, "_confirm_startup_expensive_model_override", lambda *a: None)
    monkeypatch.setattr(main, "_cleanup_oneshot_runtime", lambda: None)
    monkeypatch.setattr(main, "_has_any_provider_configured", lambda: True)
    monkeypatch.setattr(main, "_sync_bundled_skills_for_startup", lambda: None)
    monkeypatch.setattr(main, "_pin_kanban_board_env", lambda: None)
    if not fast:
        monkeypatch.setattr(main, "_try_fast_chat_launch", lambda: False)
        monkeypatch.setattr(main, "_try_termux_fast_cli_launch", lambda: False)
    def exit_stub(code):
        raise SystemExit(code)
    monkeypatch.setattr(main, "_exit_after_oneshot", exit_stub)
    with pytest.raises(SystemExit) as stop:
        main.main()
    assert stop.value.code == 0
    cfg = factory.call_args.kwargs.get("reasoning_config")
    native = ResponsesApiTransport().build_kwargs(model="gpt-6-luna", messages=[{"role": "user", "content": "offline"}],
             tools=[], reasoning_config=cfg, provider="openai-codex", is_codex_backend=True,
             session_id="offline", base_url="https://chatgpt.com/backend-api/codex")
    assert native["reasoning"]["effort"] == "high"


def test_explicit_empty_library_toolsets_survive_without_defaults(isolated_agent, monkeypatch):
    import hermes_cli.oneshot as oneshot
    import hermes_cli.mcp_startup as mcp
    _, factory, _ = isolated_agent
    discovery = MagicMock()
    monkeypatch.setattr(mcp, "ensure_mcp_discovery_before_agent_build", discovery)
    oneshot._run_agent("offline", model="gpt-6-luna", provider="openai-codex",
                       toolsets=[], use_config_toolsets=False)
    assert factory.call_args.kwargs["enabled_toolsets"] == []
    discovery.assert_not_called()


def test_public_library_empty_toolsets_are_not_blank_cli_default(monkeypatch):
    import hermes_cli.oneshot as oneshot
    runner = MagicMock(return_value=("offline", {}))
    monkeypatch.setattr(oneshot, "_run_agent", runner)
    assert oneshot.run_oneshot("offline", model="gpt-6-luna", provider="openai-codex", toolsets=[]) == 0
    assert runner.call_args.kwargs["toolsets"] == []
    assert runner.call_args.kwargs["use_config_toolsets"] is False
    assert oneshot._normalize_toolsets("") is None
    assert oneshot._normalize_toolsets(None) is None


def test_usage_file_records_native_send_and_retains_it_on_later_failure(isolated_agent, monkeypatch, tmp_path):
    import json
    import httpx
    import hermes_cli.oneshot as oneshot
    from ares_runtime.physical_dispatch import bind_http_client, inference_call_scope
    _, factory, _ = isolated_agent
    def conversation(prompt):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=httpx.ByteStream(b'{}')))
        with bind_http_client(httpx.Client(transport=transport)) as sdk:
            body = {"model": "fixture", "reasoning": {"effort": "high"},
                    "input": "private-do-not-save", "max_output_tokens": 1}
            # Exercise the production logical-call boundary, not an unowned
            # raw inference send (now refused for an explicit request).
            with inference_call_scope(factory.return_value, body):
                sdk.post("https://fixture.invalid/v1/responses", json=body)
        raise RuntimeError("fixture failure after send")
    factory.return_value.run_conversation.side_effect = conversation
    path = tmp_path / "usage.json"
    assert oneshot.run_oneshot("offline", model="gpt-6-luna", provider="openai-codex", reasoning="high", usage_file=str(path)) == 1
    report = json.loads(path.read_text())
    assert report["failed"] is True
    evidence = report["native_request_evidence"]
    assert len(evidence["requests"]) == 1
    assert evidence["requests"][0]["native_controls"]["reasoning"]["effort"] == "high"
    assert evidence["requests"][0]["outcome"] == "ok"
    assert evidence["provider_response_model"] is None
    assert "private-do-not-save" not in json.dumps(report)
