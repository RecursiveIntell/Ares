"""Discovery fixtures are layout data, not native cross-platform execution."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli.desktop_package import packaged_executables, rebuilt_mac_bundle


@pytest.fixture
def desktop(tmp_path):
    desktop = tmp_path / "apps" / "desktop"
    desktop.mkdir(parents=True)
    (desktop / "package.json").write_text(json.dumps({
        "name": "hermes", "productName": "Ares",
        "build": {"productName": "Ares", "executableName": "Ares"},
    }))
    return desktop


def executable(desktop, relative):
    path = desktop / "release" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture, never executed")
    return path


@pytest.mark.parametrize("platform,relative", [
    ("linux", "linux-unpacked/Ares"),
    ("linux", "linux-arm64-unpacked/Ares"),
    ("linux", "linux-armv7l-unpacked/Ares"),
    ("win32", "win-unpacked/Ares.exe"),
    ("win32", "win-ia32-unpacked/Ares.exe"),
    ("win32", "win-arm64-unpacked/Ares.exe"),
    ("darwin", "mac/Ares.app/Contents/MacOS/Ares"),
    ("darwin", "mac-arm64/Ares.app/Contents/MacOS/Ares"),
    ("darwin", "mac-universal/Ares.app/Contents/MacOS/Ares"),
])
def test_current_builder_layout(desktop, platform, relative):
    expected = executable(desktop, relative)
    assert packaged_executables(desktop, platform) == [expected]


def test_manifest_is_the_name_authority(desktop):
    (desktop / "package.json").write_text(json.dumps({
        "productName": "Wrong", "build": {"productName": "Custom App", "executableName": "Custom Runner"},
    }))
    expected = executable(desktop, "mac/Custom App.app/Contents/MacOS/Custom Runner")
    assert packaged_executables(desktop, "darwin") == [expected]
    assert rebuilt_mac_bundle(desktop) == expected.parents[2]


@pytest.mark.parametrize("platform,relative", [
    ("linux", "linux-unpacked/hermes"),
    ("linux", "linux-unpacked/Hermes"),
    ("win32", "win-unpacked/Hermes.exe"),
    ("darwin", "mac/Hermes.app/Contents/MacOS/Hermes"),
])
def test_explicit_legacy_fallback(desktop, platform, relative, caplog):
    (desktop / "package.json").write_text(json.dumps({"productName": "Hermes"}))
    expected = executable(desktop, relative)
    assert packaged_executables(desktop, platform) == [expected]
    if relative.endswith("/hermes"):
        assert "legacy Hermes" in caplog.text


def test_current_identity_wins_over_newer_legacy(desktop):
    current = executable(desktop, "linux-unpacked/Ares")
    legacy = executable(desktop, "linux-unpacked/hermes")
    os.utime(current, (1, 1))
    os.utime(legacy, (2, 2))
    assert packaged_executables(desktop, "linux") == [current]


@pytest.mark.parametrize("bad", ["../Ares", "/tmp/Ares", "Ares/foo", "Ares\\foo", "*", "Ares\n", "Ares.", "", None, []])
def test_malformed_identity_never_falls_back(desktop, bad):
    executable(desktop, "linux-unpacked/hermes")
    (desktop / "package.json").write_text(json.dumps({"build": {"productName": "Ares", "executableName": bad}}))
    assert packaged_executables(desktop, "linux") == []


@pytest.mark.parametrize("contents", ["{", "null", "[]", "{}", '{"build": []}', '{"build": null}'])
def test_malformed_manifest(desktop, contents):
    executable(desktop, "linux-unpacked/hermes")
    (desktop / "package.json").write_text(contents)
    assert packaged_executables(desktop, "linux") == []


def test_missing_manifest_and_candidates_fail_closed(desktop):
    assert packaged_executables(desktop, "linux") == []
    executable(desktop, "linux-unpacked/hermes")
    (desktop / "package.json").unlink()
    assert packaged_executables(desktop, "linux") == []
    assert rebuilt_mac_bundle(desktop) is None


def test_directory_is_not_an_executable(desktop):
    (desktop / "release/linux-unpacked/Ares").mkdir(parents=True)
    assert packaged_executables(desktop, "linux") == []


@pytest.mark.linux_only
def test_symlink_substitution_is_rejected(desktop, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("not a build")
    candidate = executable(desktop, "linux-unpacked/Ares")
    candidate.unlink()
    candidate.symlink_to(outside)
    assert packaged_executables(desktop, "linux") == []


@pytest.mark.linux_only
def test_release_root_symlink_is_rejected(desktop, tmp_path):
    outside = tmp_path / "outside"
    (outside / "linux-unpacked").mkdir(parents=True)
    (outside / "linux-unpacked/Ares").write_text("not a build")
    (desktop / "release").symlink_to(outside, target_is_directory=True)
    assert packaged_executables(desktop, "linux") == []


def test_installed_target_cannot_substitute_for_missing_source(desktop, tmp_path):
    installed = tmp_path / "Applications/Ares.app/Contents/MacOS/Ares"
    installed.parent.mkdir(parents=True)
    installed.write_text("installed")
    assert rebuilt_mac_bundle(desktop) is None
    source = executable(desktop, "mac/Ares.app/Contents/MacOS/Ares")
    assert rebuilt_mac_bundle(desktop) == source.parents[2]
    assert rebuilt_mac_bundle(desktop) != installed.parents[2]


def test_shell_helper_contract(desktop):
    from hermes_cli import desktop_package
    command = [sys.executable, desktop_package.__file__, str(desktop)]
    missing = subprocess.run(command, capture_output=True, text=True)
    assert missing.returncode == 1
    assert missing.stdout == ""
    source = executable(desktop, "mac/Ares.app/Contents/MacOS/Ares")
    found = subprocess.run(command, capture_output=True, text=True)
    assert found.returncode == 0
    assert found.stdout.strip() == str(source.parents[2])


@pytest.mark.linux_only
def test_cli_adapter_detects_current_package(desktop):
    from hermes_cli.main import _desktop_packaged_executable
    expected = executable(desktop, "linux-unpacked/Ares")
    assert _desktop_packaged_executable(desktop) == expected


@pytest.mark.parametrize("platform,relative", [
    ("linux", "linux-unpacked/hermes"),
    ("win32", "win-unpacked/Hermes.exe"),
    ("darwin", "mac/Hermes.app/Contents/MacOS/Hermes"),
])
def test_missing_current_output_never_selects_stale_legacy(desktop, platform, relative):
    executable(desktop, relative)
    assert packaged_executables(desktop, platform) == []
    assert rebuilt_mac_bundle(desktop) is None
