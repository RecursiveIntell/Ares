"""Gateway policy survives schema construction; no provider or handler runs.

The constructor substitute assembles schemas with the real model_tools path.
The group-member test also uses the real session.create and deferred builder,
with a temporary profile. Inference, MCP discovery, notification, and approval
side effects are inert. These tests do not qualify native-provider execution or
post-construction memory/context-engine schema injection.
"""

from types import SimpleNamespace

import pytest


@pytest.fixture
def inert_surface(monkeypatch):
    import model_tools
    import toolsets
    from tools.registry import registry

    monkeypatch.setattr(registry, "_tools", {})
    monkeypatch.setattr(registry, "_scoped_tools", {})
    monkeypatch.setattr(registry, "_generation", registry._generation)
    monkeypatch.setattr(toolsets, "_resolve_toolset_memo", {})
    names = {
        "policy_canary_allowed": "policy_canary_keep",
        "policy_canary_forbidden": "policy_canary_deny",
        "policy_canary_mcp": "mcp_policy_canary",
    }
    for name, toolset in names.items():
        def handler(*args, **kwargs):
            pytest.fail("an inert canary handler must never execute")

        registry.register(
            name=name, toolset=toolset,
            schema={"name": name, "description": "Inert policy witness",
                    "parameters": {"type": "object", "properties": {}}},
            handler=handler, check_fn=lambda: True,
        )
    monkeypatch.setitem(toolsets.TOOLSETS, "policy_canary_composite", {
        "tools": [], "includes": list(names.values()),
    })
    return model_tools


@pytest.fixture
def construction(monkeypatch, inert_surface):
    import run_agent
    from tui_gateway import server
    import agent.coding_context
    import hermes_cli.mcp_startup
    import tui_gateway.entry

    class SchemaAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.__dict__.update(kwargs)
            self.tools = inert_surface.get_tool_definitions(
                enabled_toolsets=kwargs.get("enabled_toolsets"),
                disabled_toolsets=kwargs.get("disabled_toolsets"),
                quiet_mode=True, skip_tool_search_assembly=True,
            )
            self.valid_tool_names = {
                (tool.get("function") or tool)["name"] for tool in self.tools
            }

    monkeypatch.setattr(run_agent, "AIAgent", SchemaAgent)
    monkeypatch.setattr(server, "_resolve_startup_runtime", lambda: ("inert-model", "inert"))
    monkeypatch.setattr(server, "_resolve_runtime_with_fallback", lambda *a, **k:
                        SimpleNamespace(runtime={"provider": "inert", "api_mode": "chat_completions"},
                                        used_fallback=False))
    monkeypatch.setattr(server, "_load_provider_routing", lambda: {})
    monkeypatch.setattr(server, "_load_reasoning_config", lambda *a: None)
    monkeypatch.setattr(server, "_load_service_tier", lambda: None)
    monkeypatch.setattr(server, "_load_fallback_model", lambda: None)
    monkeypatch.setattr(server, "_parse_tui_skills_env", lambda: [])
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr(server, "_agent_cbs", lambda *a: {})
    monkeypatch.setattr(agent.coding_context, "coding_selection", lambda **k: None)
    monkeypatch.setattr(hermes_cli.mcp_startup, "wait_for_mcp_discovery", lambda: None)
    monkeypatch.setattr(tui_gateway.entry, "wait_for_mcp_discovery", lambda: None)
    monkeypatch.setattr(tui_gateway.entry, "ensure_mcp_discovery_started", lambda: None)
    monkeypatch.delenv("HERMES_TUI_TOOLSETS", raising=False)
    return server, SchemaAgent


