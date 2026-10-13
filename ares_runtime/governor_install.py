"""Opt-in native component of the existing local-runtime release descriptor.

No active selector or key authority lives here. The lifecycle owner stages a
Git-derived build, records it in release.json, and validates it before use.
"""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tomllib
from dataclasses import dataclass

from .legacy_transition import (
    LegacyTransitionError, bounded_probe, file_digest, read_identity_record,
)

NATIVE_LIFECYCLE_CONTRACT = 2
SOURCE_SCHEMA = "AresGovernorSourceV1"
RECIPE_SCHEMA = "AresManagedGovernorV1"
BINARY_PATH = ".venv/bin/context-governor"
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_FIELDS = {"schema", "remote", "revision", "crate_path", "crate_tree"}
_RECIPE_FIELDS = {
    "schema", "source", "binary", "sha256", "lock_sha256", "platform",
    "architecture", "capabilities", "build", "external_backout",
}


class GovernorInstallError(ValueError):
    """A native source, recipe, executable or backout witness was refused."""


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def _path(value: object) -> str:
    if not isinstance(value, str) or not value or value.startswith("-") or "\n" in value:
        raise GovernorInstallError("invalid native source path or remote")
    return value


def source_identity(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != _SOURCE_FIELDS:
        raise GovernorInstallError("invalid native source identity")
    if value["schema"] != SOURCE_SCHEMA or not _HEX40.fullmatch(str(value["revision"])):
        raise GovernorInstallError("invalid native source revision")
    if not _HEX40.fullmatch(str(value["crate_tree"])):
        raise GovernorInstallError("invalid native source tree")
    remote = _path(value["remote"])
    if re.search(r"https?://[^/]*@", remote):
        raise GovernorInstallError("credential-bearing native remote is not accepted")
    path = _path(value["crate_path"])
    if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
        raise GovernorInstallError("native crate path escapes the source")
    return dict(value)


@dataclass(frozen=True)
class GovernorSource:
    identity: dict
    checkout: Path | None = None

    @classmethod
    def capture(cls, path: Path) -> GovernorSource:
        path = path.expanduser().resolve(strict=True)
        crate = path / "context-governor" if (path / "context-governor/Cargo.toml").is_file() else path
        root = Path(_git(crate, "rev-parse", "--show-toplevel")).resolve()
        if _git(root, "status", "--porcelain", "--untracked-files=all"):
            raise GovernorInstallError("native source has uncommitted or untracked changes")
        relative = crate.relative_to(root).as_posix()
        revision = _git(root, "rev-parse", "HEAD")
        remote = _git(root, "remote", "get-url", "origin")
        tree_ref = revision + ("^{tree}" if relative == "." else ":" + relative)
        tree = _git(root, "rev-parse", tree_ref)
        entries = _git(root, "ls-tree", "-r", tree_ref).splitlines()
        if any(line.startswith(("120000 ", "160000 ")) for line in entries):
            raise GovernorInstallError("native source symlinks and submodules are not admitted")
        manifest_ref = revision + ":" + ("" if relative == "." else relative + "/") + "Cargo.toml"
        manifest = tomllib.loads(_git(root, "show", manifest_ref))
        if manifest.get("package", {}).get("name") != "context-governor" or "workspace" not in manifest:
            raise GovernorInstallError("source is not the standalone context-governor crate")
        identity = source_identity({"schema": SOURCE_SCHEMA, "remote": remote,
                                    "revision": revision, "crate_path": relative,
                                    "crate_tree": tree})
        return cls(identity, root)

    @classmethod
    def from_record(cls, recipe: dict) -> GovernorSource:
        return cls(source_identity(recipe["source"]))


def require_capabilities(value: object) -> dict:
    required = {
        "schema": "ContextGovernorCapabilitiesV1", "engine": "ri-context-governor",
        "receipt_schema": "ContextCompactionReceiptV2",
        "integrity": "hmac-sha256-canonical-json-v1",
        "exactness_scope": "canonical_utf8_text_v1",
    }
    flags = ("supports_recursive_lineage", "supports_certified_receipt_store",
             "supports_lineage_continuation", "supports_lineage_tip_projection",
             "supports_host_finalization_reserve", "supports_pending_activation")
    if not isinstance(value, dict) or any(value.get(k) != v for k, v in required.items()):
        raise GovernorInstallError("native capability schema mismatch")
    if any(value.get(flag) is not True for flag in flags):
        raise GovernorInstallError("native lifecycle capabilities are unavailable")
    envelope = value.get("failure_envelope")
    if envelope != {"flag": "--failure-envelope-v1", "schema": "ContextGovernorFailureV1", "stream": "stderr"}:
        raise GovernorInstallError("native failure-envelope capability mismatch")
    return dict(value)


def _executable(source: Path) -> Path:
    source = source.resolve(strict=True)
    binary = source / BINARY_PATH
    try:
        for relative in (".venv", ".venv/bin", BINARY_PATH):
            info = (source / relative).lstat()
            if stat.S_ISLNK(info.st_mode):
                raise GovernorInstallError("native executable path substitution")
        info = binary.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not info.st_mode & 0o111:
            raise GovernorInstallError("native artifact is not a unique executable file")
        if info.st_mode & 0o022:
            raise GovernorInstallError("native artifact is writable outside its owner")
        if binary.resolve(strict=True) != binary:
            raise GovernorInstallError("native artifact escapes its release")
    except OSError as exc:
        raise GovernorInstallError("native executable unavailable") from exc
    return binary


def recipe_for_source(source: Path) -> dict | None:
    descriptor = source.parent / "release.json"
    try:
        descriptor.lstat()
    except FileNotFoundError:
        if source.name == "source" and _HEX40.fullmatch(source.parent.name) and (source / BINARY_PATH).exists():
            raise GovernorInstallError("managed native descriptor is missing")
        return None
    except OSError as exc:
        raise GovernorInstallError("native descriptor is unreadable") from exc
    try:
        record, _ = read_identity_record(descriptor)
        if "governor" not in record:
            binding = record.get("runtime_binding")
            if binding is not None and not isinstance(binding, dict):
                raise GovernorInstallError("runtime binding is invalid")
            if (binding or {}).get("controller_contract") == NATIVE_LIFECYCLE_CONTRACT:
                raise GovernorInstallError("native binding has no component recipe")
            return None
        if source.name != "source" or not _HEX40.fullmatch(source.parent.name) or record.get("revision") != source.parent.name:
            raise GovernorInstallError("native recipe has a mismatched release owner")
        recipe = record["governor"]
        if not isinstance(recipe, dict) or set(recipe) != _RECIPE_FIELDS or recipe.get("schema") != RECIPE_SCHEMA:
            raise GovernorInstallError("native release recipe is invalid")
        source_identity(recipe["source"])
        if recipe["binary"] != BINARY_PATH or not _HEX64.fullmatch(str(recipe["sha256"])):
            raise GovernorInstallError("native artifact binding is invalid")
        if not _HEX64.fullmatch(str(recipe["lock_sha256"])):
            raise GovernorInstallError("native lock binding is invalid")
        if recipe["platform"] != platform.system() or recipe["architecture"] != platform.machine():
            raise GovernorInstallError("native build belongs to another host target")
        require_capabilities(recipe["capabilities"])
        build = recipe["build"]
        if not isinstance(build, dict) or set(build) != {"cargo", "rustc", "flags", "features"}:
            raise GovernorInstallError("native build recipe is invalid")
        if build["flags"] != ["build", "--release", "--locked"] or build["features"] != []:
            raise GovernorInstallError("native build recipe is unsupported")
        if not all(isinstance(build[k], str) and build[k].strip() for k in ("cargo", "rustc")):
            raise GovernorInstallError("native toolchain binding is unavailable")
        binary = _executable(source)
        if file_digest(binary) != recipe["sha256"]:
            raise GovernorInstallError("native artifact digest mismatch")
        backout = recipe["external_backout"]
        if backout is not None:
            fields = {"revision", "source", "descriptor_sha256", "binary", "sha256"}
            if not isinstance(backout, dict) or set(backout) != fields:
                raise GovernorInstallError("external native backout identity is invalid")
            if not _HEX40.fullmatch(str(backout["revision"])) or not all(
                _HEX64.fullmatch(str(backout[k])) for k in ("descriptor_sha256", "sha256")
            ):
                raise GovernorInstallError("external native backout digest is invalid")
            if not all(isinstance(backout[k], str) and Path(backout[k]).is_absolute() for k in ("source", "binary")):
                raise GovernorInstallError("external native backout path is invalid")
        return recipe
    except LegacyTransitionError as exc:
        raise GovernorInstallError("native identity read failed") from exc


def bundled_binary(source: Path, *, probe: bool = False) -> Path | None:
    recipe = recipe_for_source(source)
    if recipe is None:
        return None
    binary = _executable(source)
    if probe:
        observed = bounded_probe([str(binary), "capabilities"], cwd=source, home=source.parent)
        if require_capabilities(observed) != recipe["capabilities"]:
            raise GovernorInstallError("native capability document changed")
    return binary


def _build_governor(spec: GovernorSource, source: Path, *, external_backout: dict | None) -> dict:
    """Build only in inactive staging; never touch Cargo's installed bin root."""
    identity = source_identity(spec.identity)
    work = source.parent / "governor-build"
    work.mkdir(mode=0o700)
    if spec.checkout is not None:
        if _git(spec.checkout, "rev-parse", "HEAD") != identity["revision"] or _git(spec.checkout, "status", "--porcelain", "--untracked-files=all"):
            raise GovernorInstallError("native source changed after capture")
    checkout = work / "git"
    subprocess.run(["git", "clone", "--no-local", "--no-checkout", identity["remote"], str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "merge-base", "--is-ancestor", identity["revision"], "HEAD"], check=True)
    tree_ref = identity["revision"] + ("^{tree}" if identity["crate_path"] == "." else ":" + identity["crate_path"])
    if _git(checkout, "rev-parse", tree_ref) != identity["crate_tree"]:
        raise GovernorInstallError("native source tree mismatch")
    archive = subprocess.check_output(["git", "-C", str(checkout), "archive", "--format=tar", tree_ref])
    crate = work / "crate"
    crate.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        if any(member.issym() or member.islnk() for member in tar.getmembers()):
            raise GovernorInstallError("native archive links are not admitted")
        tar.extractall(crate, filter="data")
    environment = {k: os.environ[k] for k in ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "CARGO_HOME", "RUSTUP_HOME") if k in os.environ}
    rustup = shutil.which("rustup")
    if rustup:
        cargo = subprocess.check_output([rustup, "which", "cargo"], text=True, env=environment).strip()
        rustc = subprocess.check_output([rustup, "which", "rustc"], text=True, env=environment).strip()
    else:
        cargo, rustc = shutil.which("cargo"), shutil.which("rustc")
    if not cargo or not rustc:
        raise GovernorInstallError("native build toolchain unavailable")
    environment["PATH"] = str(Path(cargo).parent) + os.pathsep + environment.get("PATH", "")
    environment["CARGO_BUILD_JOBS"] = "2"
    toolchain = {
        "cargo": subprocess.check_output([cargo, "--version"], text=True, env=environment).strip(),
        "rustc": subprocess.check_output([rustc, "-Vv"], text=True, env=environment).strip(),
        "flags": ["build", "--release", "--locked"], "features": [],
    }
    target = work / "target"
    subprocess.run([cargo, *toolchain["flags"], "--manifest-path", str(crate / "Cargo.toml"), "--target-dir", str(target)], cwd=crate, env=environment, check=True)
    binary = source / BINARY_PATH
    for directory in (source / ".venv", source / ".venv/bin"):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise GovernorInstallError("native output directory substitution")
    if binary.exists() or binary.is_symlink():
        raise GovernorInstallError("staged native executable already exists")
    shutil.copyfile(target / "release/context-governor", binary)
    binary.chmod(0o755)
    artifact_digest = file_digest(binary)
    observed = require_capabilities(bounded_probe([str(binary), "capabilities"], cwd=source, home=source.parent))
    if file_digest(binary) != artifact_digest:
        raise GovernorInstallError("native artifact changed during capability probe")
    recipe = {"schema": RECIPE_SCHEMA, "source": identity, "binary": BINARY_PATH,
              "sha256": artifact_digest, "lock_sha256": hashlib.sha256((crate / "Cargo.lock").read_bytes()).hexdigest(),
              "platform": platform.system(), "architecture": platform.machine(),
              "capabilities": observed, "build": toolchain, "external_backout": external_backout}
    shutil.rmtree(work)
    return recipe


