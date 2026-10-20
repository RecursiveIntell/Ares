"""Closed, read-only import evidence for an explicit local-controller transition.

These witnesses correlate immutable release identity; they are not signatures,
same-UID isolation, or a substitute for the controller's selected-pointer lock.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import time

LEGACY_BINDING_SCHEMA = "AresLegacyRollbackBindingV1"
LEGACY_BINDING_FIELDS = frozenset({
    "schema", "revision", "source", "git_tree", "descriptor_sha256",
    "python_sha256", "venv_config_sha256", "controller_contract",
    "venv_prefix", "controller_file", "cli_file",
})


class LegacyTransitionError(ValueError):
    """The selected release could not supply the required identity evidence."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LegacyTransitionError("duplicate identity field")
        result[key] = value
    return result


def _reject_constant(_value):
    raise LegacyTransitionError("non-finite identity field")


def decode_identity(data: bytes) -> dict:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeError, ValueError) as exc:
        raise LegacyTransitionError("invalid release identity record") from exc
    if not isinstance(value, dict):
        raise LegacyTransitionError("release identity record must be an object")
    return value


def read_identity_record(path: Path) -> tuple[dict, bytes]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 32768:
                raise LegacyTransitionError("invalid release identity file")
            data = stream.read(32769)
        if len(data) > 32768:
            raise LegacyTransitionError("release identity file exceeded limit")
        return decode_identity(data), data
    except OSError as exc:
        raise LegacyTransitionError("release identity file unavailable") from exc


def file_digest(path: Path, *, limit: int = 64 * 1024 * 1024, follow_symlinks: bool = False) -> str:
    try:
        flags = os.O_RDONLY | os.O_NONBLOCK
        if not follow_symlinks:
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise LegacyTransitionError("identity witness is not a regular file")
            digest = hashlib.sha256()
            consumed = 0
            while chunk := stream.read(65536):
                consumed += len(chunk)
                if consumed > limit:
                    raise LegacyTransitionError("identity witness exceeded limit")
                digest.update(chunk)
            return digest.hexdigest()
    except OSError as exc:
        raise LegacyTransitionError("identity witness unavailable") from exc


def bounded_probe(command: list[str], *, cwd: Path, home: Path, timeout: float = 30.0) -> dict:
    """Bound both child lifetime and captured bytes; never return raw diagnostics."""
    environment = {
        "HOME": str(home), "HERMES_HOME": str(home), "ARES_HOME": str(home),
        "ARES_MANAGED_RUNTIME": "1", "PATH": os.defpath,
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    process = subprocess.Popen(
        command, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    output = bytearray()
    total = 0
    try:
        with selectors.DefaultSelector() as selector:
            assert process.stdout is not None and process.stderr is not None
            selector.register(process.stdout, selectors.EVENT_READ, True)
            selector.register(process.stderr, selectors.EVENT_READ, False)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LegacyTransitionError("release import probe timed out")
                for key, _event in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > 16384:
                        raise LegacyTransitionError("release import probe exceeded output limit")
                    if key.data:
                        output.extend(chunk)
            try:
                code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise LegacyTransitionError("release import probe timed out") from exc
            if code:
                raise LegacyTransitionError("release import probe failed")
        return decode_identity(bytes(output))
    finally:
        # Own child group only. A returned leader does not prove descendants
        # closed inherited descriptors or exited; cleanup is independent.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


_IMPORT_PROBE = """
import json
from pathlib import Path
import sys
import ares_runtime.local_runtime as owner
import hermes_cli.main as cli
root = Path(sys.argv[1]).resolve(strict=True)
assert Path(sys.prefix).resolve() == root / '.venv'
assert Path(owner.__file__).resolve() == root / 'ares_runtime/local_runtime.py'
assert Path(cli.__file__).resolve() == root / 'hermes_cli/main.py'
assert all(callable(getattr(owner.AresLocalRuntime, name, None)) for name in
           ('setup', 'rollback', '_materialize', '_refresh_moved_editable_install', 'locked'))
print(json.dumps({
    'venv_prefix': str(Path(sys.prefix).resolve()),
    'controller_file': str(Path(owner.__file__).resolve()),
    'cli_file': str(Path(cli.__file__).resolve()),
    'contract_present': hasattr(owner, 'LOCAL_LIFECYCLE_CONTRACT'),
    'contract': getattr(owner, 'LOCAL_LIFECYCLE_CONTRACT', None),
    'transition_contract': getattr(owner, 'LEGACY_TRANSITION_CONTRACT', None),
}))
"""


def probe_owned_imports(source: Path, python: Path, *, cwd: Path, home: Path, legacy: bool) -> dict:
    try:
        identity = bounded_probe([str(python), "-I", "-B", "-c", _IMPORT_PROBE, str(source)], cwd=cwd, home=home)
    except (OSError, LegacyTransitionError) as exc:
        raise LegacyTransitionError("release-owned final imports could not be established") from exc
    if set(identity) != {"venv_prefix", "controller_file", "cli_file", "contract_present", "contract", "transition_contract"}:
        raise LegacyTransitionError("unexpected release import identity fields")
    expected_paths = {
        "venv_prefix": str(source / ".venv"),
        "controller_file": str(source / "ares_runtime/local_runtime.py"),
        "cli_file": str(source / "hermes_cli/main.py"),
    }
    if any(identity[name] != value for name, value in expected_paths.items()):
        raise LegacyTransitionError("release import owner mismatch")
    if legacy:
        if identity["contract_present"] is not False or identity["contract"] is not None or identity["transition_contract"] is not None:
            raise LegacyTransitionError("new-controller source is not a legacy release")
    elif (identity["contract_present"] is not True or type(identity["contract"]) is not int
          or identity["contract"] != 1 or type(identity["transition_contract"]) is not int
          or identity["transition_contract"] != 1):
        raise LegacyTransitionError("candidate controller cannot own the legacy transition")
    return {name: identity[name] for name in expected_paths}


def require_legacy_binding(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != LEGACY_BINDING_FIELDS:
        raise LegacyTransitionError("missing or invalid legacy rollback binding")
    if value["schema"] != LEGACY_BINDING_SCHEMA or value["controller_contract"] != "legacy-v0":
        raise LegacyTransitionError("unsupported legacy rollback binding")
    if any(not isinstance(item, str) or not item or len(item) > 4096 for item in value.values()):
        raise LegacyTransitionError("invalid legacy rollback identity fields")
    for field, length in (("revision", 40), ("git_tree", 40), ("descriptor_sha256", 64), ("python_sha256", 64), ("venv_config_sha256", 64)):
        item = value[field]
        if len(item) != length or any(char not in "0123456789abcdef" for char in item):
            raise LegacyTransitionError("invalid legacy rollback digest")
    return dict(value)
