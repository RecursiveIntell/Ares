"""Focused regression coverage for offline Codex app-server selection/init."""

from types import SimpleNamespace

import pytest

from hermes_cli import runtime_provider as rp


@pytest.mark.parametrize("provider", ["openai", "openai-codex"])
@pytest.mark.parametrize(
    "native_setting",
    [{"openai_runtime": "codex_app_server"}, {"api_mode": "codex_app_server"}],
)
def test_native_trial_selection_precedes_pool_and_credential_resolution(
    monkeypatch, native_setting, provider
):
    model_cfg = {
        "provider": provider,
        "default": "gpt-5-codex",
        **native_setting,
    }
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "model": model_cfg,
            "compression": {"context_rebase_enabled": True},
        },
    )

    def unexpected(*_args, **_kwargs):
        pytest.fail("native trial selection must not resolve credentials or pools")

    monkeypatch.setattr(rp, "resolve_provider", unexpected)
    monkeypatch.setattr(rp, "load_pool", unexpected)
    monkeypatch.setattr(rp, "resolve_codex_runtime_credentials", unexpected)

    resolved = rp.resolve_runtime_provider(requested="auto")

    assert resolved["provider"] == provider
    assert resolved["api_mode"] == "codex_app_server"
    assert resolved["api_key"] == ""
    assert resolved["source"] == "codex-app-server-native"


@pytest.mark.parametrize(
    "model_cfg,requested,kwargs",
    [
        ({"provider": "anthropic", "openai_runtime": "codex_app_server"}, "anthropic", {}),
        (
            {"provider": "openai-codex", "openai_runtime": "codex_app_server"},
            "openai-codex",
            {"explicit_api_key": "caller-key"},
        ),
        (
            {"provider": "openai-codex", "openai_runtime": "codex_app_server"},
            "openai-codex",
            {"explicit_base_url": "https://proxy.example/v1"},
        ),
    ],
)
def test_native_trial_rejects_incompatible_provider_or_explicit_route(
    monkeypatch, model_cfg, requested, kwargs
):
    monkeypatch.setattr(rp, "_get_model_config", lambda: model_cfg)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "model": model_cfg,
            "compression": {"context_rebase_enabled": True},
        },
    )

    with pytest.raises((ValueError, rp.AuthError), match="codex_app_server"):
        rp.resolve_runtime_provider(requested=requested, **kwargs)


def test_native_selection_precedes_explicit_route_without_continuity_trial(monkeypatch):
    model_cfg = {
        "provider": "openai-codex",
        "default": "gpt-5-codex",
        "api_mode": "codex_app_server",
    }
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"model": model_cfg, "compression": {}},
    )

    def unexpected(*_args, **_kwargs):
        pytest.fail("native mode must not resolve the explicit API route")

    monkeypatch.setattr(rp, "resolve_provider", unexpected)
    monkeypatch.setattr(rp, "load_pool", unexpected)
    monkeypatch.setattr(rp, "resolve_codex_runtime_credentials", unexpected)

    resolved = rp.resolve_runtime_provider(
        requested="openai-codex",
        explicit_api_key="caller-key",
        explicit_base_url="https://proxy.example/v1",
    )

    assert resolved["provider"] == "openai-codex"
    assert resolved["api_mode"] == "codex_app_server"
    assert resolved["api_key"] == ""
    assert resolved["base_url"] == ""


def test_init_agent_rejects_native_when_context_rebase_is_configured(monkeypatch):
    from agent.agent_init import init_agent
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._base_url = ""
    agent._base_url_lower = ""
    agent._base_url_hostname = ""

    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"compression": {"context_rebase_enabled": True}})
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"compression": {"context_rebase_enabled": True}})
    monkeypatch.setattr(agent, "_get_transport", lambda: pytest.fail("generic transport warmup called"), raising=False)

    with pytest.raises(ValueError, match="not qualified.*context_rebase_enabled"):
        init_agent(
            agent,
            base_url="",
            api_key="",
            provider="openai-codex",
            api_mode="codex_app_server",
            model="gpt-5-codex",
            skip_context_files=True,
            skip_memory=True,
            quiet_mode=True,
        )


