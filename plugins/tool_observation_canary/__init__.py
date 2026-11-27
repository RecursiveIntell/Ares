"""OFFLINE staging bridge for the Libraries tool-observation canary.

NOT a production transport and NOT a production-capability claim.  Default
off.  The plugin exposes one zero-argument tool that, at most once per
operator-created private state directory, runs one operator-admitted staged
executable and returns only validated, metadata-only evidence.

Operator admission (``plugins.entries.tool_observation_canary.settings``),
snapshotted once at ``register()`` -- a later config edit cannot hot-override
it; it makes the tool fail closed with ``admission_changed`` until restart:

* ``enabled``: must be exactly ``true``.
* ``executable``: absolute, normalized, symlink-free path.  No PATH lookup.
* ``sha256``: lowercase hex digest the file must match at *every* dispatch.
* ``state_dir``: absolute path of an operator-created 0700 directory, owned by
  the current user, strictly inside the active profile home, and empty before
  the first attempt.

The model supplies nothing: the schema has no properties, and the child argv,
scope and output directory are plugin constants / plugin-derived.  Process
lifetime and captured bytes are bounded by
``ares_runtime.legacy_transition.bounded_probe`` (new session, process-group
SIGKILL, 16 KiB combined output cap, deadline), which is reused unmodified.

Launch-once / timeout reconciliation: an ``attempt.json`` marker is created
exclusively *before* launch and is never removed.  A timeout, output overflow
or I/O error leaves the outcome typed ``unknown``; the killed child's late
result is never adopted and the tool never relaunches -- while the marker
exists every call returns the recorded status without launching.  To try
again the operator must admit a new state directory.

Residual (documented, not hidden): the hash is verified on the path and the
file identity is re-stat'ed immediately before launch, but exec-by-path cannot
be made atomic with the hash; the executable and its parent directory must
therefore be non-writable by group/other and owned by the current user (or
root for the parent).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ares_runtime.legacy_transition import (
    LegacyTransitionError,
    bounded_probe,
    file_digest,
)
from tools.registry import no_cache_check_fn, tool_error, tool_result

logger = logging.getLogger(__name__)

TOOL_NAME = "ares_tool_observation_canary"
TOOLSET = "tool_observation_canary"

# Plugin-owned constants: never taken from tool input or config.
_SCOPE = "ares-tool-observation-offline-canary"
_PROBE_TOOL = "ares_offline_available_parallelism"
_ENABLE_FLAG = "--enable-offline-canary"
_RUN_DIR_NAME = "run"
_MARKER_NAME = "attempt.json"
_LAUNCH_TIMEOUT_S = 20.0
_MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
_MAX_PATH = 4096

_SETTING_KEYS = ("enabled", "executable", "sha256", "state_dir")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_RECEIPT_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_MARKER_STATUSES = frozenset({"launching", "succeeded", "failed", "unknown"})

# Exact messages raised by ``bounded_probe`` (pinned by tests so a change in the
# helper cannot silently demote a timeout to a definite failure).
_TIMEOUT_MSG = "release import probe timed out"
_FAILED_MSG = "release import probe failed"

_SUCCESS_KEYS = frozenset({
    "backend_payload", "canary", "embedder", "executed", "forge_readback",
    "namespace", "projection_readback", "receipt_id", "reopened", "retries",
    "status", "tool_name",
})

SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "OFFLINE staging canary (no production claim). Runs the operator-admitted "
        "tool-observation probe once and returns metadata-only evidence. Takes no "
        "arguments; fails closed unless the operator admitted an executable and a "
        "fresh private state directory."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


class Refusal(Exception):
    """A typed, pre-launch refusal; ``code`` is stable and metadata-only."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Admission:
    executable: str
    sha256: str
    state_dir: str


def _normalized_absolute(value: object, code: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > _MAX_PATH
            or "\x00" in value or not os.path.isabs(value)
            or os.path.normpath(value) != value):
        raise Refusal(code)
    return value


def admit_settings(settings: dict[str, Any]) -> Admission:
    """Validate operator settings syntactically.  Raises :class:`Refusal`."""
    if settings.get("enabled") is not True:
        raise Refusal("disabled")
    digest = settings.get("sha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise Refusal("invalid_admission")
    return Admission(
        executable=_normalized_absolute(settings.get("executable"), "invalid_admission"),
        sha256=digest,
        state_dir=_normalized_absolute(settings.get("state_dir"), "invalid_admission"),
    )


def _profile_home() -> Path:
    from hermes_constants import get_hermes_home

    return Path(os.path.realpath(get_hermes_home()))


def _lstat(path: str, code: str) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as exc:
        raise Refusal(code) from exc


def _verify_state_dir(adm: Admission) -> None:
    info = _lstat(adm.state_dir, "state_dir_invalid")
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or os.path.realpath(adm.state_dir) != adm.state_dir):
        raise Refusal("state_dir_invalid")
    home = _profile_home()
    state = Path(adm.state_dir)
    if state == home or not state.is_relative_to(home):
        raise Refusal("state_dir_invalid")


