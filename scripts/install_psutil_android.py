#!/usr/bin/env python3
"""Install psutil on Termux/Android by patching upstream platform detection.

psutil's setup currently gates Linux sources behind
``sys.platform.startswith('linux')``. On Termux, Python reports
``sys.platform == 'android'``, so ``pip install psutil`` aborts with
"platform android is not supported" — even though psutil compiles fine
when the Linux source path is reused.

This script downloads the official psutil sdist, applies a one-line
patch (``LINUX = sys.platform.startswith(("linux", "android"))``), and
installs the patched tree with ``pip install --no-build-isolation``.

Usage:
    python scripts/install_psutil_android.py [--pip "/path/to/pip"] [--uv]

When neither flag is given, the script auto-detects ``uv`` on PATH and
falls back to ``<sys.executable> -m pip``.

This is a stopgap. Remove once psutil upstream merges
https://github.com/giampaolo/psutil/pull/2762 and ships a release.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# Keep sibling imports working when invoked as
# ``python scripts/install_psutil_android.py`` from the repo checkout.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli.psutil_android import (
    PSUTIL_URL,
    PsutilAndroidInstallError,
    prepare_patched_psutil_sdist,
)



def _resolve_install_cmd(pip_arg: str | None, prefer_uv: bool) -> list[str]:
    if pip_arg:
        return pip_arg.split()
    def _admitted_uv():
        """Reviewed-range admission (uv-pin contract): run only a uv inside
        [0.9.28, 0.13.0); any other uv (missing, out-of-range, unreadable)
        routes to the stdlib pip fallback."""
        uv = shutil.which("uv")
        if not uv:
            return None
        try:
            out = subprocess.run([uv, "--version"], capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=15, check=False).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        import re as _re
        m = _re.fullmatch(r"uv (\d+)\.(\d+)\.(\d+)(?: \S.*)?", (out or "").strip())
        if not m:
            return None
        version = tuple(int(g) for g in m.groups())
        return uv if (0, 9, 28) <= version < (0, 13, 0) else None

    if prefer_uv:
        uv = _admitted_uv()
        if not uv:
            sys.exit("--uv requested but no reviewed-range uv on PATH")
        return [uv, "pip"]
    auto_uv = _admitted_uv()
    if auto_uv:
        return [auto_uv, "pip"]
    return [sys.executable, "-m", "pip"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pip",
        help="Explicit installer command (e.g. '/usr/bin/uv pip' or 'python -m pip')",
    )
    parser.add_argument(
        "--uv",
        action="store_true",
        help="Force using uv (errors out if uv is not on PATH)",
    )
    args = parser.parse_args()

    install_cmd_prefix = _resolve_install_cmd(args.pip, args.uv)

    print(
        "→ Termux/Android: prebuilding psutil with Linux source path "
        "compatibility shim (see psutil#2762)..."
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / "psutil.tar.gz"
        urllib.request.urlretrieve(PSUTIL_URL, archive)
        try:
            src_root = prepare_patched_psutil_sdist(archive, tmp_path)
        except PsutilAndroidInstallError as exc:
            sys.exit(str(exc))

        cmd = install_cmd_prefix + ["install", "--no-build-isolation", str(src_root)]
        print(f"  $ {' '.join(cmd)}")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            return result.returncode

    print("✓ psutil installed via Android compatibility shim")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