def test_init_agent_native_mode_skips_generic_transport_and_sdk(monkeypatch):
    from agent.agent_init import init_agent
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._base_url = ""
    agent._base_url_lower = ""
    agent._base_url_hostname = ""

    monkeypatch.setattr("agent.auxiliary_client.resolve_provider_client", lambda *a, **k: pytest.fail("generic provider router called"))
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {})
    monkeypatch.setattr("agent.model_metadata.query_ollama_num_ctx", lambda *_a, **_k: None)
    monkeypatch.setattr("run_agent.get_tool_definitions", lambda *_a, **_k: [])
    monkeypatch.setattr("hermes_cli.model_normalize.normalize_model_for_provider", lambda model, _provider: model)
    monkeypatch.setattr("agent.iteration_budget.IterationBudget", lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr("hermes_cli.config.cfg_get", lambda *_a, **_k: None)
    monkeypatch.setattr(agent, "_get_transport", lambda: pytest.fail("generic transport warmup called"), raising=False)
    monkeypatch.setattr(agent, "_create_openai_client", lambda *_a, **_k: pytest.fail("OpenAI SDK constructed"), raising=False)

    init_agent(
        agent,
        base_url="",
        api_key="",
        provider="openai-codex",
        api_mode="codex_app_server",
        model="gpt-5-codex",
        skip_context_files=True,
        skip_memory=True,
        quiet_mode=True,
    )

    assert agent.api_mode == "codex_app_server"
    assert agent.client is None
    assert agent._client_kwargs == {}


@pytest.mark.parametrize("continuity_enabled", [False, True])
def test_direct_init_binds_configured_openai_provider_before_native_guard(
    monkeypatch, tmp_path, continuity_enabled
):
    from agent.agent_init import init_agent
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._base_url = agent._base_url_lower = agent._base_url_hostname = ""
    config = {
        "model": {
            "default": {"provider": "openai", "model": "dummy-model"},
            "openai_runtime": "codex_app_server",
        },
        "compression": {"context_rebase_enabled": continuity_enabled},
    }
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda *_a, **_k: pytest.fail("generic provider router called"),
    )
    monkeypatch.setattr("agent.model_metadata.query_ollama_num_ctx", lambda *_a, **_k: None)
    monkeypatch.setattr("run_agent.get_tool_definitions", lambda *_a, **_k: [])
    monkeypatch.setattr("agent.iteration_budget.IterationBudget", lambda *_a: SimpleNamespace())
    monkeypatch.setattr("hermes_cli.config.cfg_get", lambda *_a, **_k: None)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent, "_get_transport", lambda: pytest.fail("generic transport warmed"), raising=False)
    monkeypatch.setattr(agent, "_create_openai_client", lambda *_a, **_k: pytest.fail("OpenAI SDK constructed"), raising=False)

    if continuity_enabled:
        with pytest.raises(ValueError, match="not qualified.*context_rebase_enabled"):
            init_agent(
                agent,
                model="dummy-model",
                skip_memory=True,
                skip_context_files=True,
                quiet_mode=True,
            )
    else:
        init_agent(
            agent,
            model="dummy-model",
            skip_memory=True,
            skip_context_files=True,
            quiet_mode=True,
        )
        assert agent.provider == "openai"
        assert agent.requested_provider == "openai"
        assert agent.api_mode == "codex_app_server"
        assert agent.client is None


