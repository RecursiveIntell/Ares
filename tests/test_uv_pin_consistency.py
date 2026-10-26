"""Task 1.1 (PLAN-FINAL) consistency tests: one pinned uv everywhere.

The pin source is .github/uv-version. Two layers:
- a REPO-WIDE scan (git-tracked files) asserting every uv installer
  acquisition URL embeds the pinned version — no fixed allowlist of files,
  so a new surface cannot silently ship unversioned (verifier finding on
  the three-file scan);
- cross-surface pin agreement (scripts, managed_uv, composite action) and
  the pin lying inside the reviewed policy range.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Raw string: class excludes whitespace and the three delimiters (backslash-s
# inside a raw string is regex \s, not a literal backslash and 's').
ACQ_URL_RX = re.compile(
    r"(?:https://astral[.]sh/uv/(?:([\d.]+)/)?install[.](?:sh|ps1))"
    r"|(?:https://github[.]com/astral-sh/uv/releases/(?:download/)?(?:([\d.]+)/)?uv-installer[.]ps1)"
)
VERSIONED_URL_RX = re.compile(
    r"(?:https://astral[.]sh/uv/([\d.]+)/install[.](?:sh|ps1))"
    r"|(?:https://github[.]com/astral-sh/uv/releases/(?:download/)?([\d.]+)/uv-installer[.]ps1)"
)
SCAN_EXCLUDES = (
    ".venv/", ".git/", "node_modules/", "dist/", "build/", ".hermes-runtime/",
    # Vendored third-party reference docs (unsloth/axolotl upstream manuals):
    # not Ares install surfaces; their uv one-liners are upstream content.
    "optional-skills/mlops/training/unsloth/references/",
    "optional-skills/mlops/training/axolotl/references/",
)


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


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, timeout=60, check=True
    )
    files = []
    for line in out.stdout.splitlines():
        if not line or any(x in line for x in SCAN_EXCLUDES):
            continue
        p = REPO / line
        if p.exists() and p.stat().st_size < 2_000_000:
            files.append(p)
    return files


def test_pin_file_declares_exact_version() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", _load_pin()), "pin file must be a single x.y.z line"


def test_every_tracked_acquisition_url_is_versioned() -> None:
    """REPO-WIDE: no tracked file may fetch an unversioned/other-version uv
    installer. Versioned URLs must carry exactly the pin."""
    pin = _load_pin()
    violations: list[str] = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Expand shell/Python pin references so templated URLs are checkable.
        expanded = text.replace("${UV_PIN}", pin).replace("{UV_PIN}", pin).replace("{0}", pin)
        for m in ACQ_URL_RX.finditer(expanded):
            url = m.group(0)
            vm = VERSIONED_URL_RX.match(url)
            if vm is None:
                violations.append(f"{path.relative_to(REPO)}: unversioned uv installer URL {url}")
            elif pin not in vm.groups():
                violations.append(f"{path.relative_to(REPO)}: uv URL pins {url} != {pin}")
    assert not violations, "unversioned/mismatched uv installer URLs:\n" + "\n".join(violations)


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
    """Network sanity: the versioned installer path must exist (E-33).
    Skips (does not fail) when the host has no network access."""
    import pytest

    pin = _load_pin()
    try:
        r = subprocess.run(
            ["curl", "-sIL", "-o", "/dev/null", "-w", "%{http_code}",
             f"https://astral.sh/uv/{pin}/install.sh"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        pytest.skip(f"network check unavailable: {exc}")
    # curl exit codes: 6 = could not resolve host, 7 = could not connect,
    # 28 = timeout — offline/sandboxed hosts skip rather than fail.
    if r.returncode in (6, 7, 28):
        pytest.skip(f"network unavailable (curl rc={r.returncode})")
    assert r.stdout.strip() == "200", f"versioned installer URL returned {r.stdout.strip()!r}"


def test_pin_agrees_across_all_surfaces() -> None:
    """Single source of truth: .github/uv-version; every embedder must match it."""
    pin = _load_pin()
    for name in ("install.sh", "scripts/install.sh", "setup-hermes.sh"):
        m = re.search(r'^\s*UV_PIN="(\d+\.\d+\.\d+)"', (REPO / name).read_text(encoding="utf-8"), re.M)
        assert m is not None, f"{name}: missing UV_PIN literal"
        assert m.group(1) == pin, f"{name}: UV_PIN={m.group(1)} disagrees with pin file {pin}"
    m = re.search(r'^UV_PIN = "(\d+\.\d+\.\d+)"', (REPO / "hermes_cli" / "managed_uv.py").read_text(encoding="utf-8"), re.M)
    assert m is not None, "managed_uv.py: missing UV_PIN constant"
    assert m.group(1) == pin, f"managed_uv.py: UV_PIN={m.group(1)} disagrees with pin file {pin}"
    action = REPO / ".github" / "actions" / "setup-uv-pinned" / "action.yml"
    assert action.exists(), "missing .github/actions/setup-uv-pinned/action.yml"
    m = re.search(r"default: '(\d+\.\d+\.\d+)'", action.read_text(encoding="utf-8"))
    assert m is not None, "composite action: missing default version input"
    assert m.group(1) == pin, f"composite action default {m.group(1)} disagrees with pin file {pin}"
