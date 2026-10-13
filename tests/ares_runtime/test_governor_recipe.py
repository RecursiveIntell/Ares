"""Native recipe/source witnesses; fake executables test plumbing, not Rust qualification."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys

import pytest

from ares_runtime.governor_install import (
    BINARY_PATH, GovernorInstallError, GovernorSource, NATIVE_LIFECYCLE_CONTRACT,
    RECIPE_SCHEMA, SOURCE_SCHEMA, build_governor, bundled_binary,
    external_backout, recipe_for_source, require_pair,
)
from ares_runtime.local_runtime import AresLocalPaths, AresLocalRuntime, AresLocalRuntimeError

pytestmark = pytest.mark.linux_only


def capabilities():
    return {
        "schema": "ContextGovernorCapabilitiesV1", "engine": "ri-context-governor",
        "receipt_schema": "ContextCompactionReceiptV2", "integrity": "hmac-sha256-canonical-json-v1",
        "exactness_scope": "canonical_utf8_text_v1",
        "supports_recursive_lineage": True, "supports_certified_receipt_store": True,
        "supports_lineage_continuation": True, "supports_lineage_tip_projection": True,
        "supports_host_finalization_reserve": True, "supports_pending_activation": True,
        "failure_envelope": {"flag": "--failure-envelope-v1", "schema": "ContextGovernorFailureV1", "stream": "stderr"},
    }


def owner(tmp_path):
    return AresLocalRuntime(AresLocalPaths(
        tmp_path / "state", tmp_path / "runtime", tmp_path / "home",
        tmp_path / "bin/ares", tmp_path / "units/fixture.service",
    ))


def native_release(runtime, revision="a" * 40, *, backout=None):
    source = runtime.paths.releases_dir / revision / "source"
    binary = source / BINARY_PATH
    binary.parent.mkdir(parents=True)
    (binary.parent / "python").write_text("inert completeness witness")
    binary.write_text(f"#!{sys.executable}\nprint({json.dumps(json.dumps(capabilities()))})\n")
    binary.chmod(0o755)
    recipe = {
        "schema": RECIPE_SCHEMA,
        "source": {"schema": SOURCE_SCHEMA, "remote": "fixture-native", "revision": "b" * 40,
                   "crate_path": "context-governor", "crate_tree": "c" * 40},
        "binary": BINARY_PATH, "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "lock_sha256": "d" * 64, "platform": platform.system(), "architecture": platform.machine(),
        "capabilities": capabilities(),
        "build": {"cargo": "fixture cargo", "rustc": "fixture rustc", "flags": ["build", "--release", "--locked"], "features": []},
        "external_backout": backout,
    }
    record = {"revision": revision, "governor": recipe,
              "runtime_binding": {"schema": "AresLocalRuntimeBindingV1", "source": str(source.resolve()),
                                  "controller_contract": NATIVE_LIFECYCLE_CONTRACT}}
    descriptor = source.parent / "release.json"
    descriptor.write_text(json.dumps(record))
    return source, binary, record, descriptor


def base_release(runtime, revision="f" * 40):
    source = runtime.paths.releases_dir / revision / "source"
    binary = source / ".venv/bin/python"
    binary.parent.mkdir(parents=True)
    binary.write_text("inert base interpreter")
    record = {"revision": revision, "runtime_binding": {"schema": "AresLocalRuntimeBindingV1",
              "source": str(source.resolve()), "controller_contract": 1}}
    (source.parent / "release.json").write_text(json.dumps(record))
    return source


def test_valid_bundle_is_source_owned_not_path_selected(tmp_path, monkeypatch):
    runtime = owner(tmp_path)
    source, binary, _, _ = native_release(runtime)
    monkeypatch.setenv("CONTEXT_GOVERNOR_BIN", "/unowned/override")
    assert bundled_binary(source, probe=True) == binary
    runtime._require_complete_release(source, desktop=False)


@pytest.mark.parametrize("change", ["tamper", "missing", "symlink", "hardlink", "writable", "architecture", "metadata_removed", "duplicate"])
def test_native_identity_changes_are_refused(tmp_path, change):
    runtime = owner(tmp_path)
    source, binary, record, descriptor = native_release(runtime)
    if change == "tamper":
        binary.write_text("changed")
    elif change == "missing":
        binary.unlink()
    elif change == "symlink":
        moved = source / "unowned-binary"
        binary.rename(moved)
        binary.symlink_to(moved)
    elif change == "hardlink":
        (source / "alias").hardlink_to(binary)
    elif change == "writable":
        binary.chmod(0o777)
    elif change == "architecture":
        record["governor"]["architecture"] = "not-this-host"
        descriptor.write_text(json.dumps(record))
    elif change == "metadata_removed":
        record.pop("governor")
        descriptor.write_text(json.dumps(record))
    else:
        descriptor.write_text('{"revision":"bad","revision":"duplicate"}')
    with pytest.raises((GovernorInstallError, AresLocalRuntimeError)):
        runtime._require_complete_release(source, desktop=False)


def test_immutable_recipe_mismatch_does_not_quarantine_or_repair(tmp_path):
    runtime = owner(tmp_path)
    source, binary, _, descriptor = native_release(runtime)
    before = (descriptor.read_bytes(), binary.read_bytes(), descriptor.stat().st_ino)
    different = GovernorSource({"schema": SOURCE_SCHEMA, "remote": "other-native", "revision": "e" * 40,
                                "crate_path": "context-governor", "crate_tree": "c" * 40})
    with pytest.raises(AresLocalRuntimeError, match="recipe mismatch"):
        runtime._materialize("not-contacted", source.parent.name, desktop=False, governor=different)
    assert before == (descriptor.read_bytes(), binary.read_bytes(), descriptor.stat().st_ino)
    assert not runtime.paths.current_link.exists()


def test_external_backout_and_reselection_refuse_changed_predecessor(tmp_path):
    runtime = owner(tmp_path)
    base = base_release(runtime)
    external = tmp_path / "external/context-governor"
    external.parent.mkdir()
    external.write_bytes(b"external predecessor witness")
    binding = external_backout((base.parent.name, base), external)
    source, _, _, _ = native_release(runtime, backout=binding)
    runtime._activate(base.parent.name)
    runtime._activate(source.parent.name)
    assert runtime.rollback() == base.parent.name
    assert runtime.rollback() == source.parent.name
    before = (runtime.active_release(), runtime.previous_release())
    external.write_bytes(b"changed predecessor")
    with pytest.raises(AresLocalRuntimeError, match="backout identity"):
        runtime.rollback()
    assert (runtime.active_release(), runtime.previous_release()) == before


def test_native_to_native_backout_does_not_require_retired_external_predecessor(tmp_path):
    runtime = owner(tmp_path)
    one, _, _, _ = native_release(runtime, revision="a" * 40)
    two, _, _, _ = native_release(runtime, revision="e" * 40)
    require_pair((one.parent.name, one), (two.parent.name, two))


def test_adapter_checks_bundle_again_before_native_execution(tmp_path, monkeypatch):
    from plugins.context_engine import _context_governor as adapter
    runtime = owner(tmp_path)
    source, binary, _, descriptor = native_release(runtime)
    monkeypatch.setattr(adapter, "_NATIVE_SOURCE_OWNER", source)
    engine = adapter.ContextGovernorEngine()
    assert Path(engine.binary) == binary
    binary.write_text("changed after construction")
    with pytest.raises(adapter.ContextGovernorActivationError, match="digest mismatch"):
        engine._run_json(["capabilities"], {})
    binary.unlink()
    descriptor.unlink()
    with pytest.raises(adapter.ContextGovernorActivationError):
        engine._run_json(["capabilities"], {})


def test_declared_bundle_refuses_explicit_foreign_binary(tmp_path, monkeypatch):
    from plugins.context_engine import _context_governor as adapter
    runtime = owner(tmp_path)
    source, _, _, _ = native_release(runtime)
    monkeypatch.setattr(adapter, "_NATIVE_SOURCE_OWNER", source)
    with pytest.raises(adapter.ContextGovernorActivationError, match="explicit binary"):
        adapter.ContextGovernorEngine(binary="/unowned/other")


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def repository(tmp_path):
    root = tmp_path / "git"
    root.mkdir()
    git(root, "init", "--initial-branch", "main")
    git(root, "config", "user.name", "Native Fixture")
    git(root, "config", "user.email", "native@example.invalid")
    crate = root / "context-governor"
    crate.mkdir()
    (crate / "Cargo.toml").write_text('[package]\nname="context-governor"\nversion="0.0.0"\n[workspace]\n')
    (crate / "Cargo.lock").write_text("version=4\n")
    git(root, "add", "context-governor")
    git(root, "commit", "-m", "native fixture")
    git(root, "remote", "add", "origin", str(root))
    return root, crate


def test_source_capture_uses_exact_git_crate_tuple(tmp_path):
    root, crate = repository(tmp_path)
    spec = GovernorSource.capture(root)
    assert spec.identity["revision"] == git(root, "rev-parse", "HEAD")
    assert spec.identity["crate_tree"] == git(root, "rev-parse", "HEAD:context-governor")
    assert GovernorSource.capture(crate).identity == spec.identity
    (crate / "Cargo.toml").write_text("changed")
    with pytest.raises(GovernorInstallError, match="uncommitted"):
        GovernorSource.capture(crate)


def test_source_capture_refuses_untracked_inputs_and_source_links(tmp_path):
    root, crate = repository(tmp_path)
    extra = crate / "untracked"
    extra.write_text("not admitted")
    with pytest.raises(GovernorInstallError, match="untracked"):
        GovernorSource.capture(root)
    extra.unlink()
    (crate / "alias").symlink_to("Cargo.toml")
    git(root, "add", "context-governor/alias")
    git(root, "commit", "-m", "link negative")
    with pytest.raises(GovernorInstallError, match="symlinks"):
        GovernorSource.capture(root)