@pytest.mark.parametrize("surface, expected", [
    ("desktop", ["desktop_ui", "project"]), ("tui", ["project"]),
    ("no-canary-gui-grant", []),
])
def test_empty_canonical_selection_never_becomes_all(monkeypatch, surface, expected):
    from tui_gateway import server
    import agent.coding_context
    import hermes_cli.config
    import hermes_cli.tools_config

    monkeypatch.delenv("HERMES_TUI_TOOLSETS", raising=False)
    monkeypatch.setattr(agent.coding_context, "coding_selection", lambda **k: None)
    monkeypatch.setattr(hermes_cli.config, "load_config", lambda: {})
    monkeypatch.setattr(hermes_cli.tools_config, "_get_platform_tools", lambda *a, **k: set())
    if surface == "no-canary-gui-grant":
        monkeypatch.setattr(server, "_gui_surface_toolsets", lambda *a: set())
    assert server._load_enabled_toolsets(surface) == expected


@pytest.mark.parametrize("disabled", [
    ["policy_canary_deny", "mcp_policy_canary"],
    '["policy_canary_deny", "mcp_policy_canary"]',
    "['policy_canary_deny', 'mcp_policy_canary']",
    [" policy_canary_deny ", "mcp_policy_canary", ""],
])
def test_fresh_agent_subtracts_disabled_members_of_composite(construction, monkeypatch, disabled):
    server, _ = construction
    monkeypatch.setattr(server, "_load_cfg", lambda: {"agent": {"disabled_toolsets": disabled}})
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda *a: ["policy_canary_composite"])
    agent = server._make_agent("canary-fresh", "canary-key", platform_override="desktop")
    assert agent.valid_tool_names == {"policy_canary_allowed"}
    assert agent.disabled_toolsets == ["policy_canary_deny", "mcp_policy_canary"]
    assert agent.platform == "desktop"


@pytest.mark.parametrize("agent_cfg", [None, {}, {"disabled_toolsets": []}])
def test_unset_disabled_preserves_existing_default_surface(construction, monkeypatch, agent_cfg):
    server, _ = construction
    monkeypatch.setattr(server, "_load_cfg", lambda: {"agent": agent_cfg})
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda *a: None)
    agent = server._make_agent("canary-default", "canary-key")
    assert agent.valid_tool_names == {
        "policy_canary_allowed", "policy_canary_forbidden", "policy_canary_mcp",
    }


@pytest.mark.parametrize("enabled", [[], ["policy_canary_composite"]])
def test_background_preserves_explicit_selection_and_disabled(construction, monkeypatch, enabled):
    server, SchemaAgent = construction
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda *a:
                        pytest.fail("an explicit parent selection must not fall back"))
    parent = SimpleNamespace(enabled_toolsets=enabled, disabled_toolsets=["policy_canary_deny", "mcp_policy_canary"],
                             model="inert-model", provider="inert")
    kwargs = server._background_agent_kwargs(parent, "canary-child")
    child = SchemaAgent(**kwargs)
    assert kwargs["enabled_toolsets"] == enabled
    assert kwargs["disabled_toolsets"] == parent.disabled_toolsets
    assert child.valid_tool_names == ({"policy_canary_allowed"} if enabled else set())
    assert kwargs["platform"] == "tui"
    kwargs["disabled_toolsets"].append("child-only")
    assert "child-only" not in parent.disabled_toolsets


@pytest.mark.parametrize("parent", [SimpleNamespace(model="inert-model"),
                                    SimpleNamespace(model="inert-model", enabled_toolsets=None)])
def test_background_none_or_missing_still_uses_tui_defaults(construction, monkeypatch, parent):
    server, _ = construction
    calls = []
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda platform:
                        calls.append(platform) or ["policy_canary_keep"])
    kwargs = server._background_agent_kwargs(parent, "canary-child")
    assert kwargs["enabled_toolsets"] == ["policy_canary_keep"]
    assert calls == ["tui"]


def test_background_explicit_empty_disabled_stays_empty(construction, monkeypatch):
    server, _ = construction
    monkeypatch.setattr(server, "_load_cfg", lambda: {"agent": {"disabled_toolsets": ["policy_canary_keep"]}})
    parent = SimpleNamespace(model="inert-model", enabled_toolsets=[], disabled_toolsets=[])
    kwargs = server._background_agent_kwargs(parent, "canary-child")
    assert kwargs["disabled_toolsets"] == []


