"""Read-only discovery of unpacked desktop builds from electron-builder identity.

The desktop package manifest owns current names. Hermes layouts are an explicit
compatibility layout, allowed only when the manifest identifies Hermes. This
module uses only the standard library so the POSIX handoff can invoke it directly.
Discovery is not a signature, architecture, or executable-integrity check.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


def _component(value: object) -> str:
    # Do not let manifest strings become paths, globs, shell syntax, or controls.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]*", value):
        raise ValueError("invalid desktop package identity")
    if value.endswith((" ", ".")):
        raise ValueError("invalid desktop package identity")
    return value


def _identity(desktop_dir: Path) -> tuple[str, str]:
    package = json.loads((desktop_dir / "package.json").read_text(encoding="utf-8"))
    build = package.get("build", {})
    # The package must positively identify itself; missing/malformed identity
    # must never authorize a stale package of a different product.
    product = build.get("productName", package.get("productName", package.get("name")))
    return _component(product), _component(build.get("executableName", product))


def _layouts(platform: str, product: str, executable: str) -> list[Path]:
    if platform == "darwin":
        return [Path(folder) / f"{product}.app" / "Contents" / "MacOS" / executable
                for folder in ("mac-arm64", "mac", "mac-universal")]
    if platform == "win32":
        return [Path(folder) / f"{executable}.exe" for folder in
                ("win-unpacked", "win-ia32-unpacked", "win-arm64-unpacked")]
    if platform == "linux":
        return [Path(folder) / executable for folder in
                ("linux-unpacked", "linux-arm64-unpacked", "linux-armv7l-unpacked")]
    return []


def packaged_executables(desktop_dir: Path, platform: str) -> list[Path]:
    """Return safe existing current-name files, or explicit legacy fallbacks.

    ``platform`` is layout data, not a claim that a foreign binary can run here.
    Symlinks outside the release tree (including the release root) are rejected.
    """
    try:
        product, executable = _identity(desktop_dir)
        desktop = desktop_dir.resolve(strict=True)
        release = desktop_dir / "release"
        release_root = release.resolve(strict=True)
        if release_root != desktop / "release":
            return []

        def existing(paths: list[Path]) -> list[Path]:
            result = []
            for relative in paths:
                candidate = release / relative
                try:
                    resolved = candidate.resolve(strict=True)
                    resolved.relative_to(release_root)
                    if candidate.is_file() and resolved == release_root / relative:
                        result.append(candidate)
                except (OSError, ValueError, RuntimeError):
                    continue
            return result

        current = existing(_layouts(platform, product, executable))
        if current:
            return current
        # Current identity is authoritative even if its output is missing.
        # Lowercase Linux Hermes binaries are supported only for a positively
        # identified legacy manifest, never as a substitute for an Ares build.
        if (product, executable) not in (("Hermes", "Hermes"), ("hermes", "hermes")):
            return []
        legacy = _layouts(platform, "Hermes", "Hermes")
        if platform == "linux":
            legacy += _layouts(platform, "Hermes", "hermes")
        fallback = existing(legacy)
        if fallback:
            logger.warning("Using legacy Hermes desktop package layout in %s", release)
        return fallback
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        return []


def rebuilt_mac_bundle(desktop_dir: Path) -> Path | None:
    """Find the rebuilt source, never an installed relaunch destination."""
    candidates = packaged_executables(desktop_dir, "darwin")
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime).parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("desktop_dir", type=Path)
    args = parser.parse_args()
    bundle = rebuilt_mac_bundle(args.desktop_dir)
    if bundle is None:
        return 1
    print(bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
