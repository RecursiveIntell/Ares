"""Ares engine selection and resumed lineage through the real host initializer.

All provider clients, activation probes and governor commands are fakes. The
session ancestry is read from an isolated real SessionDB, not an adapter stub.
"""

import importlib
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB
from plugins.context_engine import ContextEngineActivationError
from agent.context_compressor import ContextCompressor


@pytest.fixture
def governor(tmp_path):
    engine_type = importlib.import_module(
        "plugins.context_engine.ri-context-governor"
    ).RiContextGovernorEngine
    with patch("hermes_cli.config.load_config", return_value={}):
        engine = engine_type(
            binary=str(tmp_path / "fake-governor"),
            store_dir=str(tmp_path / "governor-store"),
        )
    engine.probe_activation = MagicMock(return_value={"verified": True})
    engine._run_certified_json = MagicMock(return_value=[])
    engine._load_prior_session_context = MagicMock()
    return engine


@contextmanager
def _host_init(engine, *, engine_name="ri-context-governor"):
    cfg = {
        "context": {"engine": engine_name},
        "compression": {"threshold": 0.75},
        "agent": {"environment_probe": False},
    }
    with ExitStack() as stack:
        for target, value in (
            ("hermes_cli.config.load_config", cfg),
            ("hermes_cli.config.load_config_readonly", cfg),
            ("plugins.context_engine._load_engine_from_dir", engine),
            ("agent.model_metadata.get_model_context_length", 64_000),
            ("agent.context_compressor.get_model_context_length", 64_000),
            ("run_agent.get_tool_definitions", []),
            ("run_agent.check_toolset_requirements", {}),
        ):
            stack.enter_context(patch(target, return_value=value))
        stack.enter_context(patch("run_agent.OpenAI"))
        if engine_name != "ri-context-governor":
            stack.enter_context(
                patch("plugins.context_engine.load_context_engine", return_value=engine)
            )
        from run_agent import AIAgent

        def create(**kwargs):
            options = dict(
                model="test-model",
                provider="openrouter",
                api_key="fake-provider-key",
                base_url="https://provider.invalid/v1",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                skip_background_review=True,
            )
            options.update(kwargs)
            return AIAgent(**options)

        yield create


def test_explicit_ares_selection_refuses_missing_engine():
    with (
        _host_init(None) as create,
        patch("agent.agent_init.ContextCompressor", wraps=ContextCompressor) as stock,
        patch("hermes_cli.plugins.get_plugin_context_engine") as optional,
    ):
        with pytest.raises(ContextEngineActivationError, match="instantiated"):
            create()
    stock.assert_not_called()
    optional.assert_not_called()


@pytest.mark.parametrize(
    ("stage", "method", "message"),
    [
        ("probe", "probe_activation", "activation probe"),
        ("binding", "bind_session_state", "session binding"),
        ("startup", "on_session_start", "session start"),
    ],
)
def test_explicit_ares_selection_surfaces_startup_failure(
    governor, stage, method, message
):
    setattr(governor, method, MagicMock(side_effect=RuntimeError(f"{stage} failed")))
    with (
        _host_init(governor) as create,
        patch("agent.agent_init.ContextCompressor") as stock,
    ):
        with pytest.raises(ContextEngineActivationError, match=message) as failure:
            create()
    assert isinstance(failure.value.__cause__, RuntimeError)
    stock.assert_not_called()


def test_explicit_ares_selection_probes_before_session_binding(governor):
    events = []
    governor.probe_activation.side_effect = lambda: events.append("probe")
    original_bind = governor.bind_session_state

    def bind(**kwargs):
        events.append("bind")
        return original_bind(**kwargs)

    governor.bind_session_state = bind
    with _host_init(governor) as create:
        agent = create(session_id="new-session", max_tokens=4096)
    assert agent.context_compressor is governor
    assert events[0:2] == ["probe", "bind"]
    governor.probe_activation.assert_called_once()
    assert governor.max_tokens == 4096
    assert governor.threshold_tokens == int((64_000 - 4096) * 0.75)


@pytest.mark.parametrize(
    ("end_reason", "model_config", "expected_root"),
    [
        ("compression", {}, "root"),
        ("compression", {"_branched_from": "root"}, "tip"),
        ("compression", {"_delegate_from": "root"}, "tip"),
        (
            "context_rebase",
            {
                "_context_rebase_from": "root",
                "_context_rebase_transition": "transition-fixture",
                "_context_epoch": 1,
            },
            "root",
        ),
        ("context_rebase", {}, "tip"),
    ],
)
def test_session_start_keeps_the_sessiondb_lineage(
    governor, tmp_path, end_reason, model_config, expected_root
):
    db = SessionDB(tmp_path / "session-state.db")
    try:
        db.create_session("root", "cli")
        db.end_session("root", end_reason)
        db.create_session(
            "tip", "cli", parent_session_id="root", model_config=model_config
        )
        with _host_init(governor) as create:
            create(session_id="tip", session_db=db)
        assert governor.session_id == "tip"
        assert governor._session_db is db
        assert governor._governor_session_id() == expected_root
    finally:
        db.close()


def test_optional_engine_missing_still_uses_stock_compressor():
    with (
        _host_init(None, engine_name="optional-engine") as create,
        patch("hermes_cli.plugins.get_plugin_context_engine", return_value=None),
    ):
        agent = create()
    from agent.context_compressor import ContextCompressor

    assert isinstance(agent.context_compressor, ContextCompressor)


def test_optional_engine_startup_remains_best_effort(governor):
    governor.probe_activation.side_effect = AssertionError("must not strict-probe")
    governor.bind_session_state = MagicMock(side_effect=RuntimeError("optional bind"))
    governor.on_session_start = MagicMock(side_effect=RuntimeError("optional start"))
    with _host_init(governor, engine_name="optional-engine") as create:
        agent = create()
    assert agent.context_compressor is governor
    governor.probe_activation.assert_not_called()
    assert "session_db" not in governor.on_session_start.call_args.kwargs


def test_manual_model_switch_keeps_the_output_reserve(governor):
    governor._policy["token_budget"] = 128_000
    with _host_init(governor) as create:
        agent = create(max_tokens=4096)
        with patch("agent.model_metadata.get_model_context_length", return_value=32_000):
            agent.switch_model(
                "switched-model",
                "openrouter",
                api_key="fake-provider-key",
                base_url="https://provider.invalid/v1",
            )
    assert governor.model == "switched-model"
    assert governor.max_tokens == 4096
    assert governor.context_length == 32_000
    assert governor._target_tokens(100_000) == 27_904


def test_fallback_and_primary_restore_keep_the_output_reserve(governor):
    governor._policy["token_budget"] = 128_000
    fallback_client = MagicMock()
    fallback_client.base_url = "https://api.openai.com/v1"
    fallback_client.api_key = "fake-fallback-key"
    with _host_init(governor) as create:
        agent = create(max_tokens=4096)
        agent._fallback_chain = [{"provider": "openai", "model": "fallback-model"}]
        agent._fallback_model = agent._fallback_chain[0]
        with (
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(fallback_client, None),
            ),
            patch("agent.model_metadata.get_model_context_length", return_value=32_000),
        ):
            assert agent._try_activate_fallback() is True
        assert governor.model == "fallback-model"
        assert governor.max_tokens == 4096
        assert governor._target_tokens(100_000) == 27_904
        assert agent._restore_primary_runtime() is True
    assert governor.model == "test-model"
    assert governor.max_tokens == 4096
    assert governor.context_length == 64_000
    assert governor._target_tokens(100_000) == 59_904
