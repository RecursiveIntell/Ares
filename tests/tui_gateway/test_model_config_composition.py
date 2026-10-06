"""Real gateway configuration boundaries; external clients remain inert."""
import copy
import threading
from types import SimpleNamespace

import pytest

from agent.context_engine import ContextEngine
from hermes_cli.config import load_config
from tests.hermes_cli.test_model_switch_route_contract import routes  # noqa: F401
from tui_gateway import server
from tests.tui_gateway.test_failed_turn_retention import _session, turn_env  # noqa: F401

_REAL_MODEL_SYNC = server._sync_agent_model_with_config


class InertEngine(ContextEngine):
    @property
    def name(self):
        return "composition-inert"

    def update_from_response(self, usage):
        pass

    def should_compress(self, prompt_tokens=None):
        return False

    def compress(self, messages, current_tokens=None):
        return messages


@pytest.fixture
def world(routes, monkeypatch):
    routes.config["context"] = {"engine": "composition-inert"}
    routes.config["agent"] = {"reasoning_overrides": {"chosen-model": "high"}}
    routes.write()
    monkeypatch.setenv("HERMES_IGNORE_RULES", "1")
    monkeypatch.setattr(server, "_load_cfg", load_config)
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda *a: [])
    monkeypatch.setattr(server, "_load_fallback_model", lambda: [])
    monkeypatch.setattr(server, "_load_service_tier", lambda: None)
    monkeypatch.setattr(server, "_load_provider_routing", lambda: {})
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr("hermes_cli.mcp_startup.wait_for_mcp_discovery", lambda: None)
    monkeypatch.setattr("tui_gateway.entry.wait_for_mcp_discovery", lambda: None)
    monkeypatch.setattr("plugins.context_engine.load_context_engine", lambda *a, **k: InertEngine())
    monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *a, **k: 128_000)
    monkeypatch.setattr("run_agent.get_tool_definitions", lambda *a, **k: [])
    monkeypatch.setattr("run_agent.check_toolset_requirements", lambda *a, **k: {})
    monkeypatch.setattr("run_agent.OpenAI", lambda **k: SimpleNamespace(kwargs=k))
    monkeypatch.setattr("hermes_cli.timeouts.get_provider_request_timeout", lambda *a, **k: None)
    monkeypatch.setattr(server, "_restart_slash_worker", lambda *a: None)
    monkeypatch.setattr(server, "_persist_live_session_runtime", lambda *a: None)
    monkeypatch.setattr(server, "_persist_live_session_system_prompt", lambda *a: None)
    monkeypatch.setattr(server, "_probe_credentials", lambda *a: None)
    monkeypatch.setattr(server, "_git_branch_for_cwd", lambda *a: None)
    monkeypatch.setattr(server, "_project_info_for_cwd", lambda *a: None)
    monkeypatch.setattr("hermes_cli.banner.get_available_skills", lambda: {})
    monkeypatch.setattr("hermes_cli.banner.get_update_result", lambda **k: None)
    monkeypatch.setattr("tools.mcp_tool.get_mcp_status", lambda: [])
    agents = []

    def build(override=None):
        value = server._make_agent("selected", "inert-session", model_override=override)
        value._create_openai_client = lambda kwargs, **k: SimpleNamespace(kwargs=kwargs.copy())
        agents.append(value)
        return value

    yield SimpleNamespace(routes=routes, build=build)
    server._sessions.clear()
    for value in agents:
        db = getattr(value, "_session_db", None)
        if db is not None and getattr(value, "_owns_session_db", False):
            db.close()


def outgoing(value):
    return value._build_api_kwargs([{"role": "user", "content": "inert input"}], [])


def selected_session(value, monkeypatch):
    session = {"agent": value, "history": [], "history_lock": threading.Lock(),
               "model_override": {"model": "model-a", "provider": "endpoint-a"},
               "model_verified_for": ("endpoint-a", "model-a")}
    monkeypatch.setattr(server, "_sessions", {"selected": session})
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    return session


