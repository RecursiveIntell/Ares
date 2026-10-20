"""Catalog context boundaries; payload probes are replaced, no provider calls."""
from pathlib import Path

import pytest
import yaml

from hermes_constants import get_hermes_home
import hermes_cli.inventory as inventory
import hermes_cli.profiles as profiles
import tui_gateway.server as srv


@pytest.fixture
def catalogs(tmp_path, monkeypatch):
    import socket
    def denied(*args, **kwargs):
        pytest.fail("Network calls forbidden in the catalog scope gate")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    homes = {name: tmp_path / name for name in ["source", "target"]}
    for name, home in homes.items():
        home.mkdir()
        (home / "config.yaml").write_text(yaml.safe_dump({
            "model": {"provider": f"{name}-provider", "default": f"{name}-model"},
            "providers": {f"{name}-provider": {"base_url": f"http://{name}.invalid/v1", "models": [f"{name}-model"]}},
        }))
    monkeypatch.setenv("HERMES_HOME", str(homes["source"]))
    monkeypatch.setattr(srv, "_hermes_home", homes["source"])
    monkeypatch.setattr(srv, "_sessions", {})
    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: homes.get("source" if name == "default" else name, tmp_path / "missing"))
    captured = []
    def payload(ctx, **kwargs):
        captured.append((Path(get_hermes_home()).resolve(), ctx))
        return {"provider": ctx.current_provider, "model": ctx.current_model,
                "providers": [{"slug": key, "models": value["models"]} for key, value in ctx.user_providers.items()]}
    monkeypatch.setattr(inventory, "build_model_options_payload", payload)
    return homes, captured


def options(**params):
    return srv.handle_request({"id": "fixture", "method": "model.options", "params": params})


def test_distinct_nonempty_catalogs_use_target_and_restore_source(catalogs):
    homes, captured = catalogs
    target = options(profile="target")["result"]
    source = options(profile="source")["result"]
    assert target["providers"] == [{"slug": "target-provider", "models": ["target-model"]}]
    assert source["providers"] == [{"slug": "source-provider", "models": ["source-model"]}]
    assert [home for home, _ in captured] == [homes["target"], homes["source"]]
    assert Path(get_hermes_home()).resolve() == homes["source"]


@pytest.mark.parametrize("profile", ["unknown", "../target", "/tmp", "", [], 42])
def test_invalid_unknown_or_traversal_profile_never_reads_launch_catalog(catalogs, profile):
    homes, captured = catalogs
    assert options(profile=profile)["error"]["code"] == 4033
    assert captured == []
    assert Path(get_hermes_home()).resolve() == homes["source"]


def test_known_session_owner_defines_catalog_and_rejects_conflicting_target(catalogs):
    homes, captured = catalogs
    srv._sessions["target-session"] = {"profile_home": str(homes["target"]), "agent": None}
    assert options(session_id="target-session")["result"]["provider"] == "target-provider"
    assert options(profile="source", session_id="target-session")["error"]["code"] == 4033
    assert len(captured) == 1
    assert options(profile="target", session_id="target-session")["result"]["provider"] == "target-provider"
    assert Path(get_hermes_home()).resolve() == homes["source"]


def test_default_and_omitted_profile_keep_launch_behavior(catalogs):
    assert options()["result"]["provider"] == "source-provider"
    assert options(profile="default")["result"]["provider"] == "source-provider"


def test_unknown_session_cannot_borrow_launch_catalog(catalogs):
    _, captured = catalogs
    assert options(session_id="missing")["error"]["code"] == 4001
    assert captured == []


def test_exception_restores_profile_context(catalogs, monkeypatch):
    homes, _ = catalogs
    def fail(ctx, **kwargs):
        assert Path(get_hermes_home()).resolve() == homes["target"]
        raise RuntimeError("fixture catalog failure")
    monkeypatch.setattr(inventory, "build_model_options_payload", fail)
    assert options(profile="target")["error"]["code"] == 5033
    assert Path(get_hermes_home()).resolve() == homes["source"]