@pytest.mark.parametrize(
    "native_setting",
    [{"api_mode": "codex_app_server"}, {"openai_runtime": "codex_app_server"}],
    ids=["api-mode", "openai-runtime"],
)
def test_direct_init_configured_native_trial_fails_before_generic_sdk(
    monkeypatch, tmp_path, native_setting
):
    from agent.agent_init import init_agent
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._base_url = agent._base_url_lower = agent._base_url_hostname = ""
    config = {
        "model": {"provider": "openai", "default": "dummy-model", **native_setting},
        "compression": {"context_rebase_enabled": True},
    }
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    monkeypatch.setattr(
        "hermes_cli.model_normalize.normalize_model_for_provider",
        lambda model, _provider: model,
    )
    monkeypatch.setattr(agent, "_get_transport", lambda: None, raising=False)
    monkeypatch.setattr(
        agent,
        "_create_openai_client",
        lambda *_args, **_kwargs: pytest.fail("generic OpenAI SDK constructed"),
        raising=False,
    )
    monkeypatch.setattr(
        "tools.lazy_deps.ensure",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(ValueError, match="codex_app_server"):
        init_agent(
            agent,
            provider="openai",
            model="dummy-model",
            api_key="dummy-key",
            base_url="https://offline.invalid/v1",
            skip_memory=True,
            skip_context_files=True,
            quiet_mode=True,
        )


def test_direct_init_trial_rejects_explicit_alternate_to_configured_native(
    monkeypatch, tmp_path
):
    from agent.agent_init import init_agent
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._base_url = agent._base_url_lower = agent._base_url_hostname = ""
    config = {
        "model": {
            "provider": "openai",
            "default": "dummy-model",
            "api_mode": "codex_app_server",
        },
        "compression": {"context_rebase_enabled": True},
    }
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    monkeypatch.setattr(agent, "_get_transport", lambda: None, raising=False)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        agent,
        "_create_openai_client",
        lambda *_args, **_kwargs: pytest.fail("alternate generic SDK constructed"),
        raising=False,
    )

    with pytest.raises(ValueError, match="codex_app_server"):
        init_agent(
            agent,
            provider="openai",
            api_mode="chat_completions",
            model="dummy-model",
            api_key="dummy-key",
            base_url="https://offline.invalid/v1",
            skip_memory=True,
            skip_context_files=True,
            quiet_mode=True,
        )


def test_direct_init_explicit_alternate_remains_compatible_without_trial(
    monkeypatch, tmp_path
):
    from agent.agent_init import init_agent
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._base_url = agent._base_url_lower = agent._base_url_hostname = ""
    config = {
        "model": {
            "provider": "openai",
            "default": "dummy-model",
            "api_mode": "codex_app_server",
        },
        "compression": {},
    }
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    monkeypatch.setattr(
        "hermes_cli.model_normalize.normalize_model_for_provider",
        lambda model, _provider: model,
    )
    monkeypatch.setattr("agent.model_metadata.query_ollama_num_ctx", lambda *_a, **_k: None)
    monkeypatch.setattr("run_agent.get_tool_definitions", lambda *_a, **_k: [])
    monkeypatch.setattr("agent.iteration_budget.IterationBudget", lambda *_a: SimpleNamespace())
    monkeypatch.setattr("hermes_cli.config.cfg_get", lambda *_a, **_k: None)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent, "_get_transport", lambda: None, raising=False)
    monkeypatch.setattr(agent, "_create_openai_client", lambda *_a, **_k: object(), raising=False)

    init_agent(
        agent,
        provider="openai",
        api_mode="chat_completions",
        model="dummy-model",
        api_key="dummy-key",
        base_url="https://offline.invalid/v1",
        skip_memory=True,
        skip_context_files=True,
        quiet_mode=True,
    )

    assert agent.api_mode == "chat_completions"


def test_direct_init_openai_runtime_is_inert_for_unsupported_provider_without_trial(
    monkeypatch, tmp_path
):
    from agent.agent_init import init_agent
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._base_url = agent._base_url_lower = agent._base_url_hostname = ""
    config = {
        "model": {
            "provider": "anthropic",
            "default": "dummy-model",
            "openai_runtime": "codex_app_server",
        },
        "compression": {},
    }
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    monkeypatch.setattr(
        "hermes_cli.model_normalize.normalize_model_for_provider",
        lambda model, _provider: model,
    )
    monkeypatch.setattr("agent.model_metadata.query_ollama_num_ctx", lambda *_a, **_k: None)
    monkeypatch.setattr("run_agent.get_tool_definitions", lambda *_a, **_k: [])
    monkeypatch.setattr("agent.iteration_budget.IterationBudget", lambda *_a: SimpleNamespace())
    monkeypatch.setattr("hermes_cli.config.cfg_get", lambda *_a, **_k: None)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent, "_get_transport", lambda: None, raising=False)
    monkeypatch.setattr(
        "agent.anthropic_adapter.build_anthropic_client",
        lambda *_a, **_k: object(),
    )

    init_agent(
        agent,
        provider="anthropic",
        model="dummy-model",
        api_key="dummy-key",
        base_url="https://api.anthropic.com/v1",
        skip_memory=True,
        skip_context_files=True,
        quiet_mode=True,
    )

    assert agent.api_mode == "anthropic_messages"


