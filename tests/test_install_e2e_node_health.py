"""Run the final E2E Node gate against real npm and disposable module trees."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


CHECK = Path(__file__).resolve().parents[1] / "scripts/sandbox/check-node-deps.sh"


def _package(path: Path, name: str, **fields: object) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "package.json").write_text(json.dumps({"name": name, "version": "1.0.0", **fields}))


def _fixture(tmp_path: Path) -> Path:
    root = tmp_path / "install"
    _package(root, "node-health-fixture", private=True,
             workspaces=["ui-tui", "web", "apps/desktop"],
             devDependencies={"root-probe": "1.0.0"})
    _package(root / "ui-tui", "ui-tui", dependencies={"tui-probe": "1.0.0"})
    _package(root / "web", "web", dependencies={"web-probe": "1.0.0"})
    # Desktop's dependency deliberately remains absent: the update route does
    # not install it, and the health gate must not broaden that contract.
    _package(root / "apps/desktop", "desktop", dependencies={"desktop-probe": "1.0.0"})
    modules = root / "node_modules"
    modules.mkdir()
    (modules / "ui-tui").symlink_to(root / "ui-tui", target_is_directory=True)
    (modules / "web").symlink_to(root / "web", target_is_directory=True)
    for name in ("root-probe", "tui-probe", "web-probe"):
        _package(modules / name, name)
    return root


def _run(root: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    if not shutil.which("node") or not shutil.which("npm"):
        pytest.skip("real Node.js and npm are required for the offline dependency health gate")
    # No inherited npm configuration or credentials participate in this probe.
    env = {key: value for key, value in os.environ.items()
           if not key.lower().startswith("npm_config_") and key.lower() not in
           {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "node_options"}}
    env.update({"HOME": str(tmp_path / "home"), "npm_config_cache": str(tmp_path / "npm-cache"),
                "npm_config_offline": "true", "CI": "1"})
    return subprocess.run(["bash", str(CHECK), str(root)], env=env,
                          capture_output=True, text=True, timeout=30, check=False)


def test_healthy_required_workspaces_pass_without_desktop_dependencies(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    proc = _run(root, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "Node dependency health check passed" in proc.stdout
    assert not (root / "node_modules/desktop-probe").exists()


@pytest.mark.parametrize("dependency", ["root-probe", "tui-probe", "web-probe"])
def test_partial_required_dependency_tree_fails(tmp_path: Path, dependency: str) -> None:
    root = _fixture(tmp_path)
    shutil.rmtree(root / "node_modules" / dependency)
    proc = _run(root, tmp_path)
    assert proc.returncode != 0
    assert "required Node dependencies are missing or invalid" in proc.stderr
    assert "health check passed" not in proc.stdout


@pytest.mark.parametrize("dependency", ["root-probe", "tui-probe", "web-probe"])
def test_missing_transitive_dependency_fails(tmp_path: Path, dependency: str) -> None:
    root = _fixture(tmp_path)
    _package(root / "node_modules" / dependency, dependency,
             dependencies={"nested-probe": "1.0.0"})
    proc = _run(root, tmp_path)
    assert proc.returncode != 0
    assert "required Node dependencies are missing or invalid" in proc.stderr
    assert "health check passed" not in proc.stdout


@pytest.mark.parametrize("dependency", ["root-probe", "tui-probe", "web-probe"])
def test_invalid_dependency_version_fails(tmp_path: Path, dependency: str) -> None:
    root = _fixture(tmp_path)
    _package(root / "node_modules" / dependency, dependency, version="2.0.0")
    proc = _run(root, tmp_path)
    assert proc.returncode != 0
    assert "missing or invalid" in proc.stderr


def test_uninstalled_optional_root_dependency_does_not_broaden_gate(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    _package(root, "node-health-fixture", private=True,
             workspaces=["ui-tui", "web", "apps/desktop"],
             devDependencies={"root-probe": "1.0.0"},
             dependencies={"optional-root-probe": "1.0.0"},
             optionalDependencies={"optional-root-probe": "1.0.0"})
    proc = _run(root, tmp_path)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("invalid", [[], "not-a-map", {"root-probe": 42}])
def test_invalid_root_dependency_map_fails_closed(tmp_path: Path, invalid: object) -> None:
    root = _fixture(tmp_path)
    _package(root, "node-health-fixture", private=True,
             workspaces=["ui-tui", "web", "apps/desktop"], devDependencies=invalid)
    proc = _run(root, tmp_path)
    assert proc.returncode != 0
    assert "health check passed" not in proc.stdout


def test_npm_failure_output_stays_out_of_health_transcript(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    # npm treats a remote dependency spec as its required version. Its error
    # includes that spec, so the gate must suppress the raw diagnostic entirely.
    _package(root / "web", "web", dependencies={"web-probe": "https://user:private-sentinel@example.invalid/pkg.tgz"})
    shutil.rmtree(root / "node_modules/web-probe")
    proc = _run(root, tmp_path)
    assert proc.returncode != 0
    assert "private-sentinel" not in proc.stdout + proc.stderr
    assert "example.invalid" not in proc.stdout + proc.stderr
