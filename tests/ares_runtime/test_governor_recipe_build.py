"""Real Git/subprocess plumbing with explicitly synthetic compiler/executable fixtures."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from ares_runtime import governor_install as native
from test_governor_recipe import capabilities, git, repository

pytestmark = pytest.mark.linux_only


def tools(tmp_path, monkeypatch, *, configured_target=None):
    root = tmp_path / "tools"
    root.mkdir()
    cargo = root / "cargo"
    binary_text = f"#!{sys.executable}\nprint({json.dumps(json.dumps(capabilities()))})\n"
    cargo.write_text(
        f"#!{sys.executable}\n"
        "import sys\nfrom pathlib import Path\n"
        "if sys.argv[1:] == ['--version']:\n print('cargo synthetic fixture')\n"
        "else:\n"
        " assert sys.argv[1:4] == ['build','--release','--locked']\n"
        " target_root=Path(sys.argv[sys.argv.index('--target-dir')+1])\n"
        f" selected=sys.argv[sys.argv.index('--target')+1] if '--target' in sys.argv else {configured_target!r}\n"
        " target=(target_root/selected if selected else target_root)/'release'\n"
        " target.mkdir(parents=True)\n"
        f" (target/'context-governor').write_text({binary_text!r})\n"
    )
    rustc = root / "rustc"
    rustc.write_text(f"#!{sys.executable}\nprint('rustc synthetic fixture\\nhost: x86_64-unknown-linux-gnu')\n")
    cargo.chmod(0o755)
    rustc.chmod(0o755)
    original = native.shutil.which
    def which(command):
        if command == "rustup":
            return None
        if command in ("cargo", "rustc"):
            return str(root / command)
        return original(command)
    monkeypatch.setattr(native.shutil, "which", which)
    return root


def stage(tmp_path):
    source = tmp_path / "stage/source"
    (source / ".venv/bin").mkdir(parents=True)
    return source


def test_locked_native_build_uses_git_archive_and_only_staged_output(tmp_path, monkeypatch):
    root, _ = repository(tmp_path)
    spec = native.GovernorSource.capture(root)
    tools(tmp_path, monkeypatch)
    source = stage(tmp_path)
    old = tmp_path / "global-cargo/bin/context-governor"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"unchanged external executable")
    recipe = native.build_governor(spec, source, external_backout=None)
    produced = source / native.BINARY_PATH
    assert recipe["source"] == spec.identity
    assert recipe["sha256"] == hashlib.sha256(produced.read_bytes()).hexdigest()
    assert recipe["lock_sha256"] == hashlib.sha256(b"version=4\n").hexdigest()
    assert recipe["capabilities"] == capabilities()
    assert recipe["build"]["flags"] == ["build", "--release", "--locked", "--target", "x86_64-unknown-linux-gnu"]
    assert old.read_bytes() == b"unchanged external executable"
    assert not (source.parent / "governor-build").exists()
    assert not (source.parent / "release.json").exists()


def test_native_build_refuses_source_commit_not_in_declared_remote(tmp_path, monkeypatch):
    root, crate = repository(tmp_path)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(root), str(bare)], check=True)
    git(root, "remote", "set-url", "origin", str(bare))
    (crate / "private-change").write_text("unpublished source")
    git(root, "add", "context-governor/private-change")
    git(root, "commit", "-m", "unpublished negative")
    spec = native.GovernorSource.capture(root)
    tools(tmp_path, monkeypatch)
    source = stage(tmp_path)
    with pytest.raises(native.GovernorInstallError, match="no admission"):
        native.build_governor(spec, source, external_backout=None)
    assert not (source / native.BINARY_PATH).exists()


def test_native_build_refuses_capture_drift_before_output(tmp_path, monkeypatch):
    root, crate = repository(tmp_path)
    spec = native.GovernorSource.capture(root)
    (crate / "Cargo.lock").write_text("changed")
    tools(tmp_path, monkeypatch)
    source = stage(tmp_path)
    with pytest.raises(native.GovernorInstallError, match="changed after capture"):
        native.build_governor(spec, source, external_backout=None)
    assert not (source / native.BINARY_PATH).exists()


def test_native_build_refuses_output_directory_substitution(tmp_path, monkeypatch):
    root, _ = repository(tmp_path)
    spec = native.GovernorSource.capture(root)
    tools(tmp_path, monkeypatch)
    source = stage(tmp_path)
    bin_dir = source / ".venv/bin"
    bin_dir.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    bin_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(native.GovernorInstallError, match="directory substitution"):
        native.build_governor(spec, source, external_backout=None)
    assert not (outside / "context-governor").exists()


def test_native_build_explicitly_pins_compiler_host_over_cargo_target(tmp_path, monkeypatch):
    root, _ = repository(tmp_path)
    spec = native.GovernorSource.capture(root)
    tools(tmp_path, monkeypatch, configured_target="ambient-cross-target")
    recipe = native.build_governor(spec, stage(tmp_path), external_backout=None)
    flags = recipe["build"]["flags"]
    assert flags[flags.index("--target") + 1] == "x86_64-unknown-linux-gnu"
    assert recipe["build"]["rustc"].endswith("host: x86_64-unknown-linux-gnu")