def test_group_member_session_create_uses_its_profile_policy(construction, monkeypatch, tmp_path):
    import yaml
    from hermes_constants import get_hermes_home_override
    import agent.credits_tracker
    import tools.approval
    import hermes_cli.profiles

    server, _ = construction
    profile = tmp_path / "policy-canary-member"
    profile.mkdir()
    cfg = {"platform_toolsets": {"cli": ["policy_canary_composite"]},
           "agent": {"disabled_toolsets": ["policy_canary_deny", "mcp_policy_canary"]},
           "known_plugin_toolsets": {"cli": ["policy_canary_keep", "policy_canary_deny", "mcp_policy_canary"]},
           "mcp_servers": {}, "context": {"engine": "compressor"}}
    config_file = profile / "config.yaml"
    config_file.write_text(yaml.safe_dump(cfg))
    original = config_file.read_bytes()
    before_override = get_hermes_home_override()
    events = []
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(hermes_cli.profiles, "get_profile_dir", lambda name: str(profile))
    monkeypatch.setattr(server, "_schedule_agent_build", lambda sid: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_open_profile_session_db", lambda *a: None)
    monkeypatch.setattr(server, "_wire_callbacks", lambda *a: None)
    monkeypatch.setattr(server, "_start_notification_poller", lambda *a: None)
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *a: None)
    monkeypatch.setattr(server, "_schedule_mcp_late_refresh", lambda *a: None)
    monkeypatch.setattr(server, "_session_info", lambda agent, session:
                        {"tools": sorted(agent.valid_tool_names), "source": session["source"]})
    monkeypatch.setattr(server, "_emit", lambda event, sid, data: events.append((event, data)))
    monkeypatch.setattr(tools.approval, "register_gateway_notify", lambda *a: None)
    monkeypatch.setattr(tools.approval, "load_permanent_allowlist", lambda: None)
    monkeypatch.setattr(agent.credits_tracker, "seed_credits_at_session_start", lambda *a: None)

    result = server._methods["session.create"]("policy-canary-create", {
        "profile": "policy-canary-member", "source": "desktop", "hidden": True,
        "title": "OFFLINE POLICY CANARY", "cwd": str(tmp_path),
        "model": "inert-session-model", "provider": "inert",
    })
    sid = result["result"]["session_id"]
    session = server._sessions[sid]
    assert session["history"] == []
    server._start_agent_build(sid, session)
    assert session["agent_ready"].wait(5), "bounded inert build did not complete"
    session["_agent_build_thread"].join(1)
    assert session["agent_error"] is None
    built = session["agent"]
    assert built.valid_tool_names == {"policy_canary_allowed"}
    assert built.model == "inert-session-model"
    assert built.provider == "inert"
    assert built.platform == "desktop"
    assert session["profile_home"] == str(profile)
    assert any(event == "session.info" and data["tools"] == ["policy_canary_allowed"]
               for event, data in events)
    assert config_file.read_bytes() == original
    assert get_hermes_home_override() == before_override


@pytest.mark.parametrize("pin", ["all", "*"])
def test_explicit_all_pin_keeps_existing_default_meaning(monkeypatch, pin):
    from tui_gateway import server
    monkeypatch.setenv("HERMES_TUI_TOOLSETS", pin)
    assert server._load_enabled_toolsets("desktop") is None


def test_config_load_failure_keeps_existing_default_meaning(monkeypatch):
    from tui_gateway import server
    import agent.coding_context
    import hermes_cli.config

    monkeypatch.delenv("HERMES_TUI_TOOLSETS", raising=False)
    monkeypatch.setattr(agent.coding_context, "coding_selection", lambda **k: None)
    def broken_config():
        raise ValueError("inert failure")
    monkeypatch.setattr(hermes_cli.config, "load_config", broken_config)
    assert server._load_enabled_toolsets("desktop") is None
