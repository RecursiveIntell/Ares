"""Task 1.1 (PLAN-FINAL) consistency tests: one pinned uv everywhere.

Static invariants (see .github/uv-version as the single pin source):
- every uv installer URL in the acquisition surfaces embeds the pinned version;
- the pin lies inside the reviewed policy range in scripts/install_uv_policy.py;
- Ares-owned workflows (ares-*.yml) that use uv go through the pinned composite
  action.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
URL_RX = re.compile(r"https://astral[.]sh/uv/[^\\s\"')]+")


def _load_pin() -> str:
    pin_file = REPO / ".github" / "uv-version"
    assert pin_file.exists(), f"missing pin source file {pin_file}"
    return pin_file.read_text().strip()


def _load_policy_module():
    spec = importlib.util.spec_from_file_location(
        "install_uv_policy", REPO / "scripts" / "install_uv_policy.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("install_uv_policy", mod)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_pin_file_declares_exact_version() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", _load_pin()), "pin file must be a single x.y.z line"


def test_every_installer_url_is_versioned() -> None:
    pin = _load_pin()
    surfaces = [
        REPO / "install.sh",
        REPO / "scripts" / "install.sh",
        REPO / "hermes_cli" / "managed_uv.py",
    ]
    for path in surfaces:
        assert path.exists(), path
        text = path.read_text(encoding="utf-8")
        # Expand shell/Python pin references so the URL check sees the literal version.
        text = text.replace("${UV_PIN}", pin).replace("{UV_PIN}", pin)
        urls = URL_RX.findall(text)
        assert urls, f"{path.name}: expected at least one uv installer URL"
        for url in urls:
            assert f"/uv/{pin}/" in url, f"{path.name}: unversioned or non-pinned URL {url}"


def test_pin_inside_reviewed_policy_range() -> None:
    pin = _load_pin()
    mod = _load_policy_module()
    assert hasattr(mod, "UV_MIN_VERSION") and hasattr(mod, "UV_MAX_EXCLUSIVE"), (
        "install_uv_policy.py must expose UV_MIN_VERSION/UV_MAX_EXCLUSIVE for consistency testing"
    )
    parts = tuple(int(x) for x in pin.split("."))
    assert mod.UV_MIN_VERSION <= parts < mod.UV_MAX_EXCLUSIVE, (
        f"pin {pin} outside reviewed range {mod.UV_MIN_VERSION}..<{mod.UV_MAX_EXCLUSIVE}"
    )


def test_ares_owned_uv_workflows_use_pinned_action() -> None:
    wf_dir = REPO / ".github" / "workflows"
    offenders: list[str] = []
    for wf in sorted(wf_dir.glob("ares-*.yml")):
        text = wf.read_text(encoding="utf-8")
        if "astral.sh/uv/" in text:
            offenders.append(wf.name)
        if re.search(r"\buv\b", text) and "setup-uv-pinned" not in text:
            offenders.append(f"{wf.name} (uses uv without setup-uv-pinned)")
    assert not offenders, f"Ares-owned workflows must use .github/actions/setup-uv-pinned: {offenders}"


def test_pinned_url_actually_resolves() -> None:
    """Network sanity: the versioned installer path must exist (E-33)."""
    import subprocess

    pin = _load_pin()
    r = subprocess.run(
        ["curl", "-sIL", "-o", "/dev/null", "-w", "%{http_code}", f"https://astral.sh/uv/{pin}/install.sh"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.stdout.strip() == "200", f"versioned installer URL returned {r.stdout.strip()!r}"


def test_pin_agrees_across_all_surfaces() -> None:
    """Single source of truth: .github/uv-version; every embedder must match it."""
    pin = _load_pin()
    # POSIX installers embed UV_PIN="x.y.z"
    for name in ("install.sh", "scripts/install.sh"):
        m = re.search(r'^UV_PIN="(\d+\.\d+\.\d+)"', (REPO / name).read_text(encoding="utf-8"), re.M)
        assert m is not None, f"{name}: missing UV_PIN literal"
        assert m.group(1) == pin, f"{name}: UV_PIN={m.group(1)} disagrees with pin file {pin}"
    # managed_uv.py embeds UV_PIN = "x.y.z"
    m = re.search(r'^UV_PIN = "(\d+\.\d+\.\d+)"', (REPO / "hermes_cli" / "managed_uv.py").read_text(encoding="utf-8"), re.M)
    assert m is not None, "managed_uv.py: missing UV_PIN constant"
    assert m.group(1) == pin, f"managed_uv.py: UV_PIN={m.group(1)} disagrees with pin file {pin}"
    # composite action default input
    action = REPO / ".github" / "actions" / "setup-uv-pinned" / "action.yml"
    assert action.exists(), "missing .github/actions/setup-uv-pinned/action.yml"
    m = re.search(r"default: '(\d+\.\d+\.\d+)'", action.read_text(encoding="utf-8"))
    assert m is not None, "composite action: missing default version input"
    assert m.group(1) == pin, f"composite action default {m.group(1)} disagrees with pin file {pin}"