def _verify_executable(adm: Admission) -> os.stat_result:
    info = _lstat(adm.executable, "executable_missing")
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o022 or not info.st_mode & 0o100
            or info.st_size > _MAX_EXECUTABLE_BYTES
            or os.path.realpath(adm.executable) != adm.executable):
        raise Refusal("executable_unsafe")
    parent = _lstat(os.path.dirname(adm.executable), "executable_unsafe")
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o022
            or parent.st_uid not in (os.geteuid(), 0)):
        raise Refusal("executable_unsafe")
    return info


def verify_admission(adm: Admission, *, hash_executable: bool) -> None:
    """Filesystem recheck.  ``hash_executable`` is False only for ``check_fn``."""
    _verify_state_dir(adm)
    before = _verify_executable(adm)
    if not hash_executable:
        return
    try:
        actual = file_digest(Path(adm.executable), limit=_MAX_EXECUTABLE_BYTES)
    except LegacyTransitionError as exc:
        raise Refusal("executable_unreadable") from exc
    if not hmac.compare_digest(actual, adm.sha256):
        raise Refusal("hash_mismatch")
    after = _verify_executable(adm)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise Refusal("executable_changed")


# --------------------------------------------------------------------- state

def _read_marker(state_dir: str) -> str | None:
    """Return the recorded status, or None when no attempt marker exists."""
    path = os.path.join(state_dir, _MARKER_NAME)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        return "unknown"
    try:
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(4097)
        status = json.loads(data.decode("utf-8")).get("status")
    except (OSError, ValueError, AttributeError):
        return "unknown"
    return status if status in _MARKER_STATUSES and status != "launching" else "unknown"


def _write_marker(state_dir: str, status: str, *, exclusive: bool) -> None:
    path = os.path.join(state_dir, _MARKER_NAME)
    payload = (json.dumps({"schema": 1, "status": status}) + "\n").encode()
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return
    temp = f"{path}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def _record_final(state_dir: str, status: str) -> None:
    try:
        _write_marker(state_dir, status, exclusive=False)
    except OSError:
        # The "launching" marker already forbids relaunch; a read of it maps to
        # ``unknown``, which is the conservative truth here.
        logger.warning("tool_observation_canary: could not record final status")


# ------------------------------------------------------------- output parsing

def _is_true(value: object) -> bool:
    return value is True


def validate_output(value: dict) -> dict:
    """Strictly validate the probe's success record; metadata-only projection."""
    if set(value) != _SUCCESS_KEYS:
        raise ValueError("unexpected field set")
    forge, projection = value["forge_readback"], value["projection_readback"]
    if (not isinstance(forge, dict) or set(forge) != {"found", "scope_matches", "status"}
            or not isinstance(projection, dict) or set(projection) != {"found", "status"}):
        raise ValueError("unexpected readback shape")
    receipt_id = value["receipt_id"]
    checks = (
        value["canary"] == "offline_disposable",
        value["status"] == "succeeded",
        _is_true(value["executed"]), _is_true(value["reopened"]),
        type(value["retries"]) is int and value["retries"] == 0,
        value["tool_name"] == _PROBE_TOOL,
        value["namespace"] == _SCOPE,
        value["embedder"] == "mock_offline_only",
        value["backend_payload"] == "withheld",
        isinstance(receipt_id, str) and _RECEIPT_RE.fullmatch(receipt_id) is not None,
        _is_true(forge["found"]), _is_true(forge["scope_matches"]),
        forge["status"] == "succeeded",
        _is_true(projection["found"]), projection["status"] == "succeeded",
    )
    if not all(checks):
        raise ValueError("success record failed validation")
    return {
        "outcome": "succeeded",
        "receipt_id": receipt_id,
        "namespace": _SCOPE,
        "tool_name": _PROBE_TOOL,
        "forge_readback": "succeeded",
        "projection_readback": "succeeded",
        "retries": 0,
        "offline_only": True,
        "production_claim": False,
    }


# ------------------------------------------------------------------ dispatch

