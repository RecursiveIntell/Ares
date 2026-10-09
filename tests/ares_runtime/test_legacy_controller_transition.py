"""Behavioral legacy transition tests; all runtimes and imports are disposable."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import venv

import pytest

from ares_runtime.local_runtime import AresLocalPaths, AresLocalRuntime, AresLocalRuntimeError, _parser


def _git(source: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def runtime(tmp_path: Path) -> AresLocalRuntime:
    runtime = AresLocalRuntime(AresLocalPaths(
        tmp_path / "state", tmp_path / "data", tmp_path / "home",
        tmp_path / "bin" / "ares", tmp_path / "units" / "fixture-only.service",
    ))
    runtime._ensure_layout()
    runtime.paths.agent_home.mkdir()
    return runtime


def _release(runtime: AresLocalRuntime, *, legacy: bool, tag: str = "first") -> Path:
    staged = runtime.paths.staging_dir / tag
    staged.mkdir()
    (staged / "ares_runtime").mkdir()
    (staged / "ares_runtime" / "__init__.py").write_text("")
    # This is an identity-probe fixture, not a fabricated full Ares build.
    (staged / "ares_runtime" / "local_runtime.py").write_text(
        ("" if legacy else "LOCAL_LIFECYCLE_CONTRACT = 1\nLEGACY_TRANSITION_CONTRACT = 1\n")
        + "class AresLocalRuntime:\n"
        + "    def setup(self): pass\n"
        + "    def rollback(self): pass\n"
        + "    def _materialize(self): pass\n"
        + "    def _refresh_moved_editable_install(self): pass\n"
        + "    def locked(self): pass\n"
    )
    (staged / "hermes_cli").mkdir()
    (staged / "hermes_cli" / "__init__.py").write_text("")
    (staged / "hermes_cli" / "main.py").write_text("def main(): pass\n")
    (staged / ".gitignore").write_text(".venv/\n__pycache__/\n")
    _git(staged, "init", "--initial-branch", "main")
    _git(staged, "config", "user.name", "Disposable Runtime Fixture")
    _git(staged, "config", "user.email", "fixture@example.invalid")
    _git(staged, "add", ".")
    _git(staged, "commit", "-m", f"identity fixture {tag}")
    revision = _git(staged, "rev-parse", "HEAD")
    final = runtime.paths.releases_dir / revision / "source"
    final.parent.mkdir()
    staged.rename(final)
    venv.EnvBuilder(with_pip=False, symlinks=True).create(final / ".venv")
    site = subprocess.run(
        [str(runtime._python_for(final)), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    (Path(site) / "fixture-final-binding.pth").write_text(str(final.resolve()) + "\n")
    record = {"revision": revision, "source": "explicit disposable source", "installed_at": 1}
    if not legacy:
        record["runtime_binding"] = {
            "schema": "AresLocalRuntimeBindingV1", "source": str(final.resolve()), "controller_contract": 1,
        }
    runtime._atomic_json(final.parent / "release.json", record)
    return final


def _select(runtime: AresLocalRuntime, current: Path, previous: Path | None = None) -> None:
    runtime._atomic_link(runtime.paths.current_link, current.resolve())
    if previous is not None:
        runtime._atomic_link(runtime.paths.previous_link, previous.resolve())


def _pair(runtime: AresLocalRuntime) -> tuple[str | None, str | None]:
    current = runtime.paths.current_link
    previous = runtime.paths.previous_link
    return (
        str(current.readlink()) if current.is_symlink() else None,
        str(previous.readlink()) if previous.is_symlink() else None,
    )


def _setup_seams(runtime: AresLocalRuntime, monkeypatch, materialize=None) -> list[str]:
    calls: list[str] = []
    for name in ("_materialize", "_seed_agent_home", "_provision_context_governor_key",
                 "_write_config", "_install_launcher", "_install_gateway_unit", "_handoff_gateway"):
        def record(*_args, _name=name, **_kwargs):
            calls.append(_name)
            if _name == "_materialize" and materialize is not None:
                materialize()
            return False
        monkeypatch.setattr(runtime, name, record)
    return calls


def test_cli_transition_requires_an_exact_expected_revision():
    args = _parser().parse_args(["setup", "--transition-from-legacy", "a" * 40])
    assert args.transition_from_legacy == "a" * 40


def test_setup_without_explicit_transition_refuses_before_any_build(runtime, monkeypatch):
    old = _release(runtime, legacy=True)
    new = _release(runtime, legacy=False, tag="new")
    _select(runtime, old)
    before = _pair(runtime)
    old_bytes = (old.parent / "release.json").read_bytes()
    calls = _setup_seams(runtime, monkeypatch)
    with pytest.raises(AresLocalRuntimeError, match="transition-from-legacy"):
        runtime.setup(new, desktop=False, gateway=False, seed_from=runtime.paths.agent_home)
    assert calls == []
    assert _pair(runtime) == before
    assert (old.parent / "release.json").read_bytes() == old_bytes


def test_legacy_probe_checks_real_final_imports_without_promoting_contract(runtime):
    old = _release(runtime, legacy=True)
    before = (old.parent / "release.json").read_bytes()
    binding = runtime._probe_legacy_release(old, desktop=False)
    assert binding["schema"] == "AresLegacyRollbackBindingV1"
    assert binding["revision"] == old.parent.name
    assert binding["source"] == str(old.resolve())
    assert binding["controller_contract"] == "legacy-v0"
    assert binding["git_tree"] == _git(old, "rev-parse", "HEAD^{tree}")
    assert (old.parent / "release.json").read_bytes() == before
    with pytest.raises(AresLocalRuntimeError):
        runtime._require_complete_release(old, desktop=False)


@pytest.mark.parametrize("mutation", ["missing-descriptor", "wrong-revision", "unknown-field", "new-binding", "bool-time", "missing-python", "dirty-source", "symlink-descriptor"])
def test_legacy_probe_refuses_unverified_or_new_format_releases(runtime, mutation):
    old = _release(runtime, legacy=True)
    descriptor = old.parent / "release.json"
    record = json.loads(descriptor.read_text())
    if mutation == "missing-descriptor":
        descriptor.unlink()
    elif mutation == "missing-python":
        runtime._python_for(old).unlink()
    elif mutation == "dirty-source":
        (old / "hermes_cli" / "main.py").write_text("changed = True\n")
    elif mutation == "symlink-descriptor":
        alias = descriptor.with_name("original.json")
        descriptor.rename(alias)
        descriptor.symlink_to(alias)
    else:
        if mutation == "wrong-revision":
            record["revision"] = "f" * 40
        elif mutation == "unknown-field":
            record["trusted"] = True
        elif mutation == "new-binding":
            record["runtime_binding"] = {"controller_contract": 0}
        elif mutation == "bool-time":
            record["installed_at"] = True
        runtime._atomic_json(descriptor, record)
    with pytest.raises(AresLocalRuntimeError):
        runtime._probe_legacy_release(old, desktop=False)
    assert not runtime.paths.current_link.exists()


def test_legacy_probe_refuses_wrong_editable_import_owner(runtime, tmp_path):
    old = _release(runtime, legacy=True)
    site = subprocess.run([str(runtime._python_for(old)), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], check=True, capture_output=True, text=True).stdout.strip()
    (Path(site) / "fixture-final-binding.pth").write_text(str(tmp_path / "missing-source") + "\n")
    with pytest.raises(AresLocalRuntimeError):
        runtime._probe_legacy_release(old, desktop=False)


def test_new_controller_without_binding_is_not_legacy(runtime):
    new = _release(runtime, legacy=False)
    descriptor = new.parent / "release.json"
    record = json.loads(descriptor.read_text())
    record.pop("runtime_binding")
    runtime._atomic_json(descriptor, record)
    with pytest.raises(AresLocalRuntimeError):
        runtime._probe_legacy_release(new, desktop=False)


def test_explicit_setup_records_distinct_binding_and_normal_rollback_preserves_old_bytes(runtime, monkeypatch):
    old = _release(runtime, legacy=True)
    new = _release(runtime, legacy=False, tag="new")
    _select(runtime, old)
    old_bytes = (old.parent / "release.json").read_bytes()
    calls = _setup_seams(runtime, monkeypatch)
    assert runtime.setup(new, desktop=False, gateway=False, seed_from=runtime.paths.agent_home, transition_from_legacy=old.parent.name)[0] == new.parent.name
    assert runtime.active_release()[0] == new.parent.name
    assert runtime.previous_release()[0] == old.parent.name
    record = json.loads((new.parent / "release.json").read_text())
    assert record["legacy_rollback_binding"]["revision"] == old.parent.name
    assert record["runtime_binding"]["controller_contract"] == 1
    assert runtime.rollback() == old.parent.name
    assert runtime.active_release()[0] == old.parent.name
    assert runtime.previous_release()[0] == new.parent.name
    assert (old.parent / "release.json").read_bytes() == old_bytes
    assert "_materialize" in calls


@pytest.mark.parametrize("expected", ["a" * 40, "malformed", ""])
def test_setup_transition_expected_revision_mismatch_is_effect_free(runtime, monkeypatch, expected):
    old = _release(runtime, legacy=True)
    new = _release(runtime, legacy=False, tag="new")
    _select(runtime, old)
    before = _pair(runtime)
    calls = _setup_seams(runtime, monkeypatch)
    with pytest.raises(AresLocalRuntimeError):
        runtime.setup(new, desktop=False, gateway=False, seed_from=runtime.paths.agent_home, transition_from_legacy=expected)
    assert calls == []
    assert _pair(runtime) == before


def test_setup_rechecks_legacy_after_build_before_selection(runtime, monkeypatch):
    old = _release(runtime, legacy=True)
    new = _release(runtime, legacy=False, tag="new")
    _select(runtime, old)
    before = _pair(runtime)
    def drift():
        descriptor = old.parent / "release.json"
        record = json.loads(descriptor.read_text())
        record["installed_at"] = 2
        runtime._atomic_json(descriptor, record)
    calls = _setup_seams(runtime, monkeypatch, drift)
    with pytest.raises(AresLocalRuntimeError, match="changed"):
        runtime.setup(new, desktop=False, gateway=False, seed_from=runtime.paths.agent_home, transition_from_legacy=old.parent.name)
    assert _pair(runtime) == before
    assert calls == ["_materialize"]


def _bound_pair(runtime: AresLocalRuntime) -> tuple[Path, Path]:
    old = _release(runtime, legacy=True)
    new = _release(runtime, legacy=False, tag="new")
    _select(runtime, old)
    binding = runtime._probe_legacy_release(old, desktop=False)
    runtime._record_legacy_rollback_binding(new, binding)
    runtime._activate(new.parent.name)
    return old, new


@pytest.mark.parametrize("mutation", ["schema", "revision", "source", "git_tree", "descriptor_sha256", "unknown-field", "missing"])
def test_legacy_rollback_binding_must_match_exact_reverified_target(runtime, mutation):
    old, new = _bound_pair(runtime)
    descriptor = new.parent / "release.json"
    record = json.loads(descriptor.read_text())
    if mutation == "missing":
        record.pop("legacy_rollback_binding")
    elif mutation == "unknown-field":
        record["legacy_rollback_binding"]["trusted"] = True
    else:
        record["legacy_rollback_binding"][mutation] = "wrong"
    runtime._atomic_json(descriptor, record)
    before = _pair(runtime)
    old_bytes = (old.parent / "release.json").read_bytes()
    with pytest.raises(AresLocalRuntimeError):
        runtime.rollback()
    assert _pair(runtime) == before
    assert (old.parent / "release.json").read_bytes() == old_bytes


@pytest.mark.parametrize("drift", ["descriptor", "venv-config"])
def test_legacy_rollback_revalidates_current_bytes(runtime, drift):
    old, _new = _bound_pair(runtime)
    before = _pair(runtime)
    if drift == "descriptor":
        path = old.parent / "release.json"
        record = json.loads(path.read_text())
        record["installed_at"] = 2
        runtime._atomic_json(path, record)
    else:
        path = old / ".venv" / "pyvenv.cfg"
        path.write_text(path.read_text() + "\n# drift witness\n")
    with pytest.raises(AresLocalRuntimeError):
        runtime.rollback()
    assert _pair(runtime) == before


def test_transition_binding_cannot_be_written_to_either_selected_release(runtime):
    old, new = _bound_pair(runtime)
    binding = runtime._probe_legacy_release(old, desktop=False)
    for selected in (old, new):
        before = (selected.parent / "release.json").read_bytes()
        with pytest.raises(AresLocalRuntimeError):
            runtime._record_legacy_rollback_binding(selected, binding)
        assert (selected.parent / "release.json").read_bytes() == before


def test_update_from_legacy_refuses_before_remote_or_build_effects(runtime, monkeypatch):
    old = _release(runtime, legacy=True)
    _select(runtime, old)
    before = _pair(runtime)
    calls = []
    for name in ("_read_config", "_remote_revision", "_materialize_upstream_candidate", "_write_config"):
        def forbidden(*_args, _name=name, **_kwargs):
            calls.append(_name)
            raise AssertionError("unexpected effect")
        monkeypatch.setattr(runtime, name, forbidden)
    with pytest.raises(AresLocalRuntimeError, match="transition-from-legacy"):
        runtime.update(desktop=False)
    assert calls == []
    assert _pair(runtime) == before


def test_direct_activation_cannot_skip_legacy_transition_binding(runtime):
    old = _release(runtime, legacy=True)
    new = _release(runtime, legacy=False, tag="new")
    _select(runtime, old)
    before = _pair(runtime)
    with pytest.raises(AresLocalRuntimeError):
        runtime._activate(new.parent.name)
    assert _pair(runtime) == before


def test_legacy_descriptor_gaining_a_new_binding_never_uses_legacy_route(runtime):
    old, _new = _bound_pair(runtime)
    descriptor = old.parent / "release.json"
    record = json.loads(descriptor.read_text())
    record["runtime_binding"] = {"controller_contract": 0}
    runtime._atomic_json(descriptor, record)
    before = _pair(runtime)
    with pytest.raises(AresLocalRuntimeError):
        runtime.rollback()
    assert _pair(runtime) == before


def test_post_backout_forward_selection_uses_strict_new_contract(runtime):
    old, new = _bound_pair(runtime)
    assert runtime.rollback() == old.parent.name
    assert runtime.rollback() == new.parent.name
    assert runtime.active_release()[0] == new.parent.name


def test_transition_binding_refuses_an_old_built_new_controller_without_capability(runtime):
    old = _release(runtime, legacy=True)
    new = _release(runtime, legacy=False, tag="new")
    _select(runtime, old)
    (new / "ares_runtime" / "local_runtime.py").write_text("LOCAL_LIFECYCLE_CONTRACT = 1\n")
    binding = runtime._probe_legacy_release(old, desktop=False)
    before = (new.parent / "release.json").read_bytes()
    with pytest.raises(AresLocalRuntimeError):
        runtime._record_legacy_rollback_binding(new, binding)
    assert (new.parent / "release.json").read_bytes() == before


def test_finalization_publishes_both_binding_families_in_one_atomic_record(runtime, monkeypatch):
    old = _release(runtime, legacy=True)
    new = _release(runtime, legacy=False, tag="new")
    _select(runtime, old)
    binding = runtime._probe_legacy_release(old, desktop=False)
    descriptor = new.parent / "release.json"
    record = json.loads(descriptor.read_text())
    record.pop("runtime_binding")
    runtime._atomic_json(descriptor, record)
    original = runtime._atomic_json
    writes = []
    def observe(path, value):
        if path == descriptor:
            writes.append(json.loads(json.dumps(value)))
        original(path, value)
    monkeypatch.setattr(runtime, "_atomic_json", observe)
    runtime._record_final_runtime_binding(new, legacy_rollback_binding=binding)
    assert len(writes) == 1
    assert writes[0]["runtime_binding"]["controller_contract"] == 1
    assert writes[0]["legacy_rollback_binding"] == binding
    runtime._require_complete_release(new, desktop=False)


def test_cli_dispatch_carries_explicit_transition_into_real_setup(runtime, monkeypatch):
    from ares_runtime.local_runtime import main
    old = _release(runtime, legacy=True)
    new = _release(runtime, legacy=False, tag="new")
    _select(runtime, old)
    _setup_seams(runtime, monkeypatch)
    class FixtureRuntimeFactory(AresLocalRuntime):
        def __new__(cls):
            return runtime
    monkeypatch.setattr("ares_runtime.local_runtime.AresLocalRuntime", FixtureRuntimeFactory)
    main(["setup", "--source", str(new), "--no-desktop", "--no-gateway",
          "--transition-from-legacy", old.parent.name])
    assert runtime.active_release()[0] == new.parent.name
    assert runtime.rollback() == old.parent.name


@pytest.mark.parametrize("payload", [b'{"revision":"a","revision":"b"}', b'{"value":NaN}'])
def test_transition_identity_decoder_rejects_ambiguous_json(payload):
    from ares_runtime.legacy_transition import LegacyTransitionError, decode_identity
    with pytest.raises(LegacyTransitionError):
        decode_identity(payload)


def test_probe_output_limit_is_enforced_without_returning_raw_child_text(runtime):
    from ares_runtime.legacy_transition import LegacyTransitionError, bounded_probe
    with pytest.raises(LegacyTransitionError, match="output limit"):
        bounded_probe([sys.executable, "-I", "-c", "print('fixture-only' * 20000)"],
                      cwd=runtime.paths.state_root, home=runtime.paths.agent_home)


def test_probe_timeout_reaps_its_child(runtime):
    from ares_runtime.legacy_transition import LegacyTransitionError, bounded_probe
    with pytest.raises(LegacyTransitionError, match="timed out"):
        bounded_probe([sys.executable, "-I", "-c", "import time; time.sleep(30)"],
                      cwd=runtime.paths.state_root, home=runtime.paths.agent_home, timeout=0.1)


def test_digest_rejects_fifo_without_waiting_for_a_writer(runtime):
    import os
    from ares_runtime.legacy_transition import LegacyTransitionError, file_digest
    fifo = runtime.paths.state_root / "identity-fifo"
    os.mkfifo(fifo)
    with pytest.raises(LegacyTransitionError, match="regular file"):
        file_digest(fifo)


def test_qualified_previous_transition_candidate_reuses_without_metadata_write(runtime, monkeypatch):
    old, new = _bound_pair(runtime)
    assert runtime.rollback() == old.parent.name
    binding = runtime._probe_legacy_release(old, desktop=False)
    descriptor = new.parent / "release.json"
    before = (descriptor.read_bytes(), descriptor.stat().st_ino)
    pair = _pair(runtime)
    for name in ("_atomic_json", "_build_runtime", "_refresh_moved_editable_install"):
        def forbidden(*_args, _name=name, **_kwargs):
            raise AssertionError("readonly reuse reached " + _name)
        monkeypatch.setattr(runtime, name, forbidden)
    runtime._materialize("unused", new.parent.name, desktop=False, legacy_rollback_binding=binding)
    assert (descriptor.read_bytes(), descriptor.stat().st_ino) == before
    assert _pair(runtime) == pair


def test_explicit_setup_reselects_qualified_previous_without_rewriting_it(runtime, monkeypatch):
    old, new = _bound_pair(runtime)
    assert runtime.rollback() == old.parent.name
    descriptor = new.parent / "release.json"
    before = (descriptor.read_bytes(), descriptor.stat().st_ino)
    _setup_seams(runtime, monkeypatch)
    runtime.setup(new, desktop=False, gateway=False, seed_from=runtime.paths.agent_home,
                  transition_from_legacy=old.parent.name)
    assert runtime.active_release()[0] == new.parent.name
    assert (descriptor.read_bytes(), descriptor.stat().st_ino) == before
