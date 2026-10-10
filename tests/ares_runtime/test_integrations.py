"""Behavioral tests for ares_runtime.integrations (typed config registration).

Contract under test: the registration plan merge is typed, idempotent, and
non-clobbering. Identical entries are left untouched, operator-customized
entries are preserved, malformed plans and unsafe existing configs fail
without writing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ares_runtime.integrations import (
    AresIntegrationsError,
    apply_plan,
    load_plan,
    main,
)


def _plan_file(tmp_path: Path, plan: dict) -> Path:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def _sample_plan() -> dict:
    return {
        "mcp_servers": {
            "semantic_memory": {
                "command": "/opt/ares/bin/semantic-memory-mcp",
                "args": ["--memory-dir", "/home/user/.ares/semantic-memory.db"],
            },
            "claim_ledger": {
                "command": "/opt/ares/bin/claim-ledger-mcp",
                "args": ["--ledger-dir", "/home/user/.ares/claim-ledger"],
            },
            "cea_graph": {"command": "/opt/ares/lib/cea-graph-mcp/cea-graph-mcp.py"},
        },
        "disable_builtin_memory": True,
    }


def test_apply_registers_servers_and_disables_builtin_memory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    report = apply_plan(home, load_plan(_plan_file(tmp_path, _sample_plan())))

    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert config["mcp_servers"]["semantic_memory"] == {
        "command": "/opt/ares/bin/semantic-memory-mcp",
        "enabled": True,
        "args": ["--memory-dir", "/home/user/.ares/semantic-memory.db"],
    }
    assert config["mcp_servers"]["cea_graph"] == {
        "command": "/opt/ares/lib/cea-graph-mcp/cea-graph-mcp.py",
        "enabled": True,
    }
    assert config["agent"]["disabled_toolsets"] == ["memory"]
    assert "registered mcp server: semantic_memory" in report


def test_apply_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = load_plan(_plan_file(tmp_path, _sample_plan()))
    apply_plan(home, plan)
    first = (home / "config.yaml").read_bytes()

    report = apply_plan(home, plan)
    assert (home / "config.yaml").read_bytes() == first
    assert "already registered: semantic_memory" in report
    assert "built-in memory toolset already disabled" in report
    assert "updated:" not in " ".join(report)


def test_apply_preserves_operator_configuration(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "provider": {"model": "operator-choice"},
                "mcp_servers": {
                    "semantic_memory": {
                        "command": "/custom/semantic-memory-mcp",
                        "enabled": True,
                        "args": ["--memory-dir", "/custom/store"],
                    },
                    "operator_server": {"url": "https://example.invalid/mcp"},
                },
                "agent": {"disabled_toolsets": ["memory"]},
            }
        ),
        encoding="utf-8",
    )

    report = apply_plan(home, load_plan(_plan_file(tmp_path, _sample_plan())))

    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    # Customized entry and unrelated keys are preserved verbatim.
    assert config["mcp_servers"]["semantic_memory"]["command"] == "/custom/semantic-memory-mcp"
    assert config["provider"] == {"model": "operator-choice"}
    assert config["mcp_servers"]["operator_server"] == {"url": "https://example.invalid/mcp"}
    assert "kept existing (differs from plan): semantic_memory" in report
    # The remaining plan entries still register.
    assert config["mcp_servers"]["claim_ledger"]["enabled"] is True


def test_apply_does_not_duplicate_disabled_toolsets(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        yaml.safe_dump({"agent": {"disabled_toolsets": ["memory", "voice"]}}),
        encoding="utf-8",
    )

    apply_plan(home, load_plan(_plan_file(tmp_path, _sample_plan())))
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert config["agent"]["disabled_toolsets"] == ["memory", "voice"]


def test_plan_validation_rejects_unsafe_shapes(tmp_path: Path) -> None:
    bad_plans = [
        {"mcp_servers": {"BadName": {"command": "/x"}}},
        {"mcp_servers": {"ok": {"command": "relative/path"}}},
        {"mcp_servers": {"ok": {"command": "/x", "args": "not-a-list"}}},
        {"mcp_servers": {"ok": {"command": "/x", "enabled": False}}},
        {"mcp_servers": {"ok": {"command": "/x"}}, "unknown_key": 1},
        {"disable_builtin_memory": "yes"},
        [],
    ]
    for index, bad in enumerate(bad_plans):
        path = tmp_path / f"bad-{index}.json"
        path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(AresIntegrationsError):
            load_plan(path)


def test_invalid_existing_config_fails_without_writing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text("mcp_servers: [not, a, mapping]\n", encoding="utf-8")
    before = config_path.read_bytes()

    with pytest.raises(AresIntegrationsError):
        apply_plan(home, load_plan(_plan_file(tmp_path, _sample_plan())))
    assert config_path.read_bytes() == before


def test_keeps_builtin_memory_when_semantic_entry_disabled(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "mcp_servers": {
                    "semantic_memory": {
                        "command": "/custom/semantic-memory-mcp",
                        "enabled": False,
                        "args": ["--memory-dir", "/custom/store"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    report = apply_plan(home, load_plan(_plan_file(tmp_path, _sample_plan())))

    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    # A preserved, disabled replacement must not turn off the working built-in
    # memory tools.
    assert "memory" not in (config.get("agent", {}).get("disabled_toolsets") or [])
    assert config["mcp_servers"]["semantic_memory"]["enabled"] is False
    assert any("kept enabled" in line for line in report)


def test_symlinked_config_is_preserved(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    real = shared / "config.yaml"
    real.write_text(
        yaml.safe_dump({"provider": {"model": "shared-choice"}}), encoding="utf-8"
    )
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").symlink_to(real)

    apply_plan(home, load_plan(_plan_file(tmp_path, _sample_plan())))

    assert (home / "config.yaml").is_symlink()
    updated = yaml.safe_load(real.read_text(encoding="utf-8"))
    assert updated["provider"] == {"model": "shared-choice"}
    assert updated["mcp_servers"]["semantic_memory"]["enabled"] is True


def test_cli_register_mcp_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    plan = _plan_file(tmp_path, _sample_plan())
    rc = main(["register-mcp", "--home", str(home), "--plan", str(plan)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "registered mcp server: semantic_memory" in out
    assert "updated:" in out
    assert (home / "config.yaml").is_file()


def test_cli_reports_typed_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    plan = tmp_path / "plan.json"
    plan.write_text("{not json", encoding="utf-8")
    rc = main(["register-mcp", "--home", str(home), "--plan", str(plan)])
    assert rc == 2
    assert "ares integrations error" in capsys.readouterr().err
    assert not (home / "config.yaml").exists()
