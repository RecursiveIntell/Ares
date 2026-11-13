"""``ares doctor`` must report capabilities the model cannot actually call.

``MCP readiness`` passes when servers answer, yet a plugin tool can reach the
model with no parameters, real-profile browsing can refuse to resolve the
desktop default browser, and research can resolve to no backend. These tests
drive the real probe functions against the real tool registry, browser
detector and web-backend resolver, with only host facts stubbed.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ares_runtime import capability_probe
from ares_runtime.local_runtime import AresLocalRuntime


def _register(registry, name, schema):
    registry.register(name=name, toolset="probe_fixture", schema=schema, handler=lambda args, **kw: "")


def test_tool_schema_check_names_a_tool_whose_model_schema_lacks_parameters(monkeypatch):
    from tools.registry import ToolRegistry

    registry = ToolRegistry()
    _register(registry, "good_tool", {"name": "good_tool", "description": "d", "parameters": {"type": "object", "properties": {}}})
    _register(registry, "bare_schema_tool", {"type": "object", "properties": {"x": {"type": "string"}}})
    monkeypatch.setattr("tools.registry.registry", registry)
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda force=False: None)

    result = capability_probe._tool_schema_check()

    assert result["ok"] is False
    assert "bare_schema_tool" in result["detail"]
    assert "good_tool" not in result["detail"]


def test_tool_schema_check_passes_when_every_tool_has_parameters(monkeypatch):
    from tools.registry import ToolRegistry

    registry = ToolRegistry()
    _register(registry, "good_tool", {"name": "good_tool", "description": "d", "parameters": {"type": "object", "properties": {}}})
    monkeypatch.setattr("tools.registry.registry", registry)
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda force=False: None)

    assert capability_probe._tool_schema_check()["ok"] is True


@pytest.mark.parametrize(
    "use_real_profile,detected,ok",
    [(True, None, False), (True, "__unsupported_channel__", False), (True, "chromium", True), (False, None, True)],
)
def test_browser_check_follows_real_detector(monkeypatch, use_real_profile, detected, ok):
    import hermes_cli.browser_connect as bc

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"browser": {"use_real_profile": use_real_profile}})
    calls = []
    monkeypatch.setattr(bc, "detect_default_chromium", lambda system=None: calls.append(1) or detected)
    if detected == "__unsupported_channel__":
        monkeypatch.setattr(bc, "UNSUPPORTED_CHANNEL", "__unsupported_channel__")

    result = capability_probe._browser_check()

    assert result["ok"] is ok
    assert bool(calls) is use_real_profile  # consent off never probes the desktop


def test_web_backend_check_fails_when_nothing_resolves(monkeypatch):
    monkeypatch.setattr("tools.web_tools._ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr("agent.web_search_registry.get_active_search_provider", lambda: None)
    monkeypatch.setattr("agent.web_search_registry.get_active_extract_provider", lambda: None)

    result = capability_probe._web_backend_check()

    assert result["ok"] is False
    assert "search: none" in result["detail"]


def test_web_backend_check_reports_keyless_tier(monkeypatch):
    provider = SimpleNamespace(name="keenable", is_available=lambda: False, is_keyless_available=lambda: True)
    monkeypatch.setattr("tools.web_tools._ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr("agent.web_search_registry.get_active_search_provider", lambda: provider)
    monkeypatch.setattr("agent.web_search_registry.get_active_extract_provider", lambda: provider)

    result = capability_probe._web_backend_check()

    assert result["ok"] is True
    assert "search: keenable (keyless)" in result["detail"]


def test_a_crashing_check_is_reported_not_raised(monkeypatch):
    def _boom():
        raise RuntimeError("fixture")

    monkeypatch.setattr(capability_probe, "CHECKS", (("tool schemas", _boom),))

    assert capability_probe.run() == {"tool schemas": {"ok": False, "detail": "probe failed: RuntimeError: fixture"}}


def test_doctor_surfaces_capability_probe_results(tmp_path: Path, monkeypatch) -> None:
    runtime = AresLocalRuntime.__new__(AresLocalRuntime)
    source = tmp_path / "source"
    source.mkdir()
    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    payload = {
        "tool schemas": {"ok": False, "detail": "model sees no parameters for: recursive_agent_execute"},
        "real-profile browser": {"ok": True, "detail": "real profile resolves to chromium"},
    }

    def subprocess_run(command, **_kwargs):
        assert command[:3] == [python, "-m", "ares_runtime.capability_probe"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("ares_runtime.local_runtime.subprocess.run", subprocess_run)
    monkeypatch.setattr(runtime, "_agent_environment", lambda: {}, raising=False)

    checks = runtime._capability_checks(python, source)

    assert ("tool schemas", False, "model sees no parameters for: recursive_agent_execute") in checks
    assert ("real-profile browser", True, "real profile resolves to chromium") in checks


def test_doctor_reports_a_failed_capability_probe(tmp_path: Path, monkeypatch) -> None:
    runtime = AresLocalRuntime.__new__(AresLocalRuntime)
    monkeypatch.setattr(
        "ares_runtime.local_runtime.subprocess.run",
        lambda *_a, **_kw: SimpleNamespace(returncode=1, stdout="", stderr="ImportError: x\n"),
    )
    monkeypatch.setattr(runtime, "_agent_environment", lambda: {}, raising=False)

    assert runtime._capability_checks(tmp_path / "python", tmp_path) == [
        ("callable capabilities", False, "probe failed: ImportError: x")
    ]