@pytest.mark.parametrize("actual", [None, {"enabled": False}, {"enabled": True, "effort": "medium"}])
def test_once_restores_actual_reasoning_and_primary(world, monkeypatch, actual):
    value = world.build()
    # A is normally initialized, without the preparatory switch used by older
    # runtime fixtures. A manual session value may differ from primary state.
    assert "reasoning_config" not in value._primary_runtime
    assert value.reasoning_config is None
    value.reasoning_config = copy.deepcopy(actual)
    session = selected_session(value, monkeypatch)
    pin = copy.deepcopy(session["model_override"])
    result = server._apply_model_switch("selected", session,
        "chosen-model --provider endpoint-b --once", confirm_expensive_model=True,
        persist_override=False)
    assert result["scope"] == "once"
    assert value.reasoning_config == {"enabled": True, "effort": "high"}
    server._restore_agent_model_runtime(value, session.pop("one_turn_model_restore"))
    assert (value.model, value.base_url) == ("model-a", "http://a.invalid/v1")
    assert value.reasoning_config == actual
    assert value._primary_runtime["reasoning_config"] == actual
    assert session["model_override"] == pin
    # A subsequent primary retry must preserve the restored session setting.
    value._fallback_activated = True
    assert value._restore_primary_runtime()
    assert value.reasoning_config == actual


def test_snapshot_copies_actual_manual_reasoning(world):
    value = world.build()
    value.reasoning_config = {"enabled": True, "effort": "medium"}
    value._primary_runtime["reasoning_config"] = {"enabled": True, "effort": "low"}
    snapshot = server._snapshot_agent_model_runtime(value)
    value.reasoning_config["effort"] = "high"
    assert snapshot["reasoning_config"] == {"enabled": True, "effort": "medium"}
    assert snapshot["primary_runtime"]["reasoning_config"] == {"enabled": True, "effort": "medium"}


@pytest.mark.parametrize("setting,expected", [
    (None, None), ("none", {"enabled": False}),
    ("medium", {"enabled": True, "effort": "medium"}),
])
@pytest.mark.parametrize("outcome", ["complete", "returned_error", "raised_error", "interrupted"])
def test_real_pinned_once_turn_restores_session_reasoning(world, monkeypatch, setting, expected, outcome):
    value = world.build()
    assert value.reasoning_config is None
    # Initialize the real agent before this fixture substitutes inline turn
    # threads; the shared Thread substitution would deadlock QueueListener.
    turn_env.__wrapped__(monkeypatch, world.routes.home)
    session = selected_session(value, monkeypatch)
    session.update(_session(value, session_key=value.session_id, running=True,
                            profile_home=str(world.routes.home), model_override=session["model_override"]))
    monkeypatch.setattr(server, "_sync_agent_model_with_config", _REAL_MODEL_SYNC)
    monkeypatch.setattr(server, "_sync_agent_compression_with_config", lambda *a: None)
    monkeypatch.setattr(server, "_voice_tts_enabled", lambda: False)
    monkeypatch.setattr(server, "_load_interim_assistant_messages", lambda: False)
    monkeypatch.setattr(server, "_start_usage_ticker", lambda *a: (threading.Event(), SimpleNamespace(join=lambda *a, **k: None)))
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *a: None)
    monkeypatch.setattr(server, "record_turn_start", lambda *a, **k: None)
    monkeypatch.setattr(server, "_retire_turn_marker", lambda *a: None)
    if setting is not None:
        ack = server._methods["config.set"]("reasoning-request", {
            "key": "reasoning", "value": setting, "session_id": "selected", "scope": "session"})
        assert "error" not in ack
    assert value.reasoning_config == expected
    pin = copy.deepcopy(session["model_override"])
    original_config = (world.routes.home / "config.yaml").read_bytes()
    selected = server._apply_model_switch("selected", session,
        "chosen-model --provider endpoint-b --once", confirm_expensive_model=True,
        persist_override=False)
    assert selected["scope"] == "once"
    observations = []

    def offline(*a, **k):
        observations.append((value.model, copy.deepcopy(value.reasoning_config)))
        assert "one_turn_model_restore" not in session
        if outcome == "raised_error":
            raise RuntimeError("inert conversation failure")
        if outcome == "returned_error":
            return {"error": "inert returned failure", "final_response": "", "completed": False, "api_calls": 0}
        return {"final_response": "inert", "completed": False, "api_calls": 0,
                "interrupted": outcome == "interrupted"}

    monkeypatch.setattr(value, "run_conversation", offline)
    server._run_prompt_submit("once-request", "selected", session, "inert input")
    assert observations == [("chosen-model", {"enabled": True, "effort": "high"})]
    assert (value.model, value.base_url) == ("model-a", "http://a.invalid/v1")
    assert value.reasoning_config == expected
    assert value._primary_runtime["reasoning_config"] == expected
    assert session["model_override"] == pin
    assert not session.get("_one_turn_model_runtime")
    assert (world.routes.home / "config.yaml").read_bytes() == original_config


