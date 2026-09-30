"""Session identity and owner readback boundaries for model-option repair."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tui_gateway import server


@pytest.fixture(autouse=True)
def isolated_options(monkeypatch):
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_load_cfg_raw", lambda: {})
    monkeypatch.setattr(server, "_emit", Mock())
    monkeypatch.setattr(server, "_persist_live_session_runtime", Mock())


@pytest.mark.parametrize("key,value", [("reasoning", "xhigh"), ("reasoning", "show"), ("fast", "on")])
@pytest.mark.parametrize("scope", ["session", "global"])
def test_retired_runtime_never_becomes_profile_write(monkeypatch, key, value, scope):
    write = Mock()
    save = Mock()
    monkeypatch.setattr(server, "_write_config_key", write)
    monkeypatch.setattr(server, "_save_cfg", save)
    result = server.handle_request({"id": "stale", "method": "config.set", "params": {
        "session_id": "retired", "scope": scope, "key": key, "value": value,
    }})
    assert result.get("error", {}).get("code") == 4001
    write.assert_not_called()
    save.assert_not_called()


@pytest.mark.parametrize("mirror,expected", [({"reasoning_effort": "xhigh"}, "xhigh"),
                                             ({"reasoning_effort": "none"}, "none"),
                                             ({"reasoning_effort": ""}, ""), ({}, "")])
def test_host_owned_effort_never_falls_back_to_stale_parent(monkeypatch, mirror, expected):
    parent = SimpleNamespace(model="fixture", provider="fixture", reasoning_config={"enabled": True, "effort": "low"})
    session = {"session_key": "stored", "agent": parent, "_compute_host_active": True,
               "_metadata_mirror": mirror}
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda s: True)
    assert server._session_info(parent, session)["reasoning_effort"] == expected


def test_local_agent_remains_reasoning_owner(monkeypatch):
    parent = SimpleNamespace(model="fixture", provider="fixture", reasoning_config={"enabled": True, "effort": "low"})
    session = {"session_key": "stored", "agent": parent, "_metadata_mirror": {"reasoning_effort": "high"}}
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda s: False)
    assert server._session_info(parent, session)["reasoning_effort"] == "low"