@pytest.mark.parametrize(
    "native_setting,continuity_enabled",
    [
        ({"api_mode": "codex_app_server"}, False),
        ({"openai_runtime": "codex_app_server"}, True),
    ],
    ids=["explicit-native-mode", "trial-native-runtime"],
)
def test_direct_init_rejects_native_selection_for_unsupported_provider(
    monkeypatch, tmp_path, native_setting, continuity_enabled
):
    from agent.agent_init import init_agent
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._base_url = agent._base_url_lower = agent._base_url_hostname = ""
    config = {
        "model": {
            "provider": "anthropic",
            "default": "dummy-model",
            **native_setting,
        },
        "compression": {"context_rebase_enabled": continuity_enabled},
    }
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    monkeypatch.setattr(
        agent,
        "_create_openai_client",
        lambda *_args, **_kwargs: pytest.fail("generic OpenAI SDK constructed"),
        raising=False,
    )

    with pytest.raises(ValueError, match="codex_app_server.*requires provider"):
        init_agent(
            agent,
            provider="anthropic",
            model="dummy-model",
            api_key="dummy-key",
            base_url="https://api.anthropic.com/v1",
            skip_memory=True,
            skip_context_files=True,
            quiet_mode=True,
        )


@pytest.fixture
def offline_route_boundaries(monkeypatch):
    """Observe routing with dummy pool entries; never read account state."""
    state = SimpleNamespace(
        config={"model": {}, "compression": {}}, calls=[], provider="openai"
    )

    def resolve_provider(*_args, **_kwargs):
        state.calls.append("provider")
        return state.provider

    class FakePool:
        def has_credentials(self):
            state.calls.append("pool-check")
            return True

        def select(self):
            state.calls.append("pool-select")
            return SimpleNamespace(
                runtime_api_key="offline-dummy-key",
                runtime_base_url="https://offline.invalid/v1",
                source="offline-fake-pool",
            )

    state.pool = FakePool()

    def load_pool(*_args, **_kwargs):
        state.calls.append("pool-load")
        return state.pool

    def denied(*_args, **_kwargs):
        pytest.fail("unintercepted credential/account boundary reached")

    monkeypatch.setattr("hermes_cli.config.load_config", lambda: state.config)
    monkeypatch.setattr(rp, "_get_model_config", lambda: state.config["model"])
    monkeypatch.setattr(rp, "_getenv", lambda _name, default="": default)
    monkeypatch.setattr(rp, "_resolve_named_custom_runtime", lambda **_kw: None)
    monkeypatch.setattr(rp, "_resolve_explicit_runtime", lambda **_kw: None)
    monkeypatch.setattr(rp, "resolve_provider", resolve_provider)
    monkeypatch.setattr(rp, "load_pool", load_pool)
    monkeypatch.setattr(rp, "credential_pool_matches_provider", lambda *_a, **_kw: True)
    monkeypatch.setattr(rp, "resolve_codex_runtime_credentials", denied)
    monkeypatch.setattr(rp, "resolve_nous_runtime_credentials", denied)
    monkeypatch.setattr(rp, "resolve_api_key_provider_credentials", denied)
    return state


@pytest.mark.parametrize("configured_provider", ["auto", None, ""])
@pytest.mark.parametrize("resolved_provider", ["openai", "openai-codex"])
def test_ambiguous_native_opt_in_refuses_before_generic_pool(
    offline_route_boundaries, configured_provider, resolved_provider
):
    """The original six red combinations now require an explicit route."""
    state = offline_route_boundaries
    state.config["model"] = {
        "default": "offline-model", "openai_runtime": "codex_app_server"
    }
    if configured_provider is not None:
        state.config["model"]["provider"] = configured_provider
    state.provider = resolved_provider

    with pytest.raises(ValueError, match="requires.*provider"):
        rp.resolve_runtime_provider(requested="auto")

    assert state.calls == []


@pytest.mark.parametrize("requested", [None, "", "auto"])
@pytest.mark.parametrize("model", ["offline-model", "openai/gpt-5-codex"])
def test_model_spelling_and_unspecified_request_do_not_authorize_native(
    offline_route_boundaries, requested, model
):
    state = offline_route_boundaries
    state.config["model"] = {
        "default": model, "openai_runtime": "codex_app_server"
    }

    with pytest.raises(ValueError, match="requires.*provider"):
        rp.resolve_runtime_provider(requested=requested)

    assert state.calls == []


