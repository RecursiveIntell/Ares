"""An Ares budget refusal must leave the owning host on its previous route.

These follow-up counterexamples use real AIAgent handoffs and the real governor
budget validator. Provider clients, metadata, activation and native commands are
fakes; no conversation or provider request runs.
"""

from unittest.mock import MagicMock, patch

import pytest

from agent.error_classifier import FailoverReason
from tests.run_agent.test_ares_context_initialization import _host_init, governor


_HOST_FIELDS = (
    "model", "provider", "requested_provider", "base_url", "_base_url_lower",
    "api_mode", "api_key", "client", "_anthropic_client", "_anthropic_api_key",
    "_anthropic_base_url", "_is_anthropic_oauth", "_client_kwargs",
    "_credential_pool", "_credential_pool_entry_id", "_config_context_length",
    "_custom_providers", "_reasoning_echo_flag", "_use_prompt_caching",
    "_use_native_cache_layout", "_transport_cache", "_fallback_activated",
    "_primary_runtime", "_cached_system_prompt", "reasoning_config",
    "_provider_fallback_active", "_provider_fallback_route",
    "_pending_fallback_notice",
)
_ENGINE_FIELDS = (
    "model", "provider", "base_url", "api_key", "api_mode", "context_length",
    "max_tokens", "threshold_tokens", "threshold_percent", "protect_first_n",
    "protect_last_n", "session_id", "_lineage_session_id", "_session_db",
    "_pending_admission", "last_receipt_id", "compression_count",
)