def _unknown(reason: str, *, executed: bool | None = True) -> str:
    # ``executed`` is None when a spawn may not even have happened (OSError).
    return tool_error(
        "outcome unknown; not retried. Inspect the operator state directory and "
        "admit a new one to try again.",
        outcome="unknown", reason=reason, executed=executed, relaunched=False,
    )


def run_once(adm: Admission) -> str:
    """Launch exactly once.  Caller has already rechecked admission + hash."""
    prior = _read_marker(adm.state_dir)
    if prior is not None:
        return tool_error(
            "an attempt is already recorded for this state directory; not relaunched",
            outcome="already_attempted", executed=False, relaunched=False,
            marker_status=prior, marker_is_diagnostic_only=True,
        )
    try:
        if os.listdir(adm.state_dir):
            raise Refusal("state_dir_not_empty")
        _write_marker(adm.state_dir, "launching", exclusive=True)
    except FileExistsError:
        return tool_error("attempt already recorded", outcome="already_attempted",
                          executed=False, relaunched=False,
                          marker_status="unknown", marker_is_diagnostic_only=True)
    except OSError:
        raise Refusal("state_dir_invalid") from None

    command = [
        adm.executable, _ENABLE_FLAG, "--scope", _SCOPE,
        "--out-dir", os.path.join(adm.state_dir, _RUN_DIR_NAME),
    ]
    try:
        raw = bounded_probe(
            command, cwd=Path(adm.state_dir), home=Path(adm.state_dir),
            timeout=_LAUNCH_TIMEOUT_S,
        )
    except LegacyTransitionError as exc:
        message = str(exc)
        if message == _FAILED_MSG:
            _record_final(adm.state_dir, "failed")
            return tool_error(
                "probe exited unsuccessfully; no output is surfaced. Not retried.",
                outcome="failed", reason="probe_exit_nonzero", executed=True,
                relaunched=False,
            )
        # timeout, output-limit overflow, or malformed stdout: the child may
        # have done work.  Never adopt any late result; remain unknown.
        _record_final(adm.state_dir, "unknown")
        return _unknown("launch_timeout" if message == _TIMEOUT_MSG else "unreconciled_output")
    except (OSError, ValueError):
        _record_final(adm.state_dir, "unknown")
        return _unknown("launch_error", executed=None)

    try:
        result = validate_output(raw)
    except (ValueError, KeyError, TypeError):
        _record_final(adm.state_dir, "unknown")
        return _unknown("malformed_or_unexpected_output")
    _record_final(adm.state_dir, "succeeded")
    return tool_result(result)


def _refusal_result(refusal: Refusal) -> str:
    return tool_error(
        f"refused: {refusal.code}", outcome="refused", reason=refusal.code,
        executed=False,
    )


def make_tool(read_settings: Callable[[], dict[str, Any]], frozen: Admission | str):
    """Build ``(check_fn, handler)`` bound to the register-time admission."""

    def _live() -> Admission:
        if isinstance(frozen, str):  # register-time refusal code; restart to change
            raise Refusal(frozen)
        try:
            current = admit_settings(read_settings())
        except Refusal:
            raise
        except Exception as exc:  # config reader failure: fail closed
            raise Refusal("invalid_admission") from exc
        if current != frozen:
            raise Refusal("admission_changed")
        return current

    @no_cache_check_fn
    def check_fn() -> bool:
        try:
            verify_admission(_live(), hash_executable=False)
        except Refusal:
            return False
        except Exception:
            return False
        return True

    def handler(args: dict | None = None, **_kwargs: Any) -> str:
        try:
            if args:
                return tool_error("this tool accepts no arguments", outcome="refused",
                                  reason="arguments_not_accepted", executed=False)
            adm = _live()
            verify_admission(adm, hash_executable=True)
            return run_once(adm)
        except Refusal as refusal:
            return _refusal_result(refusal)
        except Exception:
            logger.exception("tool_observation_canary: unexpected bridge failure")
            return tool_error("unexpected bridge failure", outcome="unknown",
                              reason="bridge_error", relaunched=False)

    return check_fn, handler


def register(ctx) -> None:
    def read_settings() -> dict[str, Any]:
        return {key: ctx.get_config(key) for key in _SETTING_KEYS}

    try:
        frozen: Admission | str = admit_settings(read_settings())
    except Refusal as refusal:
        frozen = refusal.code  # default-off / invalid: registered but gated off
    except Exception:
        frozen = "invalid_admission"
    check_fn, handler = make_tool(read_settings, frozen)
    ctx.register_tool(
        name=TOOL_NAME,
        toolset=TOOLSET,
        schema=SCHEMA,
        handler=handler,
        check_fn=check_fn,
        description=SCHEMA["description"],
        emoji="🧪",
    )