@pytest.mark.parametrize("configured_provider", ["auto", None, ""])
@pytest.mark.parametrize("runtime_flag", [None, "", "auto"])
def test_default_runtime_retains_generic_route(
    offline_route_boundaries, configured_provider, runtime_flag
):
    state = offline_route_boundaries
    model_cfg = {"default": "offline-model"}
    if configured_provider is not None:
        model_cfg["provider"] = configured_provider
    if runtime_flag is not None:
        model_cfg["openai_runtime"] = runtime_flag
    state.config["model"] = model_cfg

    result = rp.resolve_runtime_provider(requested=None)

    assert result["provider"] == "openai"
    assert result["api_mode"] == "chat_completions"
    assert result["api_key"] == "offline-dummy-key"
    assert result["credential_pool"] is state.pool
    assert state.calls == ["provider", "pool-load", "pool-check", "pool-select"]


@pytest.mark.parametrize("provider", ["anthropic", "openrouter", "ollama-cloud"])
@pytest.mark.parametrize("configured_provider", ["openai", "openai-codex"])
def test_explicit_non_native_request_wins_over_configured_native_provider(
    offline_route_boundaries, provider, configured_provider
):
    state = offline_route_boundaries
    state.config["model"] = {
        "provider": configured_provider, "default": "offline-model",
        "openai_runtime": "codex_app_server",
    }
    state.provider = provider

    result = rp.resolve_runtime_provider(requested=provider)

    assert result["provider"] == provider
    assert result["requested_provider"] == provider
    assert result["api_mode"] != "codex_app_server"
    assert result["credential_pool"] is state.pool
    assert result["api_key"] == "offline-dummy-key"


@pytest.mark.parametrize("configured_provider", ["auto", None, ""])
@pytest.mark.parametrize("resolved_provider", ["openai", "openai-codex"])
def test_ambiguous_configured_api_endpoint_is_never_late_converted_to_native(
    offline_route_boundaries, configured_provider, resolved_provider
):
    state = offline_route_boundaries
    state.config["model"] = {
        "default": "offline-model", "base_url": "https://api.openai.com/v1",
        "openai_runtime": "codex_app_server",
    }
    if configured_provider is not None:
        state.config["model"]["provider"] = configured_provider
    state.provider = resolved_provider

    result = rp.resolve_runtime_provider(requested="auto")

    assert result["provider"] == resolved_provider
    assert result["api_mode"] != "codex_app_server"
    assert result["source"] == "offline-fake-pool"
    assert result["credential_pool"] is state.pool
    assert result["api_key"] == "offline-dummy-key"


@pytest.mark.parametrize("route", ["key", "url", "config-local-url"])
@pytest.mark.parametrize("configured_provider", ["auto", None, ""])
def test_ambiguous_native_toggle_preserves_explicit_api_route_priority(
    monkeypatch, offline_route_boundaries, route, configured_provider
):
    from hermes_cli.auth import resolve_provider

    state = offline_route_boundaries
    state.config["model"] = {
        "default": "offline-model", "openai_runtime": "codex_app_server"
    }
    if configured_provider is not None:
        state.config["model"]["provider"] = configured_provider
    kwargs = {}
    if route == "key":
        kwargs["explicit_api_key"] = "offline-caller-key"
    elif route == "url":
        kwargs["explicit_base_url"] = "https://offline.invalid/v1"
    else:
        state.config["model"]["base_url"] = "http://127.0.0.1:11434/v1"

    expected = {
        "provider": "openrouter", "api_mode": "chat_completions",
        "api_key": "offline-caller-key", "base_url": "https://offline.invalid/v1",
        "source": "offline-explicit-api",
    }

    def explicit_route(**received):
        state.calls.append("explicit-api")
        assert received["requested_provider"] == "auto"
        assert received.get("explicit_api_key") == kwargs.get("explicit_api_key")
        assert received.get("explicit_base_url") == kwargs.get("explicit_base_url")
        return dict(expected)

    # Real canonical auth resolution must return before scoped keys/accounts.
    monkeypatch.setattr(rp, "resolve_provider", resolve_provider)
    monkeypatch.setattr(rp, "_resolve_explicit_runtime", explicit_route)
    monkeypatch.setattr(rp, "_resolve_openrouter_runtime", explicit_route)
    result = rp.resolve_runtime_provider(requested="auto", **kwargs)

    assert result["api_mode"] == "chat_completions"
    assert result["source"] == "offline-explicit-api"
    assert state.calls == ["explicit-api"]


