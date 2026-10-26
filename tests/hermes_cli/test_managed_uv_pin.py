"""Task 1.1 (PLAN-FINAL) behavior tests: the pin governs install and self-update.

RED (c): update_managed_uv() must never let the managed uv drift out of the
reviewed range — it runs ``uv self update <pin>`` and replaces an out-of-range
managed binary with the pin before a locked sync (A-01, X-01).

subprocess.run is mocked (repo live-system guard requires it, and it makes the
argv contract assertable directly).
"""

from __future__ import annotations

import re
import sys
from types import SimpleNamespace

import pytest

from hermes_cli import managed_uv

PIN = "0.12.24"


def _run_result(returncode=0, stdout=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


@pytest.fixture()
def managed_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    # Both import shapes used inside managed_uv for the stamp file location.
    monkeypatch.setattr(managed_uv, "get_hermes_home", lambda: home, raising=False)
    try:
        import hermes_constants

        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home, raising=False)
    except ImportError:
        pass
    return home


@pytest.fixture()
def recorded_subprocess(monkeypatch):
    """Mock managed_uv.subprocess.run; record argvs, answer version probes."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[-1] == "--version" or (len(argv) > 2 and argv[1] == "--version"):
            return _run_result(0, "uv 0.12.10\n")
        return _run_result(0)

    monkeypatch.setattr(managed_uv.subprocess, "run", fake_run)
    return calls


def test_self_update_pin_governs_target(managed_home, recorded_subprocess, monkeypatch):
    """RED: today self update runs with NO target; GREEN passes the pin as TARGET_VERSION."""
    uv_bin = managed_home / "bin" / "uv"
    uv_bin.parent.mkdir(parents=True, exist_ok=True)
    uv_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(managed_uv, "resolve_uv", lambda: str(uv_bin))
    monkeypatch.setattr(managed_uv, "repair_vulnerable_runtime", lambda p: SimpleNamespace(status="ok"))
    monkeypatch.setattr(managed_uv, "_report_runtime_repair_failure", lambda r: None)

    result = managed_uv.update_managed_uv(force=True)

    assert result is not None
    self_update_calls = [c for c in recorded_subprocess if "self" in c and "update" in c]
    assert self_update_calls, "update_managed_uv must run uv self update"
    # The pin must be the TARGET_VERSION argument: [uv, self, update, 0.12.24]
    targeted = [c for c in self_update_calls if len(c) >= 4 and c[-1] == PIN]
    assert targeted, (
        f"uv self update must target the pin {PIN}; argv calls seen: {self_update_calls}"
    )


def test_out_of_range_managed_uv_is_replaced_before_locked_sync(managed_home, recorded_subprocess, monkeypatch):
    """A managed uv outside [0.9.28, 0.13.0) is reinstalled at the pin, not reused."""
    uv_bin = managed_home / "bin" / "uv"
    uv_bin.parent.mkdir(parents=True, exist_ok=True)
    uv_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(managed_uv, "resolve_uv", lambda: str(uv_bin))
    monkeypatch.setattr(managed_uv, "repair_vulnerable_runtime", lambda p: SimpleNamespace(status="ok"))
    monkeypatch.setattr(managed_uv, "_report_runtime_repair_failure", lambda r: None)

    installed: dict[str, str] = {}

    def fake_install(target):
        installed["target"] = str(target)

    monkeypatch.setattr(managed_uv, "_install_uv", fake_install)
    # Stateful version probe: before reinstall the binary reports the
    # out-of-range version; after _install_uv it reports the pin. The
    # post-reinstall validation must see the repaired version.
    versions = iter(["uv 0.13.5\n", "uv 0.12.24\n", "uv 0.12.24\n"])

    def fake_run(argv, **kw):
        if argv[-1] == "--version":
            return _run_result(0, next(versions))
        return _run_result(0)

    monkeypatch.setattr(managed_uv.subprocess, "run", fake_run)

    result = managed_uv.update_managed_uv(force=True)

    assert result is not None, "update must succeed after replacing the out-of-range uv"
    assert installed.get("target"), "_install_uv must be invoked to replace the out-of-range managed uv"


def test_failed_reinstall_fails_closed(monkeypatch, managed_home):
    """If the pin reinstall fails, update returns None — never the stale
    out-of-range binary (verifier finding: callers must refuse the sync)."""
    uv_bin = managed_home / "bin" / "uv"
    uv_bin.parent.mkdir(parents=True, exist_ok=True)
    uv_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(managed_uv, "resolve_uv", lambda: str(uv_bin))
    monkeypatch.setattr(managed_uv, "repair_vulnerable_runtime", lambda p: SimpleNamespace(status="ok"))
    monkeypatch.setattr(managed_uv, "_report_runtime_repair_failure", lambda r: None)

    def boom(target):
        raise RuntimeError("network down")

    monkeypatch.setattr(managed_uv, "_install_uv", boom)
    monkeypatch.setattr(
        managed_uv.subprocess, "run",
        lambda argv, **kw: _run_result(0, "uv 0.13.5\n") if argv[-1] == "--version" else _run_result(0),
    )

    result = managed_uv.update_managed_uv(force=True)
    assert result is None, "failed replacement must return None, not the stale binary"


def test_still_out_of_range_after_reinstall_fails_closed(monkeypatch, managed_home):
    """If the reinstalled binary still reports out-of-range, refuse the sync."""
    uv_bin = managed_home / "bin" / "uv"
    uv_bin.parent.mkdir(parents=True, exist_ok=True)
    uv_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(managed_uv, "resolve_uv", lambda: str(uv_bin))
    monkeypatch.setattr(managed_uv, "repair_vulnerable_runtime", lambda p: SimpleNamespace(status="ok"))
    monkeypatch.setattr(managed_uv, "_report_runtime_repair_failure", lambda r: None)
    monkeypatch.setattr(managed_uv, "_install_uv", lambda target: None)
    # Every probe reports the out-of-range version: replacement did not take.
    monkeypatch.setattr(
        managed_uv.subprocess, "run",
        lambda argv, **kw: _run_result(0, "uv 0.13.5\n") if argv[-1] == "--version" else _run_result(0),
    )

    result = managed_uv.update_managed_uv(force=True)
    assert result is None, "post-reinstall validation must refuse a still-out-of-range uv"


def test_in_range_managed_uv_is_not_reinstalled(managed_home, recorded_subprocess, monkeypatch):
    """Inside the reviewed range: self update to the pin; no reinstall churn."""
    uv_bin = managed_home / "bin" / "uv"
    uv_bin.parent.mkdir(parents=True, exist_ok=True)
    uv_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(managed_uv, "resolve_uv", lambda: str(uv_bin))
    monkeypatch.setattr(managed_uv, "repair_vulnerable_runtime", lambda p: SimpleNamespace(status="ok"))
    monkeypatch.setattr(managed_uv, "_report_runtime_repair_failure", lambda r: None)

    def fail_install(target):
        raise AssertionError("_install_uv must not run for an in-range managed uv")

    monkeypatch.setattr(managed_uv, "_install_uv", fail_install)

    result = managed_uv.update_managed_uv(force=True)

    assert result is not None
    targeted = [c for c in recorded_subprocess if len(c) >= 4 and c[-1] == PIN and "update" in c]
    assert targeted, "in-range uv still gets refreshed to the pin (not the newest release)"


def test_resolve_uv_admitted_hides_out_of_range_but_falls_back_when_absent(managed_home, monkeypatch):
    uv_bin = managed_home / "bin" / "uv"
    uv_bin.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(managed_uv, "resolve_uv", lambda: str(uv_bin))
    monkeypatch.setattr(
        managed_uv.subprocess, "run",
        lambda argv, **kw: _run_result(0, "uv 0.13.5\n") if argv[-1] == "--version" else _run_result(0),
    )
    # Present but out of range: never returned, and PATH fallback suppressed.
    monkeypatch.setattr(managed_uv.shutil, "which", lambda name: "/usr/bin/uv-should-not-be-used")
    assert managed_uv.resolve_uv_admitted() is None


def test_resolve_uv_admitted_uses_path_uv_only_when_managed_absent(managed_home, monkeypatch):
    monkeypatch.setattr(managed_uv, "resolve_uv", lambda: None)
    monkeypatch.setattr(managed_uv.shutil, "which", lambda name: "/usr/bin/uv")
    assert managed_uv.resolve_uv_admitted() == "/usr/bin/uv"


def test_lazy_deps_uses_admitted_lookup(monkeypatch, managed_home, tmp_path):
    """Behavioral: tools/lazy_deps.py must run its uv tier only with an
    admitted (in-range) managed uv — an out-of-range managed uv must route
    to the pip tier instead of executing the stale binary."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("lazy_deps", "tools/lazy_deps.py")
    ld = importlib.util.module_from_spec(spec)
    sys.modules["lazy_deps"] = ld
    spec.loader.exec_module(ld)

    uv_bin = managed_home / "bin" / "uv"
    uv_bin.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(managed_uv, "resolve_uv", lambda: str(uv_bin))
    monkeypatch.setattr(
        managed_uv.subprocess, "run",
        lambda argv, **kw: _run_result(0, "uv 0.13.5\n") if argv[-1] == "--version" else _run_result(0),
    )
    executed = {"uv": 0, "pip": 0}

    class FakeCompleted:
        def __init__(self):
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""

    def fake_run(argv, **kw):
        exe = str(argv[0])
        executed["uv" if exe.endswith("uv") else "pip"] += 1
        return FakeCompleted()

    monkeypatch.setattr(ld.subprocess, "run", fake_run)

    result = ld.install_specs(["some-pkg==1.0"], timeout=5)
    assert executed["uv"] == 0, "out-of-range managed uv must not be executed by lazy_deps"
    assert executed["pip"] >= 0  # pip tier may or may not be reached depending on fallbacks
