"""Task 1.1 admission routing: rejected/absent uv must route to the pip tier.

The round-13 public review found the migrated fixtures only pin the
*admitted* path. These tests drive tools.lazy_deps' real ladder (no stub of
resolve_uv_admitted) through one capturing subprocess fake, asserting:

- a managed uv that is out-of-range or unreadable is probed (metadata read)
  but NEVER used for an install; the ladder routes to ``python -m pip``;
- a PATH uv that is out-of-range is likewise probed and rejected;
- uv absent everywhere means no probe at all and straight pip;
- an in-range managed uv (control) is probed then used for the install.

Patched surfaces: ``subprocess.run`` and ``shutil.which`` on the stdlib
module objects (shared by every importer) and ``managed_uv.get_hermes_home``
for the managed-binary location. No real subprocess ever runs.
"""

from __future__ import annotations

import shutil as _shutil_module
import sys
from types import SimpleNamespace

import pytest

from tools import lazy_deps

IN_RANGE = "uv 0.12.24\n"
OUT_OF_RANGE = "uv 0.13.5\n"  # >= 0.13.0 exclusive bound


def _completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture()
def ladder(monkeypatch, tmp_path):
    """Capturing ladder: one subprocess fake, scenario knobs, empty HERMES_HOME."""
    recorded: list[list[str]] = []
    state = {"version_stdout": OUT_OF_RANGE, "version_rc": 0, "which": {"uv": None}}

    real_which = _shutil_module.which

    def fake_which(name):
        if name in state["which"]:
            return state["which"][name]
        return real_which(name)

    def fake_run(cmd, **kwargs):
        argv = [str(c) for c in cmd]
        recorded.append(argv)
        if argv and argv[-1] == "--version" and argv[0].endswith("uv"):
            return _completed(state["version_rc"], state["version_stdout"])
        if len(argv) >= 3 and argv[1] == "-m" and argv[2] == "pip":
            return _completed(0, "pip 25.0 from example (python 3.14)\n")
        return _completed(0, "installed\n")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("shutil.which", fake_which)

    import hermes_cli.managed_uv as muv

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setattr(muv, "get_hermes_home", lambda: home)
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "0")
    return SimpleNamespace(recorded=recorded, state=state, home=home)


def _make_managed_uv(home, name="uv"):
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    uv = bin_dir / name
    uv.write_text("#!/bin/sh\n")
    import stat

    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    return str(uv)


def _pip_install_calls(recorded):
    return [c for c in recorded if len(c) >= 3 and c[-2:] == ["install", "left-pad"]]


def _uv_install_calls(recorded):
    return [c for c in recorded if c and c[0].endswith("uv") and c[1:3] == ["pip", "install"]]


def test_managed_uv_out_of_range_is_probed_then_routed_to_pip(ladder):
    managed = _make_managed_uv(ladder.home)
    res = lazy_deps._venv_pip_install(("left-pad",))
    assert res.success
    assert [managed, "--version"] in ladder.recorded, ladder.recorded
    assert _uv_install_calls(ladder.recorded) == [], "unadmitted uv must never install"
    installs = _pip_install_calls(ladder.recorded)
    assert installs, ladder.recorded
    assert installs[0][:3] == [sys.executable, "-m", "pip"]


def test_managed_uv_unreadable_version_routes_to_pip(ladder):
    managed = _make_managed_uv(ladder.home)
    ladder.state["version_rc"] = 1
    ladder.state["version_stdout"] = ""
    res = lazy_deps._venv_pip_install(("left-pad",))
    assert res.success
    assert [managed, "--version"] in ladder.recorded
    assert _uv_install_calls(ladder.recorded) == []
    installs = _pip_install_calls(ladder.recorded)
    assert installs and installs[0][:3] == [sys.executable, "-m", "pip"]


def test_absent_uv_no_probe_straight_to_pip(ladder):
    res = lazy_deps._venv_pip_install(("left-pad",))
    assert res.success
    assert not any(c and c[0].endswith("uv") for c in ladder.recorded), ladder.recorded
    installs = _pip_install_calls(ladder.recorded)
    assert installs and installs[0][:3] == [sys.executable, "-m", "pip"]


def test_path_uv_out_of_range_is_probed_then_routed_to_pip(ladder):
    ladder.state["which"]["uv"] = "/usr/bin/uv"
    res = lazy_deps._venv_pip_install(("left-pad",))
    assert res.success
    assert ["/usr/bin/uv", "--version"] in ladder.recorded
    assert _uv_install_calls(ladder.recorded) == [], "rejected PATH uv must never install"
    installs = _pip_install_calls(ladder.recorded)
    assert installs and installs[0][:3] == [sys.executable, "-m", "pip"]


def test_in_range_managed_uv_is_probed_then_used(ladder):
    managed = _make_managed_uv(ladder.home)
    ladder.state["version_stdout"] = IN_RANGE
    res = lazy_deps._venv_pip_install(("left-pad",))
    assert res.success
    assert [managed, "--version"] in ladder.recorded
    uv_installs = _uv_install_calls(ladder.recorded)
    assert uv_installs and uv_installs[0][0] == managed, ladder.recorded
