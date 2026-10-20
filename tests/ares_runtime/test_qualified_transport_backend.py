"""The leased transport's version gate is enforced by packaging, not discovered at runtime.

bounded_transport refuses every governed send unless httpx/httpcore/anyio are
the exact qualified releases. These tests hold the gate, pyproject.toml and
uv.lock in lockstep, and check that a mismatch is reported precisely by both
the gate and ``ares doctor``.
"""
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import httpx
import pytest
from packaging.requirements import Requirement

from ares_runtime import transport_lease
from ares_runtime.transport_lease import (
    QUALIFIED_TRANSPORT_BACKEND,
    transport_backend_mismatches,
)
from hermes_cli.middleware import EgressDispatchDenied

ROOT = Path(__file__).resolve().parents[2]


def _declared_dependencies():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return {Requirement(spec).name.lower(): Requirement(spec) for spec in project["dependencies"]}


def _locked_versions():
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {package["name"]: package["version"] for package in lock["package"]}


@pytest.mark.parametrize("package", sorted(QUALIFIED_TRANSPORT_BACKEND))
def test_pyproject_pins_every_qualified_package_to_its_qualified_version(package):
    requirement = _declared_dependencies().get(package)
    assert requirement is not None, f"{package} must be a direct, pinned dependency"
    specifiers = list(requirement.specifier)
    assert len(specifiers) == 1 and specifiers[0].operator == "=="
    assert specifiers[0].version == QUALIFIED_TRANSPORT_BACKEND[package]


@pytest.mark.parametrize("package", sorted(QUALIFIED_TRANSPORT_BACKEND))
def test_lockfile_resolves_every_qualified_package_to_its_qualified_version(package):
    assert _locked_versions()[package] == QUALIFIED_TRANSPORT_BACKEND[package]


@pytest.mark.linux_only
def test_environment_built_from_the_lock_is_qualified():
    assert transport_backend_mismatches() == ()


def test_mismatch_names_the_package_found_and_required(monkeypatch):
    import importlib.metadata

    real = importlib.metadata.version
    monkeypatch.setattr(
        importlib.metadata, "version",
        lambda name: "4.15.1" if name == "anyio" else real(name),
    )
    assert f"anyio 4.15.1 (qualified {QUALIFIED_TRANSPORT_BACKEND['anyio']})" in transport_backend_mismatches()


def test_imported_module_version_wins_over_installed_metadata(monkeypatch):
    """A shadowing copy is the code that runs, so its __version__ is what counts."""
    import httpcore

    monkeypatch.setattr(httpcore, "__version__", "1.0.99")
    assert f"httpcore 1.0.99 (qualified {QUALIFIED_TRANSPORT_BACKEND['httpcore']})" in transport_backend_mismatches()


def test_missing_metadata_is_reported_not_raised(monkeypatch):
    import importlib.metadata

    real = importlib.metadata.version

    def version(name):
        if name == "anyio":
            raise importlib.metadata.PackageNotFoundError(name)
        return real(name)

    monkeypatch.setattr(importlib.metadata, "version", version)
    assert any(item.startswith("anyio missing") for item in transport_backend_mismatches())


def test_gate_refuses_with_the_specific_mismatch(monkeypatch):
    monkeypatch.setattr(transport_lease, "transport_backend_mismatches",
                        lambda: ("anyio 4.15.1 (qualified 4.12.1)",))
    with pytest.raises(EgressDispatchDenied) as denied:
        transport_lease.bounded_transport(httpx.HTTPTransport(), budget=None)
    assert denied.value.code == "UNSUPPORTED_TRANSPORT"
    assert "anyio 4.15.1 (qualified 4.12.1)" in str(denied.value)


def test_doctor_probe_runs_in_a_real_interpreter(tmp_path):
    """The doctor's probe program executes against a real interpreter and source tree."""
    from ares_runtime.local_runtime import AresLocalRuntime

    runtime = object.__new__(AresLocalRuntime)
    label, passed, detail = runtime._transport_backend_check(Path(sys.executable), ROOT)
    expected = list(transport_backend_mismatches())
    assert label == "governed transport backend"
    assert passed is (not expected)
    assert detail == ("qualified" if not expected else "; ".join(expected))


def test_doctor_reports_mismatches_from_the_selected_interpreter(monkeypatch, tmp_path):
    from ares_runtime import local_runtime

    def run(command, **_):
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(["anyio 4.15.1 (qualified 4.12.1)"]), stderr="")

    monkeypatch.setattr(local_runtime.subprocess, "run", run)
    runtime = object.__new__(local_runtime.AresLocalRuntime)
    assert runtime._transport_backend_check(Path("/selected/python"), tmp_path) == (
        "governed transport backend", False, "anyio 4.15.1 (qualified 4.12.1)")


def test_doctor_reports_a_failed_probe_without_raising(monkeypatch, tmp_path):
    from ares_runtime import local_runtime

    def run(command, **_):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="Traceback\nImportError: boom")

    monkeypatch.setattr(local_runtime.subprocess, "run", run)
    runtime = object.__new__(local_runtime.AresLocalRuntime)
    assert runtime._transport_backend_check(Path("/selected/python"), tmp_path) == (
        "governed transport backend", False, "probe failed: ImportError: boom")
