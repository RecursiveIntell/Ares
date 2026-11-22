"""Required context-engine activation must never silently select another engine.

These tests use the real agent selector and stand-in plugin registry methods.
No external service, provider, database, or live HERMES_HOME is involved.
"""

from unittest.mock import patch

import pytest

from agent.agent_init import _select_context_engine


def _select(name="ctx_required", *, required=True):
    return {"context": {"engine": name, "engine_required": required}}


def test_required_missing_engine_refuses_builtin_fallback():
    with (
        patch("agent.safe_worker_policy.safe_worker_enabled", return_value=False),
        patch("plugins.context_engine.load_context_engine", return_value=None),
        patch("hermes_cli.plugins.get_plugin_context_engine", return_value=None),
        pytest.raises(RuntimeError, match="Required context engine .* unavailable"),
    ):
        _select_context_engine(_select())


def test_optional_missing_engine_keeps_current_fallback():
    with (
        patch("agent.safe_worker_policy.safe_worker_enabled", return_value=False),
        patch("plugins.context_engine.load_context_engine", return_value=None),
        patch("hermes_cli.plugins.get_plugin_context_engine", return_value=None),
    ):
        assert _select_context_engine(_select(required=False)) is None


def test_required_cloning_failure_refuses_builtin_fallback():
    class Plugin:
        name = "ctx_required"

        def clone_for_agent(self):
            raise TypeError("cannot clone")

    with (
        patch("agent.safe_worker_policy.safe_worker_enabled", return_value=False),
        patch("plugins.context_engine.load_context_engine", return_value=None),
        patch("hermes_cli.plugins.get_plugin_context_engine", return_value=Plugin()),
        pytest.raises(RuntimeError, match="Required context engine .* unavailable"),
    ):
        _select_context_engine(_select())


def test_required_valid_plugin_is_selected():
    class Plugin:
        name = "ctx_required"

    instance = Plugin()
    with (
        patch("agent.safe_worker_policy.safe_worker_enabled", return_value=False),
        patch("plugins.context_engine.load_context_engine", return_value=instance),
    ):
        assert _select_context_engine(_select()) is instance


def test_required_wrong_name_cannot_take_over():
    class Plugin:
        name = "another_engine"

    with (
        patch("agent.safe_worker_policy.safe_worker_enabled", return_value=False),
        patch("plugins.context_engine.load_context_engine", return_value=Plugin()),
        pytest.raises(RuntimeError, match="resolved to 'another_engine'"),
    ):
        _select_context_engine(_select())


def test_required_plugin_is_not_silently_disabled_for_safe_worker():
    with (
        patch("agent.safe_worker_policy.safe_worker_enabled", return_value=True),
        pytest.raises(RuntimeError, match="safe-worker mode"),
    ):
        _select_context_engine(_select())


def test_required_builtin_engine_remains_usable():
    with patch("agent.safe_worker_policy.safe_worker_enabled", return_value=False):
        assert _select_context_engine(_select("compressor")) is None


@pytest.mark.parametrize("value", ["true", "false", 1, None, [], {}])
def test_malformed_required_flag_is_rejected(value):
    with pytest.raises(ValueError, match="must be a boolean"):
        _select_context_engine({"context": {"engine": "ctx_required", "engine_required": value}})
