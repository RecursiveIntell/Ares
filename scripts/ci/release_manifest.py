#!/usr/bin/env python3
"""Validate and resolve an immutable Ares source bundle (Python 3.14+)."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SCHEMA = "ares-release-manifest/v1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REF_RE = re.compile(r"^refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,240}$")
REPOSITORIES = {
    "Libraries": "https://github.com/RecursiveIntell/Libraries.git",
    "agent-memory-kits": "https://github.com/RecursiveIntell/agent-memory-kits.git",
    "agent-graph-mcp": "https://github.com/RecursiveIntell/agent-graph-mcp.git",
    "recursive-agent": "https://github.com/RecursiveIntell/recursive-agent.git",
}
SUBMODULES = {
    "semantic-memory-mcp": {
        "parent": "Libraries",
        "path": "semantic-memory-mcp",
        "url": "https://github.com/RecursiveIntell/semantic-memory-mcp.git",
    }
}
TOOLCHAINS = {"python_runtime": "3.14", "manifest_python": "3.14", "rust": "1.99.1"}
DEFAULT_MANIFEST = Path(__file__).with_name("release-manifest.json")


class ManifestError(ValueError):
    """Typed failure for an invalid or incomplete source bundle."""


def _exact_keys(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ManifestError(f"{label} keys must be exactly {sorted(expected)}")
    return value


def load_manifest(path: Path = DEFAULT_MANIFEST) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        document = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError(f"cannot read release manifest: {error}") from error
    root = _exact_keys(
        document,
        {"schema", "repositories", "submodules", "toolchains"},
        "manifest",
    )
    if root["schema"] != SCHEMA:
        raise ManifestError(f"unsupported schema: {root['schema']!r}")

    repositories = root["repositories"]
    if not isinstance(repositories, dict) or set(repositories) != set(REPOSITORIES):
        raise ManifestError(f"repositories must be exactly {sorted(REPOSITORIES)}")
    for name, expected_url in REPOSITORIES.items():
        item = _exact_keys(repositories[name], {"url", "ref", "commit"}, f"repository {name}")
        if item["url"] != expected_url:
            raise ManifestError(f"repository {name} origin is not canonical")
        if not isinstance(item["ref"], str) or not REF_RE.fullmatch(item["ref"]):
            raise ManifestError(f"repository {name} ref is invalid")
        if ".." in item["ref"] or "@" in item["ref"]:
            raise ManifestError(f"repository {name} ref is unsafe")
        if not isinstance(item["commit"], str) or not SHA_RE.fullmatch(item["commit"]):
            raise ManifestError(f"repository {name} commit must be a full lowercase Git SHA")

    submodules = root["submodules"]
    if not isinstance(submodules, dict) or set(submodules) != set(SUBMODULES):
        raise ManifestError(f"submodules must be exactly {sorted(SUBMODULES)}")
    for name, expected in SUBMODULES.items():
        item = _exact_keys(submodules[name], {"parent", "path", "url", "commit"}, f"submodule {name}")
        for field, value in expected.items():
            if item[field] != value:
                raise ManifestError(f"submodule {name} {field} is not canonical")
        if not isinstance(item["commit"], str) or not SHA_RE.fullmatch(item["commit"]):
            raise ManifestError(f"submodule {name} commit must be a full lowercase Git SHA")

    if _exact_keys(root["toolchains"], set(TOOLCHAINS), "toolchains") != TOOLCHAINS:
        raise ManifestError(f"toolchains must match the qualified recipe {TOOLCHAINS}")
    return root, raw


def _run_git(directory: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(directory), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode:
        raise ManifestError(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result


def checkout_exact(url: str, ref: str, commit: str, destination: Path) -> str:
    """Fetch a manifest-approved source and leave it detached at its exact commit."""
    if not REF_RE.fullmatch(ref) or not SHA_RE.fullmatch(commit):
        raise ManifestError("checkout ref or commit is not canonical")
    destination = destination.expanduser().resolve()
    if destination.exists():
        top = _run_git(destination, "rev-parse", "--show-toplevel").stdout.strip()
        if Path(top).resolve() != destination:
            raise ManifestError(f"existing source path is not a Git root: {destination}")
        origin = _run_git(destination, "remote", "get-url", "origin").stdout.strip()
        if origin != url:
            raise ManifestError(f"unexpected origin at {destination}")
        if _run_git(destination, "status", "--porcelain=v1", "--untracked-files=all").stdout:
            raise ManifestError(f"refusing dirty source checkout: {destination}")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "clone", "--filter=blob:none", "--no-checkout", url, str(destination)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise ManifestError(f"git clone failed for {url}: {result.stderr.strip()}")

    _run_git(destination, "fetch", "origin", ref)
    _run_git(destination, "cat-file", "-e", f"{commit}^{{commit}}")
    if _run_git(destination, "merge-base", "--is-ancestor", commit, "FETCH_HEAD", check=False).returncode:
        raise ManifestError(f"pinned commit is not reachable from {ref}")
    _run_git(destination, "checkout", "--detach", commit)
    actual = _run_git(destination, "rev-parse", "HEAD").stdout.strip()
    if actual != commit:
        raise ManifestError("checkout did not land on the pinned commit")
    if _run_git(destination, "status", "--porcelain=v1", "--untracked-files=all").stdout:
        raise ManifestError("pinned checkout is not clean after checkout")
    return actual


def verify_submodule(parent: Path, name: str, path: str, url: str, commit: str) -> str:
    """Initialize only the declared submodule and prove its OID equals the manifest pin."""
    parent = parent.resolve(strict=True)
    if name not in SUBMODULES or path != SUBMODULES[name]["path"] or url != SUBMODULES[name]["url"]:
        raise ManifestError("submodule declaration is not canonical")
    if not SHA_RE.fullmatch(commit):
        raise ManifestError("submodule commit must be a full lowercase Git SHA")
    tree = _run_git(parent, "ls-tree", "HEAD", path).stdout.strip()
    fields = tree.split("\t", 1)[0].split()
    if len(fields) != 3 or fields[0] != "160000" or fields[1] != "commit" or fields[2] != commit:
        raise ManifestError("manifest submodule commit does not match the parent gitlink")
    _run_git(parent, "submodule", "update", "--init", "--recursive", "--", path)
    source = parent / path
    origin = _run_git(source, "remote", "get-url", "origin").stdout.strip()
    actual = _run_git(source, "rev-parse", "HEAD").stdout.strip()
    if origin != url or actual != commit:
        raise ManifestError("initialized submodule identity differs from manifest")
    if _run_git(source, "status", "--porcelain=v1", "--untracked-files=all").stdout:
        raise ManifestError("initialized submodule is not clean")
    return actual


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="validate manifest and print its SHA-256")
    repo_parser = commands.add_parser("repo", help="print a pinned repository field")
    repo_parser.add_argument("name", choices=sorted(REPOSITORIES))
    repo_parser.add_argument("field", choices=("url", "ref", "commit"))
    sub_parser = commands.add_parser("submodule", help="print a pinned submodule field")
    sub_parser.add_argument("name", choices=sorted(SUBMODULES))
    sub_parser.add_argument("field", choices=("parent", "path", "url", "commit"))
    toolchain_parser = commands.add_parser("toolchain", help="print a pinned toolchain requirement")
    toolchain_parser.add_argument("name", choices=sorted(TOOLCHAINS))
    checkout_parser = commands.add_parser("checkout", help="checkout an exact repository pin")
    checkout_parser.add_argument("name", choices=sorted(REPOSITORIES))
    checkout_parser.add_argument("destination", type=Path)
    sub_check_parser = commands.add_parser("verify-submodule", help="initialize and verify a declared submodule")
    sub_check_parser.add_argument("parent", type=Path)
    sub_check_parser.add_argument("name", choices=sorted(SUBMODULES))

    args = parser.parse_args(argv)
    try:
        manifest, raw = load_manifest(args.manifest)
        if args.command == "validate":
            print(hashlib.sha256(raw).hexdigest())
        elif args.command == "repo":
            print(manifest["repositories"][args.name][args.field])
        elif args.command == "submodule":
            print(manifest["submodules"][args.name][args.field])
        elif args.command == "toolchain":
            print(manifest["toolchains"][args.name])
        elif args.command == "checkout":
            item = manifest["repositories"][args.name]
            print(checkout_exact(item["url"], item["ref"], item["commit"], args.destination))
        else:
            item = manifest["submodules"][args.name]
            print(verify_submodule(args.parent, args.name, item["path"], item["url"], item["commit"]))
    except ManifestError as error:
        print(f"release manifest invalid: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