def test_failed_route_restore_does_not_apply_saved_reasoning(world):
    value = world.build()
    value.reasoning_config = None
    snapshot = server._snapshot_agent_model_runtime(value)
    value.reasoning_config = {"enabled": True, "effort": "high"}
    value._restore_primary_runtime = lambda: False

    def refuse(**kw):
        raise ValueError("inert refused route")

    value.switch_model = refuse
    with pytest.raises(ValueError, match="inert refused route"):
        server._restore_agent_model_runtime(value, snapshot)
    assert value.reasoning_config == {"enabled": True, "effort": "high"}


def test_legacy_snapshot_without_reasoning_keeps_legacy_restore(world):
    value = world.build()
    snapshot = server._snapshot_agent_model_runtime(value)
    snapshot.pop("reasoning_config", None)
    snapshot["primary_runtime"].pop("reasoning_config", None)
    value.reasoning_config = {"enabled": True, "effort": "high"}
    server._restore_agent_model_runtime(value, snapshot)
    assert value.reasoning_config == {"enabled": True, "effort": "high"}


@pytest.mark.parametrize("global_cap,provider_cap,expected", [
    (None, 7, 7), (11, 7, 11), ("11", 7, 11),
    (None, None, None), (None, 0, None), (None, -1, None),
    (None, "7", None), (None, True, None),
    (0, 7, None), (False, 7, None), ("invalid", 7, None),
])
def test_actual_factory_provider_global_cap_precedence(world, global_cap, provider_cap, expected):
    cfg = world.routes.config
    cfg["model"]["max_tokens"] = global_cap
    cfg["providers"]["endpoint-a"]["max_output_tokens"] = provider_cap
    world.routes.write()
    from hermes_cli.runtime_provider import resolve_runtime_provider
    resolved = resolve_runtime_provider(requested="endpoint-a", target_model="model-a")
    expected_resolved = provider_cap if isinstance(provider_cap, int) and provider_cap > 0 else None
    assert resolved.get("max_output_tokens") == expected_resolved, resolved
    value = world.build()
    assert value.max_tokens == expected
    kwargs = outgoing(value)
    if expected is not None:
        for key, amount in value._max_tokens_param(expected).items():
            assert kwargs[key] == amount


def test_fresh_pinned_provider_caps_remain_independent(world):
    cfg = world.routes.config
    cfg["providers"]["endpoint-a"]["max_output_tokens"] = 7
    cfg["providers"]["endpoint-b"]["max_output_tokens"] = 13
    world.routes.write()
    a = world.build({"model": "model-a", "provider": "endpoint-a"})
    b = world.build({"model": "model-b", "provider": "endpoint-b"})
    assert (a.max_tokens, b.max_tokens) == (7, 13)
    assert (a.base_url, b.base_url) == ("http://a.invalid/v1", "http://b.invalid/v1")


def test_clear_global_then_provider_cap_at_fresh_construction(world):
    cfg = world.routes.config
    cfg["model"]["max_tokens"] = 11
    cfg["providers"]["endpoint-a"]["max_output_tokens"] = 7
    world.routes.write()
    assert world.build().max_tokens == 11
    cfg["model"].pop("max_tokens")
    world.routes.write()
    assert world.build().max_tokens == 7
    cfg["providers"]["endpoint-a"].pop("max_output_tokens")
    world.routes.write()
    assert world.build().max_tokens is None
