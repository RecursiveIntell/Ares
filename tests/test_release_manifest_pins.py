"""Task 4.2 tests for immutable source-bundle resolution and installer wiring."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts" / "ci" / "release-manifest.json"
HELPER = ROOT / "scripts" / "ci" / "release_manifest.py"
INSTALLER = ROOT / "install.sh"
SPEC = importlib.util.spec_from_file_location("ares_release_manifest", HELPER)
assert SPEC is not None and SPEC.loader is not None
release_manifest = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release_manifest
SPEC.loader.exec_module(release_manifest)


def _git(cwd: pathlib.Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _manifest_fixture() -> dict:
    return {
        "schema": release_manifest.SCHEMA,
        "repositories": {
            name: {"url": url, "ref": "refs/heads/main", "commit": "a" * 40}
            for name, url in release_manifest.REPOSITORIES.items()
        },
        "submodules": {
            name: {**values, "commit": "b" * 40}
            for name, values in release_manifest.SUBMODULES.items()
        },
        "toolchains": release_manifest.TOOLCHAINS,
    }


def test_release_manifest_pins_all_repositories_and_mcp_submodule():
    assert MANIFEST.is_file(), "release manifest is missing: scripts/ci/release-manifest.json"
    document, raw = release_manifest.load_manifest(MANIFEST)
    assert set(document["repositories"]) == set(release_manifest.REPOSITORIES)
    assert set(document["submodules"]) == {"semantic-memory-mcp"}
    assert len(release_manifest.hashlib.sha256(raw).hexdigest()) == 64


def test_manifest_rejects_mutable_refs_wrong_origins_and_extra_fields(tmp_path):
    path = tmp_path / "manifest.json"
    manifest = _manifest_fixture()
    manifest["repositories"]["Libraries"]["ref"] = "main"
    path.write_text(json.dumps(manifest))
    with pytest.raises(release_manifest.ManifestError, match="ref is invalid"):
        release_manifest.load_manifest(path)

    manifest = _manifest_fixture()
    manifest["repositories"]["Libraries"]["url"] = "https://invalid.example/Libraries.git"
    path.write_text(json.dumps(manifest))
    with pytest.raises(release_manifest.ManifestError, match="origin is not canonical"):
        release_manifest.load_manifest(path)

    manifest = _manifest_fixture()
    manifest["extra"] = True
    path.write_text(json.dumps(manifest))
    with pytest.raises(release_manifest.ManifestError, match="manifest keys"):
        release_manifest.load_manifest(path)


def test_checkout_exact_lands_on_pinned_ancestor_and_refuses_dirty_checkout(tmp_path):
    remote = tmp_path / "remote"
    destination = tmp_path / "checkout"
    remote.mkdir()
    _git(remote, "init", "-q", "-b", "main")
    (remote / "payload.txt").write_text("first\n")
    _git(remote, "add", "payload.txt")
    subprocess.run(
        ["git", "-C", str(remote), "-c", "user.name=Fixture", "-c", "user.email=fixture", "commit", "-qm", "first"],
        check=True,
    )
    pinned = _git(remote, "rev-parse", "HEAD")
    (remote / "payload.txt").write_text("second\n")
    _git(remote, "-c", "user.name=Fixture", "-c", "user.email=fixture", "commit", "-qam", "second")

    actual = release_manifest.checkout_exact(str(remote), "refs/heads/main", pinned, destination)
    assert actual == pinned
    assert _git(destination, "rev-parse", "HEAD") == pinned
    (destination / "untracked.txt").write_text("dirty\n")
    with pytest.raises(release_manifest.ManifestError, match="dirty source checkout"):
        release_manifest.checkout_exact(str(remote), "refs/heads/main", pinned, destination)


def test_submodule_pin_must_match_parent_gitlink_before_update(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    _git(parent, "init", "-q", "-b", "main")
    _git(parent, "-c", "user.name=Fixture", "-c", "user.email=fixture", "commit", "--allow-empty", "-qm", "base")
    _git(parent, "update-index", "--add", "--cacheinfo", "160000", "a" * 40, "semantic-memory-mcp")
    _git(parent, "-c", "user.name=Fixture", "-c", "user.email=fixture", "commit", "-qm", "gitlink")
    with pytest.raises(release_manifest.ManifestError, match="does not match the parent gitlink"):
        release_manifest.verify_submodule(
            parent,
            "semantic-memory-mcp",
            "semantic-memory-mcp",
            release_manifest.SUBMODULES["semantic-memory-mcp"]["url"],
            "b" * 40,
        )


def test_installer_uses_manifest_exact_checkout_and_local_mcp_submodule():
    text = INSTALLER.read_text()
    assert "release-manifest.json" in text
    assert "release_manifest.py" in text
    assert "checkout_exact" in text
    helper = HELPER.read_text()
    assert "verify_manifest_submodule" in text
    assert "def verify_submodule(" in helper
    assert "parent gitlink" in helper
    assert 'cargo install --locked --path "$LIBRARIES/semantic-memory-mcp"' in text
    assert "cargo install --locked semantic-memory-mcp" not in text
    assert "recursive-agent clone failed; skipping the plugin payload install." not in text
