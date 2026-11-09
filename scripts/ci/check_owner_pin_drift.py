#!/usr/bin/env python3
"""Fail when a pinned owner checkout is stale for the crates Ares consumes.

``current-owner-integration`` qualifies Ares against an exact pinned commit of
the canonical Rust owner workspace (``RecursiveIntell/Libraries``). A pin is
only evidence about the owner while it matches the owner's current source for
everything the qualification actually builds. A pin that lags the owner's
``main`` on those paths gives a green result for an engine that is no longer the
current one (stale-green by construction).

This check fetches the owner's ``main`` tip and compares it with the pinned
``HEAD`` of the checkout, restricted to:

* the crate directories the workflow builds, and
* every crate directory reachable from them through ``path`` dependencies
  (``dependencies``, ``dev-dependencies``, ``build-dependencies``, their
  target-specific forms, and workspace-inherited path dependencies), and
* the workspace-level files that change resolution (``Cargo.toml``,
  ``Cargo.lock``, ``rust-toolchain*``),

ignoring Markdown files. The workflow builds an example and runs one named
integration test; it runs no doctests, so documentation-only changes cannot
change its result. Revisit this exclusion if the workflow ever runs
``cargo test --doc`` or ``--all-targets`` on a crate that includes Markdown via
``include_str!``.

Any difference fails the check and lists the files, so the pin is bumped
deliberately and requalified rather than drifting. The dependency closure is a
deliberate superset (optional and dev dependencies included): over-reporting a
change only asks for a bump, while under-reporting would hide drift.

Network or git failures fail closed (exit 2); a drift finding exits 1.
Standard library only; requires Python 3.11+ for ``tomllib``.
"""
from __future__ import annotations

import argparse
import posixpath
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT_FILES = ("Cargo.toml", "Cargo.lock", "rust-toolchain", "rust-toolchain.toml")
DEP_TABLES = ("dependencies", "dev-dependencies", "build-dependencies")
MAX_LISTED = 50
# Documentation-only changes cannot affect the qualification (see module docstring).
EXCLUDE_DOCS = ":(exclude,glob)**/*.md"


class CheckError(Exception):
    """An operational failure (not a drift finding)."""


def _git(root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise CheckError(f"cannot run git: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise CheckError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


def _manifest(root: Path, rel_dir: str) -> dict | None:
    path = root / rel_dir / "Cargo.toml" if rel_dir != "." else root / "Cargo.toml"
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CheckError(f"cannot parse {path}: {exc}") from exc


def _dependency_tables(manifest: dict):
    for name in DEP_TABLES:
        table = manifest.get(name)
        if isinstance(table, dict):
            yield table
    targets = manifest.get("target")
    if isinstance(targets, dict):
        for target in targets.values():
            if not isinstance(target, dict):
                continue
            for name in DEP_TABLES:
                table = target.get(name)
                if isinstance(table, dict):
                    yield table


def _normalise(rel_dir: str, path: str) -> str:
    joined = posixpath.normpath(posixpath.join(rel_dir, path))
    if joined == ".." or joined.startswith("../") or posixpath.isabs(joined):
        raise CheckError(
            f"path dependency {path!r} from {rel_dir!r} leaves the owner checkout; "
            "drift cannot be assessed"
        )
    return joined


def dependency_closure(root: Path, crate_dirs: list[str]) -> list[str]:
    """Crate directories reachable from ``crate_dirs`` via path dependencies."""
    root_manifest = _manifest(root, ".") or {}
    workspace_deps = root_manifest.get("workspace", {}).get("dependencies", {})
    if not isinstance(workspace_deps, dict):
        workspace_deps = {}

    seen: set[str] = set()
    queue = [posixpath.normpath(d) for d in crate_dirs]
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        if not (root / current).is_dir():
            raise CheckError(f"crate directory {current!r} does not exist in the owner checkout")
        seen.add(current)
        manifest = _manifest(root, current)
        if manifest is None:
            continue
        for table in _dependency_tables(manifest):
            for name, spec in table.items():
                if not isinstance(spec, dict):
                    continue
                if "path" in spec:
                    queue.append(_normalise(current, str(spec["path"])))
                elif spec.get("workspace") is True:
                    inherited = workspace_deps.get(name)
                    if isinstance(inherited, dict) and "path" in inherited:
                        queue.append(_normalise(".", str(inherited["path"])))
    return sorted(seen)


def fetch_ref(root: Path, remote: str, ref: str, fetch_filter: str) -> str:
    args = ["fetch", "--no-tags", "--depth=1"]
    if fetch_filter:
        args.append(f"--filter={fetch_filter}")
    args += [remote, ref]
    _git(root, *args)
    return _git(root, "rev-parse", "FETCH_HEAD^{commit}").strip()


def changed_paths(root: Path, pinned: str, current: str, pathspec: list[str]) -> list[str]:
    out = _git(root, "diff", "--name-only", "--no-renames", "-z", pinned, current, "--", *pathspec)
    return sorted(p for p in out.split("\0") if p)


def run(args: argparse.Namespace) -> int:
    root = Path(args.libraries).resolve()
    if not (root / ".git").exists():
        raise CheckError(f"{root} is not a git checkout")
    pinned = _git(root, "rev-parse", "HEAD").strip()
    current = fetch_ref(root, args.remote, args.ref, args.fetch_filter)
    closure = dependency_closure(root, args.crate_dir)
    pathspec = closure + list(ROOT_FILES) + [EXCLUDE_DOCS]
    print(f"owner pin:   {pinned}")
    print(f"owner {args.ref}: {current}")
    print(f"tracked crate directories ({len(closure)}): {', '.join(closure)}")
    if pinned == current:
        print("OK: the pin is the owner's current tip.")
        return 0
    changed = changed_paths(root, pinned, current, pathspec)
    if not changed:
        print("OK: the pin differs from the owner tip, but no consumed path changed.")
        return 0
    print(
        f"FAIL: the pinned owner is stale for {len(changed)} consumed path(s) "
        f"relative to {args.remote}/{args.ref}.",
        file=sys.stderr,
    )
    for path in changed[:MAX_LISTED]:
        print(f"  changed since pin: {path}", file=sys.stderr)
    if len(changed) > MAX_LISTED:
        print(f"  ... and {len(changed) - MAX_LISTED} more", file=sys.stderr)
    print(
        f"Bump the owner pin to {current}, requalify against it, and rerun. "
        "Do not reuse a result produced against the older pin.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--libraries", required=True, help="path to the pinned owner checkout")
    parser.add_argument(
        "--crate-dir",
        action="append",
        required=True,
        help="crate directory (relative to the owner root) that the workflow builds; repeatable",
    )
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--ref", default="main")
    parser.add_argument(
        "--fetch-filter",
        default="blob:none",
        help="git partial-clone filter for the tip fetch; empty disables it",
    )
    args = parser.parse_args(argv)
    try:
        return run(args)
    except CheckError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