@pytest.fixture(autouse=True)
def _fake_catalog_prewarm():
    # Initializer prewarming uses an imported alias in its background thread.
    # Both metadata entry points stay fake, in addition to the worker guard.
    with (
        patch("agent.agent_init.fetch_model_metadata", return_value={}),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        yield


def _state(obj, fields):
    values = {}
    for name in fields:
        if not hasattr(obj, name):
            continue
        value = getattr(obj, name)
        if isinstance(value, dict):
            value = dict(value)
        elif isinstance(value, list):
            value = list(value)
        values[name] = value
    return values


def _sentinel_runtime(agent):
    # Make silent collateral changes observable, including in-place cache
    # clearing and re-reading a custom-provider snapshot during a switch.
    agent._transport_cache = {"previous-route": object()}
    agent._cached_system_prompt = "stable previous prompt"
    agent._credential_pool = MagicMock(provider=agent.provider)
    agent._credential_pool_entry_id = "previous-credential"
    agent._custom_providers = [{"name": "previous-route"}]
    agent._use_prompt_caching = True
    agent._use_native_cache_layout = True
    agent._config_context_length = 64_000


def _fallback_client():
    client = MagicMock(name="FakeFallbackClient")
    client.base_url = "https://api.openai.com/v1"
    client.api_key = "fake-fallback-key"
    return client


@pytest.mark.parametrize("api_mode", ["chat_completions", "anthropic_messages"])
def test_refused_manual_switch_preserves_host_and_engine(governor, api_mode):
    with _host_init(governor) as create:
        agent = create(max_tokens=4096)
        _sentinel_runtime(agent)
        before_host = _state(agent, _HOST_FIELDS)
        before_engine = _state(governor, _ENGINE_FIELDS)
        previous_client = agent.client
        previous_pool = agent._credential_pool
        previous_cache = agent._transport_cache
        new_client = MagicMock(name="RefusedClient")
        retire = MagicMock()
        with (
            patch("agent.model_metadata.get_model_context_length", return_value=4096),
            patch("agent.credential_pool.load_pool", return_value=None),
            patch("agent.anthropic_adapter.build_anthropic_client", return_value=new_client),
            patch.object(agent, "_create_openai_client", return_value=new_client),
            patch.object(agent, "_retire_shared_openai_client", retire),
        ):
            with pytest.raises(ValueError, match="no input budget"):
                agent.switch_model(
                    "refused-model", "openai", api_key="fake-new-key",
                    base_url="https://api.openai.com/v1", api_mode=api_mode,
                )
        assert _state(agent, _HOST_FIELDS) == before_host
        assert _state(governor, _ENGINE_FIELDS) == before_engine
        assert agent.client is previous_client
        assert agent._credential_pool is previous_pool
        assert agent._transport_cache is previous_cache
        retire.assert_called_once_with(new_client, reason="context_handoff_refused")
        previous_client.close.assert_not_called()
        new_client.close.assert_not_called()
        new_client.chat.completions.create.assert_not_called()
        new_client.messages.create.assert_not_called()


@pytest.mark.parametrize("already_on_fallback", [False, True])
def test_refused_exhausted_fallback_preserves_active_runtime(governor, already_on_fallback):
    with _host_init(governor) as create:
        agent = create(max_tokens=4096)
        if already_on_fallback:
            agent._fallback_chain = [{"provider": "openai", "model": "previous-fallback"}]
            with (
                patch("agent.auxiliary_client.resolve_provider_client", return_value=(_fallback_client(), None)),
                patch("agent.model_metadata.get_model_context_length", return_value=32_000),
                patch("agent.credential_pool.load_pool", return_value=None),
            ):
                assert agent._try_activate_fallback() is True
            agent._fallback_index = 0
        _sentinel_runtime(agent)
        agent._fallback_chain = [{"provider": "openai", "model": "refused-fallback"}]
        agent._fallback_model = agent._fallback_chain[0]
        before_host = _state(agent, _HOST_FIELDS)
        before_engine = _state(governor, _ENGINE_FIELDS)
        fallback_client = _fallback_client()
        with (
            patch("agent.auxiliary_client.resolve_provider_client", return_value=(fallback_client, None)),
            patch("agent.model_metadata.get_model_context_length", return_value=4096),
            patch("agent.credential_pool.load_pool", return_value=None),
        ):
            assert agent._try_activate_fallback() is False
        assert _state(agent, _HOST_FIELDS) == before_host
        assert _state(governor, _ENGINE_FIELDS) == before_engine
        # Preserve intentional chain progress and exhaustion cooldown. Rolling
        # those back would retry the same rejected route indefinitely.
        assert agent._fallback_index == 1
        assert agent._rate_limited_until > 0
        fallback_client.responses.create.assert_not_called()


def test_refused_fallback_advances_to_a_valid_candidate(governor):
    with _host_init(governor) as create:
        agent = create(max_tokens=4096)
        agent._fallback_chain = [
            {"provider": "openai", "model": "refused-fallback"},
            {"provider": "openai", "model": "valid-fallback"},
        ]
        clients = [_fallback_client(), _fallback_client()]
        with (
            patch("agent.auxiliary_client.resolve_provider_client", side_effect=[(c, None) for c in clients]),
            patch("agent.model_metadata.get_model_context_length", side_effect=[4096, 32_000]),
            patch("agent.credential_pool.load_pool", return_value=None),
        ):
            assert agent._try_activate_fallback() is True
        assert agent.model == governor.model == "valid-fallback"
        assert agent.client is clients[1]
        assert governor.context_length == 32_000
        assert governor.max_tokens == 4096
        assert agent._fallback_index == 2
        assert agent._provider_fallback_route == ("valid-fallback", "openai")
        assert len(agent._pending_fallback_notice) == 1
        assert "test-model via openrouter" in agent._pending_fallback_notice[0]
        assert "using valid-fallback" in agent._pending_fallback_notice[0]


@pytest.mark.parametrize(
    "reason", [FailoverReason.rate_limit, FailoverReason.billing, FailoverReason.upstream_rate_limit]
)
@pytest.mark.parametrize("has_valid_tail", [False, True])
def test_one_primary_failure_arms_backoff_once_through_refused_routes(
    governor, reason, has_valid_tail
):
    with _host_init(governor) as create:
        agent = create(max_tokens=4096)
        agent._fallback_chain = [{"provider": "openai", "model": "refused-fallback"}]
        if has_valid_tail:
            agent._fallback_chain.append({"provider": "openai", "model": "valid-fallback"})
        clients = [_fallback_client() for _ in agent._fallback_chain]
        windows = [4096, 32_000] if has_valid_tail else [4096]
        before_host = _state(agent, _HOST_FIELDS)
        before_engine = _state(governor, _ENGINE_FIELDS)
        with (
            patch("agent.chat_completion_helpers.time.monotonic", return_value=1000.0),
            patch("agent.auxiliary_client.resolve_provider_client", side_effect=[(c, None) for c in clients]),
            patch("agent.model_metadata.get_model_context_length", side_effect=windows),
            patch("agent.credential_pool.load_pool", return_value=None),
        ):
            assert agent._try_activate_fallback(reason) is has_valid_tail
            assert agent._rate_limit_backoff_count == 1
            assert agent._rate_limited_until == 1060.0
            if has_valid_tail:
                assert agent.model == governor.model == "valid-fallback"
                assert agent._fallback_index == 2
            else:
                assert _state(agent, _HOST_FIELDS) == before_host
                assert _state(governor, _ENGINE_FIELDS) == before_engine
                assert agent._fallback_index == 1
                # A distinct originating primary failure still advances the
                # backoff even with an already exhausted fallback chain.
                assert agent._try_activate_fallback(reason) is False
                assert agent._rate_limit_backoff_count == 2
                assert agent._rate_limited_until == 1120.0


def test_refused_primary_restore_preserves_fallback_then_allows_retry(governor):
    with _host_init(governor) as create:
        agent = create(max_tokens=4096)
        agent._fallback_chain = [{"provider": "openai", "model": "valid-fallback"}]
        agent._fallback_model = agent._fallback_chain[0]
        fallback_client = _fallback_client()
        with (
            patch("agent.auxiliary_client.resolve_provider_client", return_value=(fallback_client, None)),
            patch("agent.model_metadata.get_model_context_length", return_value=32_000),
            patch("agent.credential_pool.load_pool", return_value=None),
        ):
            assert agent._try_activate_fallback() is True
        _sentinel_runtime(agent)
        before_host = _state(agent, _HOST_FIELDS)
        before_engine = _state(governor, _ENGINE_FIELDS)
        original_window = agent._primary_runtime["compressor_context_length"]
        agent._primary_runtime["compressor_context_length"] = 4096
        # The saved intended destination may be invalid after reserve changes;
        # the active fallback must remain internally coherent on refusal.
        before_host["_primary_runtime"]["compressor_context_length"] = 4096
        assert agent._restore_primary_runtime() is False
        assert _state(agent, _HOST_FIELDS) == before_host
        assert _state(governor, _ENGINE_FIELDS) == before_engine
        assert agent.client is fallback_client
        agent._primary_runtime["compressor_context_length"] = original_window
        assert agent._restore_primary_runtime() is True
        assert agent.model == governor.model == "test-model"
        assert agent._fallback_activated is False
        assert agent._provider_fallback_active is False
        assert governor.context_length == original_window
        assert governor.max_tokens == 4096


@pytest.mark.parametrize("missing_field", ["compressor_model", "compressor_context_length"])
@pytest.mark.parametrize("falsey_engine", [False, True])
def test_active_engine_requires_restore_snapshot_fields(
    governor, monkeypatch, missing_field, falsey_engine
):
    with _host_init(governor) as create:
        agent = create(max_tokens=4096)
        agent._fallback_chain = [{"provider": "openai", "model": "valid-fallback"}]
        with (
            patch("agent.auxiliary_client.resolve_provider_client", return_value=(_fallback_client(), None)),
            patch("agent.model_metadata.get_model_context_length", return_value=32_000),
            patch("agent.credential_pool.load_pool", return_value=None),
        ):
            assert agent._try_activate_fallback() is True
        if falsey_engine:
            monkeypatch.setattr(type(governor), "__bool__", lambda self: False, raising=False)
        saved_value = agent._primary_runtime.pop(missing_field)
        before_host = _state(agent, _HOST_FIELDS)
        before_engine = _state(governor, _ENGINE_FIELDS)
        previous_client = agent.client
        assert agent._restore_primary_runtime() is False
        assert _state(agent, _HOST_FIELDS) == before_host
        assert _state(governor, _ENGINE_FIELDS) == before_engine
        assert agent.client is previous_client
        agent._primary_runtime[missing_field] = saved_value
        assert agent._restore_primary_runtime() is True
        assert agent.model == governor.model == "test-model"
        assert agent._fallback_activated is False