@pytest.mark.parametrize("provider", ["openai", "openai-codex"])
@pytest.mark.parametrize("outer_provider", [None, "", "auto"])
def test_canonical_nested_model_provider_selects_native_before_pool(
    offline_route_boundaries, provider, outer_provider
):
    from hermes_cli.config import _normalize_root_model_keys

    state = offline_route_boundaries
    raw = {"default": {"provider": provider, "model": "offline-model"},
           "openai_runtime": "codex_app_server"}
    if outer_provider is not None:
        raw["provider"] = outer_provider
    state.config = _normalize_root_model_keys({"model": raw, "compression": {}})

    result = rp.resolve_runtime_provider(requested="auto")

    assert state.config["model"]["default"] == "offline-model"
    assert result["provider"] == provider
    assert result["api_mode"] == "codex_app_server"
    assert result["api_key"] == result["base_url"] == ""
    assert result["credential_pool"] is None
    assert state.calls == []


def test_canonical_outer_non_native_provider_keeps_nested_model_ownership(
    offline_route_boundaries,
):
    from hermes_cli.config import _normalize_root_model_keys

    state = offline_route_boundaries
    state.provider = "anthropic"
    state.config = _normalize_root_model_keys({"model": {
        "provider": "anthropic",
        "default": {"provider": "openai", "model": "offline-model"},
        "openai_runtime": "codex_app_server",
    }})

    result = rp.resolve_runtime_provider(requested=None)

    assert state.config["model"]["default"] == "offline-model"
    assert state.config["model"]["provider"] == "anthropic"
    assert result["provider"] == "anthropic"
    assert result["api_mode"] == "anthropic_messages"
    assert result["credential_pool"] is state.pool


@pytest.mark.parametrize("provider", ["openai", "openai-codex"])
@pytest.mark.parametrize("native_key", ["openai_runtime", "api_mode"])
@pytest.mark.parametrize("request_form", ["auto", None, "", "explicit"])
@pytest.mark.parametrize("enabled", [False, True, "absent"])
def test_native_enable_rule_uses_the_selected_provider(
    offline_route_boundaries, provider, native_key, request_form, enabled
):
    state = offline_route_boundaries
    state.config = {
        "model": {"provider": provider, "default": "offline-model",
                  native_key: "codex_app_server"},
        "providers": {provider: {} if enabled == "absent" else {"enabled": enabled}},
        "compression": {},
    }
    requested = provider if request_form == "explicit" else request_form

    if enabled is False:
        with pytest.raises(ValueError, match=f"provider '{provider}' is disabled"):
            rp.resolve_runtime_provider(requested=requested)
    else:
        result = rp.resolve_runtime_provider(requested=requested)
        assert result["provider"] == provider
        assert result["api_mode"] == "codex_app_server"
        assert result["api_key"] == result["base_url"] == ""
        assert result["credential_pool"] is None
    assert state.calls == []


@pytest.mark.parametrize("provider", ["openai", "openai-codex"])
@pytest.mark.parametrize("enabled", [False, True])
def test_nested_native_provider_obeys_its_own_enable_rule(
    offline_route_boundaries, provider, enabled
):
    from hermes_cli.config import _normalize_root_model_keys

    state = offline_route_boundaries
    state.config = _normalize_root_model_keys({
        "model": {"provider": "auto",
                  "default": {"provider": provider, "model": "offline-model"},
                  "openai_runtime": "codex_app_server"},
        "providers": {provider: {"enabled": enabled}},
        "compression": {},
    })

    if enabled:
        result = rp.resolve_runtime_provider(requested="auto")
        assert result["provider"] == provider
        assert result["api_mode"] == "codex_app_server"
        assert result["credential_pool"] is None
    else:
        with pytest.raises(ValueError, match=f"provider '{provider}' is disabled"):
            rp.resolve_runtime_provider(requested="auto")
    assert state.config["model"]["default"] == "offline-model"
    assert state.calls == []


