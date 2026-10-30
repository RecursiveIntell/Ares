"""Inert specialist proposal CLI and immutable local artifacts.

This module is intentionally independent of Hermes account, model, profile
selection, plugin, and dispatch startup. It consumes only caller-named files.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import math
import os
import secrets
import stat
import sys
from pathlib import PurePosixPath
from typing import Any

from ares_runtime import specialist_routing as routing
from ares_runtime.collaboration import ContractError, canonical_json


_MAX_JSON_BYTES = 262_144
_MAX_JSON_DEPTH = 24
_MAX_ARRAY_ITEMS = 256
_MAX_STRING_BYTES = 65_536
_BUNDLE_KIND = "AresSpecialistProposalBundleV1"
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class ProposalCliError(ValueError):
    """A safe, content-free refusal suitable for a CLI diagnostic."""


class _SafeArgumentParser(argparse.ArgumentParser):
    """Argument parser whose refusals never echo caller-supplied values."""

    def error(self, _message: str) -> None:
        os.write(2, b"hermes specialists: INVALID_ARGUMENTS\n")
        raise SystemExit(2)


def _fail(code: str) -> None:
    raise ProposalCliError(code)


def _require_secure_open() -> None:
    required = ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")
    if any(not hasattr(os, flag) for flag in required) or os.open not in getattr(
        os, "supports_dir_fd", set()
    ):
        _fail("SAFE_PATH_UNSUPPORTED")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _fail("DUPLICATE_JSON_KEY")
        value[key] = item
    return value


def _constant(_value: str) -> None:
    _fail("NONFINITE_NUMBER")


def _check_tree(value: Any, depth: int = 0) -> None:
    if depth > _MAX_JSON_DEPTH:
        _fail("JSON_TOO_DEEP")
    if isinstance(value, float) and not math.isfinite(value):
        _fail("NONFINITE_NUMBER")
    if isinstance(value, str):
        try:
            size = len(value.encode("utf-8", "strict"))
        except UnicodeEncodeError:
            _fail("INVALID_UTF8")
        if size > _MAX_STRING_BYTES:
            _fail("STRING_TOO_LARGE")
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _fail("INVALID_OBJECT_KEY")
            _check_tree(key, depth + 1)
            _check_tree(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > _MAX_ARRAY_ITEMS:
            _fail("LIST_TOO_LARGE")
        for item in value:
            _check_tree(item, depth + 1)


def _decode_json(raw: bytes) -> Any:
    if len(raw) > _MAX_JSON_BYTES:
        _fail("ARTIFACT_TOO_LARGE")
    try:
        text = raw.decode("utf-8", "strict")
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except ProposalCliError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        _fail("INVALID_JSON")
    _check_tree(value)
    return value


def _open_dir_path(path: str | os.PathLike[str]) -> int:
    _require_secure_open()
    value = os.fspath(path)
    if (
        not isinstance(value, str)
        or len(value.encode("utf-8", "surrogatepass")) > 4096
        or "\x00" in value
    ):
        _fail("INVALID_PATH")
    absolute = value.startswith(os.sep)
    components = value.split(os.sep)
    if absolute:
        components = components[1:]
        current = os.open(os.sep, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    else:
        current = os.open(".", os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    try:
        for component in components:
            if component in ("", "."):
                continue
            if (
                component == ".."
                or len(component.encode("utf-8", "surrogatepass")) > 255
            ):
                _fail("UNSAFE_PATH")
            next_fd = os.open(
                component,
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                dir_fd=current,
            )
            os.close(current)
            current = next_fd
        return current
    except Exception:
        os.close(current)
        raise


def _split_file_path(path: str | os.PathLike[str]) -> tuple[str, str]:
    value = os.fspath(path)
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8", "surrogatepass")) > 4096
        or "\x00" in value
    ):
        _fail("INVALID_PATH")
    value = value.rstrip(os.sep) or os.sep
    parent, name = os.path.split(value)
    if (
        not name
        or name in (".", "..")
        or len(name.encode("utf-8", "surrogatepass")) > 255
    ):
        _fail("INVALID_PATH")
    return parent or (os.sep if value.startswith(os.sep) else "."), name


def _read_at(directory_fd: int, name: str, *, require_private: bool = False) -> bytes:
    fd = os.open(
        name,
        os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC | getattr(os, "O_NONBLOCK", 0),
        dir_fd=directory_fd,
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _fail("NOT_REGULAR_FILE")
        if require_private and stat.S_IMODE(info.st_mode) & 0o077:
            _fail("OUTPUT_NOT_PRIVATE")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(fd, min(65_536, _MAX_JSON_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > _MAX_JSON_BYTES:
                _fail("ARTIFACT_TOO_LARGE")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _read_file(path: str | os.PathLike[str]) -> bytes:
    parent, name = _split_file_path(path)
    parent_fd = _open_dir_path(parent)
    try:
        return _read_at(parent_fd, name)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            _fail("SYMLINK_REFUSED")
        raise
    finally:
        os.close(parent_fd)


def _existing_regular_bytes(
    dir_fd: int, name: str, *, require_private: bool = False
) -> bytes | None:
    try:
        return _read_at(dir_fd, name, require_private=require_private)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            _fail("SYMLINK_REFUSED")
        raise


def _sync_existing_file(dir_fd: int, name: str) -> None:
    fd = os.open(
        name,
        os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC | os.O_NONBLOCK,
        dir_fd=dir_fd,
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _fail("NOT_REGULAR_FILE")
        if stat.S_IMODE(info.st_mode) & 0o077:
            _fail("OUTPUT_NOT_PRIVATE")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(dir_fd)


def _publish_file(dir_fd: int, name: str, content: bytes) -> bool:
    """Publish immutable bytes with exclusive atomic link; return if new."""
    current = _existing_regular_bytes(dir_fd, name)
    if current is not None:
        if current == content:
            _sync_existing_file(dir_fd, name)
            return False
        _fail("IMMUTABLE_CONFLICT")

    temp = ".p04-" + secrets.token_hex(12) + ".tmp"
    temp_created = False
    try:
        fd = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
            0o600,
            dir_fd=dir_fd,
        )
        temp_created = True
        try:
            view = memoryview(content)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    _fail("SHORT_WRITE")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

        try:
            os.link(
                temp,
                name,
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
                follow_symlinks=False,
            )
            os.fsync(dir_fd)
            return True
        except FileExistsError:
            current = _existing_regular_bytes(dir_fd, name)
            if current == content:
                _sync_existing_file(dir_fd, name)
                return False
            _fail("IMMUTABLE_CONFLICT")
    finally:
        if temp_created:
            try:
                os.unlink(temp, dir_fd=dir_fd)
                os.fsync(dir_fd)
            except FileNotFoundError:
                pass


def _publish_explicit_file(path: str, content: bytes) -> bool:
    parent, name = _split_file_path(path)
    parent_fd = _open_dir_path(parent)
    try:
        return _publish_file(parent_fd, name, content)
    except OSError as exc:
        if exc.errno == 40:
            _fail("SYMLINK_REFUSED")
        raise
    finally:
        os.close(parent_fd)


def _open_private_bundle(path: str) -> int:
    parent, name = _split_file_path(path)
    parent_fd = _open_dir_path(parent)
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileExistsError:
            pass
        bundle_fd = os.open(
            name,
            os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        if exc.errno == 40:
            _fail("SYMLINK_REFUSED")
        raise
    finally:
        os.close(parent_fd)
    if stat.S_IMODE(os.fstat(bundle_fd).st_mode) & 0o077:
        os.close(bundle_fd)
        _fail("OUTPUT_NOT_PRIVATE")
    return bundle_fd


def _open_existing_bundle(path: str) -> int:
    bundle_fd = _open_dir_path(path)
    if stat.S_IMODE(os.fstat(bundle_fd).st_mode) & 0o077:
        os.close(bundle_fd)
        _fail("OUTPUT_NOT_PRIVATE")
    return bundle_fd


def _open_objects_dir(bundle_fd: int) -> int:
    try:
        os.mkdir("objects", 0o700, dir_fd=bundle_fd)
        os.fsync(bundle_fd)
    except FileExistsError:
        pass
    fd = os.open(
        "objects",
        os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
        dir_fd=bundle_fd,
    )
    if stat.S_IMODE(os.fstat(fd).st_mode) & 0o077:
        os.close(fd)
        _fail("OUTPUT_NOT_PRIVATE")
    return fd


def _parse_scope(raw: bytes) -> dict[str, Any]:
    value = _decode_json(raw)
    if not isinstance(value, dict):
        _fail("INVALID_SCOPE")
    return value


def _run_capture(scope_path: str, out_path: str) -> None:
    scope = _parse_scope(_read_file(scope_path))
    artifact = routing.capture_coverage_snapshot(scope)
    _publish_explicit_file(out_path, canonical_json(artifact.to_dict()))


def _assessment(raw: bytes) -> dict[str, Any]:
    value = _decode_json(raw)
    if not isinstance(value, dict):
        _fail("INVALID_ASSESSMENT")
    return value


def _load_detection_inputs(paths: dict[str, str]):
    raw = {key: _read_file(path) for key, path in paths.items()}
    need = routing.SpecialistNeedV1.parse_json(raw["need"])
    coverage = routing.SpecialistCoverageSnapshotV1.parse_json(raw["coverage"])
    assessment = _assessment(raw["assessment"])
    proposal = routing.compile_specialist_proposal(
        need=need, coverage=coverage, assessment=assessment
    )
    proposal_raw = canonical_json(proposal.to_dict())
    return raw, proposal, proposal_raw


def _artifact_row(role: str, raw: bytes) -> dict[str, Any]:
    content_hash = hashlib.sha256(raw).hexdigest()
    return {
        "role": role,
        "path": f"objects/{content_hash}.json",
        "byte_digest": "sha256:" + content_hash,
        "byte_length": len(raw),
    }


def _bundle_manifest(contents: dict[str, bytes]) -> tuple[dict[str, Any], bytes]:
    rows = [_artifact_row(role, contents[role]) for role in sorted(contents)]
    body: dict[str, Any] = {
        "bundle_kind": _BUNDLE_KIND,
        "schema_version": "1.0.0",
        "artifacts": rows,
    }
    manifest = dict(body)
    manifest["manifest_digest"] = (
        "sha256:" + hashlib.sha256(canonical_json(body)).hexdigest()
    )
    return manifest, canonical_json(manifest)


def _run_detect(paths: dict[str, str], out_path: str) -> None:
    raw, proposal, proposal_raw = _load_detection_inputs(paths)
    contents = {
        "assessment": raw["assessment"],
        "coverage": raw["coverage"],
        "need": raw["need"],
        "proposal": proposal_raw,
    }
    manifest, manifest_raw = _bundle_manifest(contents)
    bundle_fd = _open_private_bundle(out_path)
    try:
        # An exact complete replay is a read-only success. A conflicting
        # terminal index refuses before creating or changing any child.
        existing_manifest = _existing_regular_bytes(
            bundle_fd, "manifest.json", require_private=True
        )
        if existing_manifest is not None:
            if existing_manifest != manifest_raw:
                _fail("IMMUTABLE_CONFLICT")
            _validate_bundle(bundle_fd, existing_manifest)
            # A prior process may have linked this exact terminal index but
            # exited before its directory fsync completed. Re-establish that
            # boundary on every successful exact retry.
            _sync_existing_file(bundle_fd, "manifest.json")
            return

        objects_fd = _open_objects_dir(bundle_fd)
        try:
            for role in sorted(contents):
                row = next(
                    item for item in manifest["artifacts"] if item["role"] == role
                )
                _publish_file(
                    objects_fd, PurePosixPath(row["path"]).name, contents[role]
                )
        finally:
            os.close(objects_fd)
        # This terminal manifest is published only after every immutable child
        # file and its directory entry have been fsynced.
        _publish_file(bundle_fd, "manifest.json", manifest_raw)
    finally:
        os.close(bundle_fd)


def _manifest_body(manifest: dict[str, Any]) -> dict[str, Any]:
    if set(manifest) != {
        "bundle_kind",
        "schema_version",
        "artifacts",
        "manifest_digest",
    }:
        _fail("INVALID_MANIFEST")
    supplied = manifest["manifest_digest"]
    body = {key: value for key, value in manifest.items() if key != "manifest_digest"}
    expected = "sha256:" + hashlib.sha256(canonical_json(body)).hexdigest()
    if (
        supplied != expected
        or manifest["bundle_kind"] != _BUNDLE_KIND
        or manifest["schema_version"] != "1.0.0"
    ):
        _fail("INVALID_MANIFEST")
    return body


def _validate_bundle(bundle_fd: int, manifest_raw: bytes) -> bytes:
    manifest = _decode_json(manifest_raw)
    if not isinstance(manifest, dict) or canonical_json(manifest) != manifest_raw:
        _fail("INVALID_MANIFEST")
    _manifest_body(manifest)
    rows = manifest["artifacts"]
    if not isinstance(rows, list) or len(rows) != 4:
        _fail("INVALID_MANIFEST")
    objects_fd = os.open(
        "objects",
        os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
        dir_fd=bundle_fd,
    )
    try:
        if stat.S_IMODE(os.fstat(objects_fd).st_mode) & 0o077:
            _fail("OUTPUT_NOT_PRIVATE")
        contents: dict[str, bytes] = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != {
                "role",
                "path",
                "byte_digest",
                "byte_length",
            }:
                _fail("INVALID_MANIFEST")
            role, ref = row["role"], row["path"]
            if (
                role not in {"assessment", "coverage", "need", "proposal"}
                or role in contents
            ):
                _fail("INVALID_MANIFEST")
            if (
                not isinstance(ref, str)
                or len(ref) != 77
                or not ref.startswith("objects/")
                or not ref.endswith(".json")
            ):
                _fail("INVALID_REFERENCE")
            name = ref[len("objects/") :]
            hexdigest = name[:-5]
            if len(hexdigest) != 64 or any(
                char not in "0123456789abcdef" for char in hexdigest
            ):
                _fail("INVALID_REFERENCE")
            raw = _read_at(objects_fd, name, require_private=True)
            actual_hash = hashlib.sha256(raw).hexdigest()
            if (
                hexdigest != actual_hash
                or row["byte_digest"] != "sha256:" + actual_hash
                or type(row["byte_length"]) is not int
                or row["byte_length"] != len(raw)
            ):
                _fail("ARTIFACT_DIGEST_MISMATCH")
            contents[role] = raw
    finally:
        os.close(objects_fd)
    if set(contents) != {"assessment", "coverage", "need", "proposal"}:
        _fail("INVALID_MANIFEST")
    if [row["role"] for row in rows] != sorted(contents):
        _fail("NONCANONICAL_MANIFEST")
    need = routing.SpecialistNeedV1.parse_json(contents["need"])
    coverage = routing.SpecialistCoverageSnapshotV1.parse_json(contents["coverage"])
    assessment = _assessment(contents["assessment"])
    proposal = routing.SpecialistProposalV1.parse_json(contents["proposal"])
    rebuilt = routing.compile_specialist_proposal(
        need=need, coverage=coverage, assessment=assessment
    )
    canonical_proposal = canonical_json(proposal.to_dict())
    if (
        canonical_proposal != contents["proposal"]
        or canonical_json(rebuilt.to_dict()) != canonical_proposal
    ):
        _fail("PROPOSAL_REPLAY_MISMATCH")
    return canonical_proposal


def _bundle_dir_and_manifest(path: str) -> tuple[int, bytes]:
    if os.path.basename(path.rstrip(os.sep)) == "manifest.json":
        parent, _name = _split_file_path(path)
        bundle_fd = _open_existing_bundle(parent)
    else:
        bundle_fd = _open_existing_bundle(path)
    try:
        manifest_raw = _read_at(bundle_fd, "manifest.json", require_private=True)
        return bundle_fd, manifest_raw
    except Exception:
        os.close(bundle_fd)
        raise


def _run_replay(path: str) -> bytes:
    bundle_fd, manifest_raw = _bundle_dir_and_manifest(path)
    try:
        return _validate_bundle(bundle_fd, manifest_raw)
    finally:
        os.close(bundle_fd)


def _run_show(path: str) -> bytes:
    proposal = routing.SpecialistProposalV1.parse_json(_read_file(path))
    return canonical_json(proposal.to_dict())


def _build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="hermes specialists",
        description="Capture and inspect inert, non-dispatch specialist proposals.",
    )
    commands = parser.add_subparsers(
        dest="specialist_command",
        required=True,
        parser_class=_SafeArgumentParser,
    )
    capture = commands.add_parser(
        "capture-coverage", help="Capture an explicit coverage scope"
    )
    capture.add_argument("--scope", required=True, metavar="MANIFEST")
    capture.add_argument("--out", required=True, metavar="SNAPSHOT")
    detect = commands.add_parser("detect", help="Compile an inert proposal bundle")
    detect.add_argument("--need", required=True, metavar="ARTIFACT")
    detect.add_argument("--coverage", required=True, metavar="SNAPSHOT")
    detect.add_argument("--assessment", required=True, metavar="ARTIFACT")
    detect.add_argument("--out", required=True, metavar="PRIVATE_DIRECTORY")
    show = commands.add_parser("show", help="Display one validated proposal")
    show.add_argument("--proposal", required=True, metavar="ARTIFACT")
    replay = commands.add_parser(
        "replay", help="Validate and deterministically replay a bundle"
    )
    replay.add_argument("--bundle", required=True, metavar="DIRECTORY_OR_MANIFEST")
    return parser


def _execute(args: argparse.Namespace) -> int:
    if args.specialist_command == "capture-coverage":
        _run_capture(args.scope, args.out)
    elif args.specialist_command == "detect":
        _run_detect(
            {
                "need": args.need,
                "coverage": args.coverage,
                "assessment": args.assessment,
            },
            args.out,
        )
    elif args.specialist_command == "show":
        sys.stdout.buffer.write(_run_show(args.proposal))
    elif args.specialist_command == "replay":
        sys.stdout.buffer.write(_run_replay(args.bundle))
    else:
        _fail("UNKNOWN_COMMAND")
    return 0


def run_standalone(argv: list[str]) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        return _execute(args)
    except (ProposalCliError, ContractError, routing.CoverageCaptureError) as exc:
        code = getattr(exc, "code", "INVALID_INPUT")
        print(f"hermes specialists: {code}", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError) as exc:
        # Never expose absolute paths or input contents through OS/parser errors.
        code = (
            "SYMLINK_REFUSED"
            if getattr(exc, "errno", None) == errno.ELOOP
            else "OPERATION_REFUSED"
        )
        print(f"hermes specialists: {code}", file=sys.stderr)
        return 2


def register_parser(subparsers) -> argparse.ArgumentParser:
    """Register a parser for ordinary parser/help compatibility."""
    parser = subparsers.add_parser(
        "specialists",
        help="Capture and inspect inert specialist proposals",
        description="Capture and inspect inert, non-dispatch specialist proposals.",
    )
    command_parsers = parser.add_subparsers(dest="specialist_command", required=True)
    capture = command_parsers.add_parser(
        "capture-coverage", help="Capture an explicit coverage scope"
    )
    capture.add_argument("--scope", required=True, metavar="MANIFEST")
    capture.add_argument("--out", required=True, metavar="SNAPSHOT")
    detect = command_parsers.add_parser(
        "detect", help="Compile an inert proposal bundle"
    )
    detect.add_argument("--need", required=True, metavar="ARTIFACT")
    detect.add_argument("--coverage", required=True, metavar="SNAPSHOT")
    detect.add_argument("--assessment", required=True, metavar="ARTIFACT")
    detect.add_argument("--out", required=True, metavar="PRIVATE_DIRECTORY")
    show = command_parsers.add_parser("show", help="Display one validated proposal")
    show.add_argument("--proposal", required=True, metavar="ARTIFACT")
    replay = command_parsers.add_parser(
        "replay", help="Validate and deterministically replay a bundle"
    )
    replay.add_argument("--bundle", required=True, metavar="DIRECTORY_OR_MANIFEST")
    parser.set_defaults(func=_execute)
    return parser
