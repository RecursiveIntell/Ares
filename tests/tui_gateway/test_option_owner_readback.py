"""Option readback must observe the live host, not cached or global state."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tui_gateway import server


@pytest.fixture
def owner(monkeypatch):
    record = {"session_key": "stored", "_compute_host_active": True,
              "agent": SimpleNamespace(reasoning_config={"enabled": True, "effort": "low"}, service_tier="priority"),
              "create_reasoning_override": {"enabled": True, "effort": "medium"},
              "_metadata_mirror": {"reasoning_effort": "medium", "fast": True}}
    monkeypatch.setattr(server, "_sessions", {"live": record})
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda s: True)
    monkeypatch.setattr(server, "_load_cfg", lambda: {"agent": {"reasoning_effort": "high"}})
    lookup = Mock(return_value={"session_id": "live", "session_info": {"reasoning_effort": "xhigh", "fast": False}})
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda: SimpleNamespace(lookup_session_key=lookup))
    return record, lookup


def read(key, sid="live"):
    return server.handle_request({"id": "read", "method": "config.get", "params": {"session_id": sid, "key": key}})


@pytest.mark.parametrize("key,expected", [("reasoning", "xhigh"), ("fast", "normal")])
def test_option_read_fetches_fresh_owner_instead_of_shadow(owner, key, expected):
    record, lookup = owner
    response = read(key)
    assert response["result"]["value"] == expected
    assert response["result"]["owner"] == "compute_host"
    assert response["result"]["session_id"] == "live"
    lookup.assert_called_once_with("stored")
    assert record["_metadata_mirror"] == {"reasoning_effort": "medium", "fast": True}


@pytest.mark.parametrize("effort", ["none", ""])
def test_explicit_owner_effort_is_not_replaced_with_default(owner, effort):
    record, lookup = owner
    lookup.return_value["session_info"]["reasoning_effort"] = effort
    assert read("reasoning")["result"]["value"] == effort


@pytest.mark.parametrize("failure", ["timeout", "missing", "wrong-owner", "invalid-info", "replaced-parent", "changed-key"])
def test_unconfirmed_read_never_falls_back_to_cached_values(owner, failure):
    record, lookup = owner
    if failure == "timeout":
        lookup.side_effect = TimeoutError("fixture read timeout")
    elif failure == "missing":
        lookup.return_value = None
    elif failure == "wrong-owner":
        lookup.return_value["session_id"] = "other"
    elif failure == "invalid-info":
        lookup.return_value["session_info"] = {}
    else:
        def replace(key):
            if failure == "changed-key":
                record["session_key"] = "replacement"
            else:
                server._sessions["live"] = {"session_key": "replacement"}
            return {"session_id": "live", "session_info": {"reasoning_effort": "xhigh"}}
        lookup.side_effect = replace
    assert "error" in read("reasoning")


@pytest.mark.parametrize("key", ["reasoning", "fast"])
def test_retired_runtime_read_is_not_profile_default(owner, key):
    response = read(key, "retired")
    assert response["error"]["code"] == 4001
    owner[1].assert_not_called()


@pytest.mark.parametrize("sid", [False, 0, [], {}])
def test_malformed_runtime_read_does_not_fall_through_to_defaults(owner, sid):
    assert read("reasoning", sid)["error"]["code"] == 4002
    owner[1].assert_not_called()