@pytest.mark.parametrize("disabled_value", ["false", "0", " no ", "OFF", 0, None])
def test_native_enable_rule_uses_canonical_flag_parsing(
    offline_route_boundaries, disabled_value
):
    state = offline_route_boundaries
    state.config = {
        "model": {"provider": "openai", "openai_runtime": "codex_app_server"},
        "providers": {"openai": {"enabled": disabled_value}},
    }

    with pytest.raises(ValueError, match="provider 'openai' is disabled"):
        rp.resolve_runtime_provider(requested="auto")

    assert state.calls == []


@pytest.mark.parametrize("block", [None, "malformed", []])
def test_native_enable_rule_preserves_canonical_non_dict_block_behavior(
    offline_route_boundaries, block
):
    state = offline_route_boundaries
    state.config = {
        "model": {"provider": "openai", "openai_runtime": "codex_app_server"},
        "providers": {"openai": block},
    }

    result = rp.resolve_runtime_provider(requested="auto")

    assert result["provider"] == "openai"
    assert result["api_mode"] == "codex_app_server"
    assert result["credential_pool"] is None
    assert state.calls == []


@pytest.mark.parametrize("provider", ["anthropic", "openrouter", "ollama-cloud"])
@pytest.mark.parametrize("disabled_provider", ["openai", "openai-codex"])
def test_disabled_configured_native_does_not_block_explicit_non_native_route(
    offline_route_boundaries, provider, disabled_provider
):
    state = offline_route_boundaries
    state.provider = provider
    state.config = {
        "model": {"provider": disabled_provider, "default": "offline-model",
                  "openai_runtime": "codex_app_server"},
        "providers": {disabled_provider: {"enabled": False}},
    }

    result = rp.resolve_runtime_provider(requested=provider)

    assert result["provider"] == result["requested_provider"] == provider
    assert result["api_mode"] != "codex_app_server"
    assert result["credential_pool"] is state.pool
    assert result["api_key"] == "offline-dummy-key"


@pytest.mark.parametrize("provider", ["openai", "openai-codex"])
def test_explicit_native_override_checks_its_own_enable_rule(
    offline_route_boundaries, provider
):
    state = offline_route_boundaries
    other = "openai-codex" if provider == "openai" else "openai"
    state.config = {
        "model": {"provider": other, "openai_runtime": "codex_app_server"},
        "providers": {other: {"enabled": False}, provider: {"enabled": True}},
    }

    result = rp.resolve_runtime_provider(requested=provider)

    assert result["provider"] == result["requested_provider"] == provider
    assert result["api_mode"] == "codex_app_server"
    assert result["credential_pool"] is None
    assert state.calls == []


@pytest.mark.parametrize("route", ["key", "url", "config-url"])
def test_explicit_api_values_cannot_reenable_a_selected_disabled_native_route(
    offline_route_boundaries, route
):
    state = offline_route_boundaries
    state.config = {
        "model": {"provider": "openai", "openai_runtime": "codex_app_server"},
        "providers": {"openai": {"enabled": False}},
    }
    kwargs = {}
    if route == "key":
        kwargs["explicit_api_key"] = "offline-caller-key"
    elif route == "url":
        kwargs["explicit_base_url"] = "https://offline.invalid/v1"
    else:
        state.config["model"]["base_url"] = "https://offline.invalid/v1"

    with pytest.raises(ValueError, match="provider 'openai' is disabled"):
        rp.resolve_runtime_provider(requested="auto", **kwargs)

    assert state.calls == []


def test_disabled_native_guard_preserves_continuity_endpoint_refusal(
    offline_route_boundaries,
):
    state = offline_route_boundaries
    state.config = {
        "model": {"provider": "openai", "openai_runtime": "codex_app_server"},
        "providers": {"openai": {"enabled": False}},
        "compression": {"context_rebase_enabled": True},
    }

    with pytest.raises(ValueError, match="does not accept explicit API credentials"):
        rp.resolve_runtime_provider(requested="auto", explicit_api_key="offline-key")

    assert state.calls == []