def build_governor(spec: GovernorSource, source: Path, *, external_backout: dict | None) -> dict:
    try:
        return _build_governor(spec, source, external_backout=external_backout)
    except (OSError, subprocess.CalledProcessError, LegacyTransitionError) as exc:
        raise GovernorInstallError("native source/build/probe failed; no admission") from exc


def external_backout(previous: tuple[str, Path], binary: Path) -> dict:
    record, raw = read_identity_record(previous[1].parent / "release.json")
    if record.get("revision") != previous[0]:
        raise GovernorInstallError("external predecessor revision mismatch")
    resolved = binary.resolve(strict=True)
    return {"revision": previous[0], "source": str(previous[1].resolve()),
            "descriptor_sha256": hashlib.sha256(raw).hexdigest(),
            "binary": str(resolved), "sha256": file_digest(resolved)}


def require_pair(first: tuple[str, Path], second: tuple[str, Path]) -> None:
    """Validate both bundle owners or the exact external predecessor witness."""
    one, two = recipe_for_source(first[1]), recipe_for_source(second[1])
    if one is None and two is None:
        return
    if one is not None and two is not None:
        return
    native, base = (one, second) if one is not None else (two, first)
    witness = native["external_backout"]
    if not witness or witness["revision"] != base[0] or witness["source"] != str(base[1].resolve()):
        raise GovernorInstallError("external native backout target is not qualified")
    _, raw = read_identity_record(base[1].parent / "release.json")
    if hashlib.sha256(raw).hexdigest() != witness["descriptor_sha256"]:
        raise GovernorInstallError("external predecessor descriptor changed")
    binary = Path(witness["binary"])
    if binary.resolve(strict=True) != binary or file_digest(binary) != witness["sha256"]:
        raise GovernorInstallError("external predecessor executable changed")
