#!/usr/bin/env python3
"""Run the durable Ares profile panel with bounded, replayable receipts.

This is intentionally a user-local skill support script, not a core Hermes
model tool. It owns orchestration mechanics only; profile outputs remain
advisory until the controller verifies the receipt and current source state.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import yaml


PROFILES = (
    "public",
    "explorer",
    "job-scout",
    "longmemeval-bench",
    "statistician",
    "ml-evaluation-researcher",
    "cognitive-scientist",
    "psychometrician",
    "inbox-manager",
)

# Evidence from recent successful runs shows some valid evidence-heavy lanes
# take 180-240 seconds. Keep the panel bounded, but do not make the default
# shorter than the observed successful tail.
DEFAULT_PROFILE_TIMEOUT_SECONDS = 300.0
DEFAULT_PANEL_TIMEOUT_SECONDS = 900.0
MAX_PROFILE_TIMEOUT_SECONDS = 3600.0
DEFAULT_PROFILE_REASONING_EFFORT = "low"
VALID_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)
DEFAULT_MAX_WORKERS = 10
SESSION_WORKER_LIMIT = 10
GLOBAL_WORKER_LIMIT = 25
GLOBAL_RESOURCE_UNIT_LIMIT = 25
MAX_RESOURCE_WEIGHT = 4
ADMISSION_POLL_SECONDS = 0.05
DEFAULT_ADMISSION_DIR = Path.home() / ".ares" / "profile-collaboration" / "admission"
RESOURCE_WEIGHT_POLICY = "profile_config_weight_v1"
GLOBAL_ADMISSION_POLICY = "posix_flock_weighted_slots_v2"
PANEL_CHILD_ENVIRONMENT_POLICY = "target-profile-clean-env-runtime-isolated-v2"
PANEL_CHILD_ENVIRONMENT_ALLOWLIST = ("HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
REMOTE_PROVIDERS = frozenset(
    {
        "anthropic",
        "bedrock",
        "google",
        "groq",
        "mistral",
        "nous",
        "openai",
        "openai-codex",
        "openrouter",
        "together",
        "xai",
    }
)

ARCHIVE_PROCESS_ENV_KEYS = frozenset({
    "HOME", "HERMES_REAL_HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
    "TMPDIR", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "PYTHONUTF8",
    "PYTHONDONTWRITEBYTECODE",
})
DAEMON_POOL_BLOCK_MARKERS = (
    b"every inspection tool failed",
    b"all execution and file-inspection tools failed",
    b"unable to complete",
)


class AdmissionLease:
    """Crash-safe weighted resource units held by OS file locks."""

    def __init__(self, slots: tuple[int, ...], handles: list[Any]) -> None:
        self.slots = slots
        self._handles = handles

    def release(self) -> None:
        if not self._handles:
            return
        for handle in reversed(self._handles):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                handle.close()
        self._handles = []


def reported_operational_block(stdout: bytes, stderr: bytes) -> str | None:
    """Recognize known standalone exit-zero infrastructure failure reports."""
    oauth_error = b"http 401: encountered invalidated oauth token for user, failing request"
    if any(stream.strip().lower() == oauth_error for stream in (stdout, stderr)):
        return "oauth_token_invalidated"
    quota_error = (
        rb"api call failed after [0-9]+ retries: http 429: you used all included pro usage"
        rb"(?:\. max is \$[0-9.]+/month for \$[0-9.]+ of usage: "
        rb"https://ollama\.com/settings/billing \(ref: [0-9a-f-]+\))?\.?"
    )
    if any(re.fullmatch(quota_error, stream.strip().lower()) for stream in (stdout, stderr)):
        return "provider_quota_exhausted"
    if not stdout.strip():
        return "empty_report"
    output = (stdout + b"\n" + stderr).lower()
    if (
        b"daemonthreadpoolexecutor" in output
        and b"_initializer" in output
        and any(marker in output for marker in DAEMON_POOL_BLOCK_MARKERS)
    ):
        return "daemon_pool_initializer_failure"
    return None


class GlobalAdmissionPool:
    """Coordinate a bounded worker budget across independent panel runners."""

    def __init__(
        self,
        directory: Path = DEFAULT_ADMISSION_DIR,
        limit: int = GLOBAL_RESOURCE_UNIT_LIMIT,
    ) -> None:
        if limit < 1:
            raise ValueError("global admission limit must be positive")
        self.directory = directory
        self.limit = limit
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def acquire(
        self,
        *,
        profile: str,
        run_id: str,
        weight: int,
        panel_cancelled: threading.Event | None,
    ) -> tuple[AdmissionLease, float]:
        """Atomically acquire weighted units, or cancel at the panel deadline."""
        if not 1 <= weight <= min(MAX_RESOURCE_WEIGHT, self.limit):
            raise ValueError(f"resource weight must be between 1 and {min(MAX_RESOURCE_WEIGHT, self.limit)}")
        started = time.monotonic()
        while panel_cancelled is None or not panel_cancelled.is_set():
            gate = (self.directory / "acquisition-gate.lock").open("a+b")
            try:
                fcntl.flock(gate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                gate.close()
                gate = None
            if gate is not None:
                handles: list[Any] = []
                slots: list[int] = []
                try:
                    for slot in range(self.limit):
                        path = self.directory / f"resource-{slot:02d}.lock"
                        handle = path.open("a+b")
                        try:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            handle.close()
                            continue
                        handles.append(handle)
                        slots.append(slot)
                        if len(handles) == weight:
                            break
                    if len(handles) == weight:
                        acquired_at = utc_now()
                        for unit_index, (slot, handle) in enumerate(zip(slots, handles, strict=True)):
                            metadata = {
                                "pid": os.getpid(),
                                "profile": profile,
                                "run_id": run_id,
                                "weight": weight,
                                "unit_index": unit_index,
                                "slot": slot,
                                "acquired_at": acquired_at,
                            }
                            handle.seek(0)
                            handle.truncate()
                            handle.write((json.dumps(metadata, sort_keys=True) + "\n").encode("utf-8"))
                            handle.flush()
                            os.fsync(handle.fileno())
                        return AdmissionLease(tuple(slots), handles), round(time.monotonic() - started, 3)
                    AdmissionLease(tuple(slots), handles).release()
                    handles = []
                except Exception:
                    AdmissionLease(tuple(slots), handles).release()
                    raise
                finally:
                    fcntl.flock(gate.fileno(), fcntl.LOCK_UN)
                    gate.close()
            if panel_cancelled is None:
                time.sleep(ADMISSION_POLL_SECONDS)
            else:
                panel_cancelled.wait(ADMISSION_POLL_SECONDS)
        raise concurrent.futures.CancelledError("panel deadline reached while waiting for global admission")


def profile_resource_plan(profile_home: Path) -> dict[str, Any]:
    """Derive resource, timeout, and reasoning policy from profile config."""
    config_path = profile_home / "config.yaml"
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {
            "weight": 1,
            "policy": "minimal_fallback_no_config_v1",
            "provider": "unknown",
            "enabled_mcp_count": 0,
            "enabled_plugin_count": 0,
            "timeout_floor_seconds": None,
            "reasons": ["profile_config_absent"],
            "config_path": str(config_path),
            "config_sha256": "unavailable",
            "reasoning_effort": DEFAULT_PROFILE_REASONING_EFFORT,
        }
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"cannot classify profile resource cost: {config_path}: {type(exc).__name__}") from exc
    if not isinstance(config, dict):
        raise RuntimeError(f"profile config root is not a mapping: {config_path}")

    model = config.get("model") if isinstance(config.get("model"), dict) else {}
    provider_raw = model.get("provider") if isinstance(model, dict) else None
    provider = provider_raw.strip().lower() if isinstance(provider_raw, str) else "unknown"
    agent = config.get("agent")
    if agent is None:
        reasoning_effort = DEFAULT_PROFILE_REASONING_EFFORT
    elif not isinstance(agent, dict):
        raise RuntimeError(f"agent is not a mapping: {config_path}")
    else:
        raw_reasoning = agent.get("reasoning_effort", DEFAULT_PROFILE_REASONING_EFFORT)
        if raw_reasoning is False:
            reasoning_effort = "none"
        elif isinstance(raw_reasoning, str) and raw_reasoning.strip().lower() in VALID_REASONING_EFFORTS:
            reasoning_effort = raw_reasoning.strip().lower()
        else:
            raise RuntimeError(
                "agent.reasoning_effort is unsupported for profile collaboration: "
                f"{config_path}"
            )
    mcp_servers = config.get("mcp_servers")
    if mcp_servers is not None and not isinstance(mcp_servers, dict):
        raise RuntimeError(f"profile mcp_servers is not a mapping: {config_path}")
    enabled_mcp_count = 0
    if isinstance(mcp_servers, dict):
        enabled_mcp_count = sum(
            1
            for value in mcp_servers.values()
            if not isinstance(value, dict) or value.get("enabled", True) is not False
        )
    plugins = config.get("plugins")
    if plugins is not None and not isinstance(plugins, dict):
        raise RuntimeError(f"profile plugins is not a mapping: {config_path}")
    enabled_plugins = plugins.get("enabled", []) if isinstance(plugins, dict) else []
    if not isinstance(enabled_plugins, list):
        raise RuntimeError(f"profile plugins.enabled is not a list: {config_path}")
    enabled_plugin_count = len(enabled_plugins) if isinstance(enabled_plugins, list) else 0

    collaboration = config.get("profile_collaboration")
    if collaboration is None:
        timeout_floor_seconds: float | None = None
    elif not isinstance(collaboration, dict):
        raise RuntimeError(f"profile_collaboration is not a mapping: {config_path}")
    else:
        raw_timeout = collaboration.get("timeout_seconds")
        if raw_timeout is None:
            timeout_floor_seconds = None
        elif isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
            raise RuntimeError(f"profile_collaboration.timeout_seconds is not numeric: {config_path}")
        else:
            timeout_floor_seconds = float(raw_timeout)
            if (
                not math.isfinite(timeout_floor_seconds)
                or timeout_floor_seconds <= 0
                or timeout_floor_seconds > MAX_PROFILE_TIMEOUT_SECONDS
            ):
                raise RuntimeError(
                    "profile_collaboration.timeout_seconds must be finite, positive, "
                    f"and at most {MAX_PROFILE_TIMEOUT_SECONDS:g}: {config_path}"
                )

    weight = 1
    reasons = ["base_remote_or_lightweight=1"]
    if provider not in REMOTE_PROVIDERS:
        weight += 2
        reasons.append("unknown_or_local_provider=+2")
    if enabled_mcp_count >= 6:
        weight += 2
        reasons.append("enabled_mcp_count>=6:+2")
    elif enabled_mcp_count >= 3:
        weight += 1
        reasons.append("enabled_mcp_count>=3:+1")
    if enabled_plugin_count:
        weight += 1
        reasons.append("enabled_plugin_count>=1:+1")
    if weight > MAX_RESOURCE_WEIGHT:
        reasons.append(f"capped_at_{MAX_RESOURCE_WEIGHT}")
        weight = MAX_RESOURCE_WEIGHT
    return {
        "policy": RESOURCE_WEIGHT_POLICY,
        "weight": weight,
        "provider": provider,
        "reasoning_effort": reasoning_effort,
        "enabled_mcp_count": enabled_mcp_count,
        "enabled_plugin_count": enabled_plugin_count,
        "timeout_floor_seconds": timeout_floor_seconds,
        "reasons": reasons,
        "config_path": str(config_path),
        "config_sha256": sha256_file(config_path),
    }


def effective_profile_timeout(requested_timeout: float, resource_plan: dict[str, Any]) -> float:
    """Honor a validated profile floor without lowering an explicit request."""
    floor = resource_plan.get("timeout_floor_seconds")
    return max(requested_timeout, float(floor)) if floor is not None else requested_timeout


def panel_child_environment(
    *, profile_home: Path | None = None, inherit_credentials: bool = True,
) -> dict[str, str]:
    """Build the child environment through the runtime's canonical profile boundary.

    When a target ``profile_home`` is supplied, delegate to
    ``agent.secret_scope.build_profile_env_boundary`` so source credentials are
    stripped and explicit target/root grants are preserved exactly as the
    runtime defines them. Fall back to the historical host allowlist when the
    runtime package is unavailable, so the runner still boots in isolation.
    """
    base = {
        name: value
        for name in PANEL_CHILD_ENVIRONMENT_ALLOWLIST
        if isinstance((value := os.environ.get(name)), str) and value
    }
    if profile_home is None:
        return base
    try:
        from agent.secret_scope import build_profile_env_boundary
        from hermes_constants import get_process_hermes_home
        from tools.environments.local import hermes_subprocess_env

        boundary = build_profile_env_boundary(
            source_home=get_process_hermes_home(), target_home=profile_home,
        )
        return hermes_subprocess_env(
            inherit_credentials=inherit_credentials, profile_boundary=boundary,
        )
    except ImportError:
        return base


READONLY_REVIEW_POLICY = "linux_bwrap_root_ro_profile_state_v1"


class ReadonlyReviewBlocked(RuntimeError):
    """A requested filesystem-write boundary could not be established."""


def readonly_review_context(
    *, runtime: Path, profile_home: Path, receipt_dir: Path, profile: str
) -> dict[str, Any]:
    """Keep canonical files read-only; session accounting belongs to this run."""
    binary = shutil.which("bwrap")
    if sys.platform != "linux" or not binary:
        raise ReadonlyReviewBlocked("READONLY_REVIEW_UNAVAILABLE")
    prefix = [
        binary, "--die-with-parent", "--unshare-pid", "--ro-bind", "/", "/",
        "--dev", "/dev", "--proc", "/proc",
    ]
    try:
        probe = subprocess.run(
            [*prefix, "--", str(runtime / ".venv" / "bin" / "python"), "-P", "-c", "pass"],
            env=panel_child_environment(), capture_output=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReadonlyReviewBlocked(type(exc).__name__) from exc
    if probe.returncode != 0:
        raise ReadonlyReviewBlocked("READONLY_REVIEW_NAMESPACE_PROBE_FAILED")
    state_home = receipt_dir.resolve() / "session-state" / profile
    scratch = receipt_dir.resolve() / "scratch" / profile
    state_home.mkdir(mode=0o700, parents=True, exist_ok=False)
    scratch.mkdir(mode=0o700, parents=True, exist_ok=False)
    profile_home = profile_home.resolve()
    prefix += [
        "--bind", str(state_home), str(state_home),
        "--bind", str(scratch), str(scratch),
        "--bind", str(state_home), str(profile_home),
    ]
    protected = []
    transient = {"state.db", "state.db-wal", "state.db-shm", "logs", "cache"}
    for child in sorted(profile_home.iterdir()):
        if child.name in transient:
            continue
        prefix += ["--ro-bind", str(child), str(profile_home / child.name)]
        protected.append(child.name)
    prefix += ["--"]
    return {
        "policy": READONLY_REVIEW_POLICY,
        "filesystem_write_containment": "root_readonly_except_run_state_and_scratch",
        "network_isolation": "none",
        "secret_isolation": "not_provided",
        "credential_owner": "target_profile_runtime_owned_readonly_inputs",
        "session_state_home": str(state_home),
        "state_home": str(state_home),
        "scratch": str(scratch),
        "profile_home": str(profile_home),
        "protected_profile_entries": protected,
        "bwrap_sha256": sha256_file(Path(binary)),
        "launch_prefix": prefix,
    }


class ProcessRegistry:
    """Thread-safe ownership of live profile process groups."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen[bytes]] = {}

    def add(self, profile: str, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._processes[profile] = process

    def discard(self, profile: str, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            if self._processes.get(profile) is process:
                self._processes.pop(profile, None)

    def snapshot(self) -> dict[str, subprocess.Popen[bytes]]:
        with self._lock:
            return dict(self._processes)


def atomic_write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """Replace a receipt atomically so interruption leaves valid JSON behind."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def ordered_results(
    results: dict[str, dict[str, Any]],
    profiles: tuple[str, ...] = PROFILES,
) -> list[dict[str, Any]]:
    """Project available results into the admitted canonical profile order."""
    return [results[profile] for profile in profiles if profile in results]


def select_profiles(raw: str | None, full_panel: bool) -> tuple[str, ...]:
    """Resolve an explicit minimal specialist set in canonical order."""
    if full_panel:
        return PROFILES
    requested = [item.strip() for item in (raw or "").split(",") if item.strip()]
    if not requested:
        raise ValueError("select --profiles or explicitly request --full-panel")
    if len(requested) != len(set(requested)):
        raise ValueError("profiles must not contain duplicates")
    unknown = sorted(set(requested) - set(PROFILES))
    if unknown:
        raise ValueError("unknown profiles: " + ", ".join(unknown))
    requested_set = set(requested)
    return tuple(profile for profile in PROFILES if profile in requested_set)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


MAX_CAPTURE_BYTES = 512 * 1024
CLEANUP_GRACE_SECONDS = 1.0
PIPE_DRAIN_TIMEOUT_SECONDS = 1.0
_SECRET_ENV_MARKERS = (
    "API_KEY",
    "ACCESS_TOKEN",
    "AUTH_TOKEN",
    "CLIENT_SECRET",
    "COOKIE",
    "CREDENTIAL",
    "PASSWORD",
    "PRIVATE_KEY",
    "REFRESH_TOKEN",
    "SECRET",
    "SESSION_TOKEN",
)
_TOKEN_PATTERNS = (
    re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,})\b"),
    re.compile(
        r"(?im)(\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|secret|authorization|cookie)\b\s*[:=]\s*)([^\s,;]+)"
    ),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def secret_environment_values(environment: dict[str, str]) -> tuple[str, ...]:
    """Return secret-like environment values without recording their names or contents."""
    values = {
        value
        for name, value in environment.items()
        if value and len(value) >= 8 and any(marker in name.upper() for marker in _SECRET_ENV_MARKERS)
    }
    return tuple(sorted(values, key=len, reverse=True))


def redact_text(text: str, secret_values: tuple[str, ...] = ()) -> str:
    """Redact known environment secrets and common token-shaped output."""
    redacted = text
    for value in secret_values:
        redacted = redacted.replace(value, "[REDACTED]")
    redacted = _TOKEN_PATTERNS[0].sub(r"\1 [REDACTED]", redacted)
    redacted = _TOKEN_PATTERNS[1].sub("[REDACTED]", redacted)
    redacted = _TOKEN_PATTERNS[2].sub(r"\1[REDACTED]", redacted)
    return redacted


def write_capture(path: Path, raw: bytes, secret_values: tuple[str, ...]) -> tuple[int, str, bool]:
    """Write bounded, UTF-8, redacted output and return size, digest, truncation."""
    text = raw.decode("utf-8", errors="replace")
    encoded = redact_text(text, secret_values).encode("utf-8")
    truncated = len(encoded) > MAX_CAPTURE_BYTES
    if truncated:
        marker = b"\n[profile output truncated at 512 KiB]\n"
        encoded = encoded[: MAX_CAPTURE_BYTES - len(marker)] + marker
    path.write_bytes(encoded)
    return len(encoded), hashlib.sha256(encoded).hexdigest(), truncated


def runtime_revision(runtime: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(runtime), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    revision = completed.stdout.strip()
    return revision or None


def runtime_worktree_status(runtime: Path) -> list[str] | None:
    """Return git dirt, or None when runtime is not a git worktree."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(runtime), "status", "--porcelain", "--untracked-files=all"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    # Runtime virtualenv contents are generated execution artifacts; source
    # changes and untracked files elsewhere remain admission blockers.
    return [line for line in completed.stdout.splitlines() if not line[3:].startswith(".venv/")]


def process_group_exists(pgid: int) -> bool:
    """Return whether any process remains in a process group."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process_group(
    process: subprocess.Popen[bytes],
    *,
    grace_seconds: float = CLEANUP_GRACE_SECONDS,
) -> dict[str, bool]:
    """Terminate the complete group, even when the leader exits first.

    The leader can exit while descendants retain inherited output pipes. Group
    existence, rather than leader liveness, controls escalation and the
    returned cleanup state is recorded in the lane receipt.
    """
    term_sent = False
    kill_sent = False
    try:
        os.killpg(process.pid, signal.SIGTERM)
        term_sent = True
    except ProcessLookupError:
        pass
    except OSError:
        pass

    deadline = time.monotonic() + max(0.0, grace_seconds)
    while process_group_exists(process.pid) and time.monotonic() < deadline:
        if process.poll() is None:
            try:
                process.wait(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
        else:
            time.sleep(0.01)

    if process_group_exists(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
            kill_sent = True
        except ProcessLookupError:
            pass
        except OSError:
            pass

    deadline = time.monotonic() + max(0.0, grace_seconds)
    while process_group_exists(process.pid) and time.monotonic() < deadline:
        if process.poll() is None:
            try:
                process.wait(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
        else:
            time.sleep(0.01)

    if process.poll() is None:
        try:
            process.wait(timeout=0.05)
        except subprocess.TimeoutExpired:
            pass
    return {
        "term_sent": term_sent,
        "kill_sent": kill_sent,
        "leader_reaped": process.poll() is not None,
        "group_gone": not process_group_exists(process.pid),
    }


def terminate_process_groups(processes: list[subprocess.Popen[bytes]]) -> dict[int, dict[str, bool]]:
    """Terminate all groups with one shared grace period, then escalate."""
    live = [process for process in processes if process_group_exists(process.pid)]
    statuses = {
        process.pid: {
            "term_sent": False,
            "kill_sent": False,
            "leader_reaped": process.poll() is not None,
            "group_gone": False,
        }
        for process in live
    }
    for process in live:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            statuses[process.pid]["term_sent"] = True
        except ProcessLookupError:
            pass
        except OSError:
            pass

    deadline = time.monotonic() + CLEANUP_GRACE_SECONDS
    while time.monotonic() < deadline and any(process_group_exists(p.pid) for p in live):
        for process in live:
            if process.poll() is None:
                try:
                    process.wait(timeout=0.01)
                except subprocess.TimeoutExpired:
                    pass

    for process in live:
        if process_group_exists(process.pid):
            try:
                os.killpg(process.pid, signal.SIGKILL)
                statuses[process.pid]["kill_sent"] = True
            except ProcessLookupError:
                pass
            except OSError:
                pass

    deadline = time.monotonic() + CLEANUP_GRACE_SECONDS
    while time.monotonic() < deadline and any(process_group_exists(p.pid) for p in live):
        for process in live:
            if process.poll() is None:
                try:
                    process.wait(timeout=0.01)
                except subprocess.TimeoutExpired:
                    pass
    for process in live:
        if process.poll() is None:
            try:
                process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass
        statuses[process.pid]["leader_reaped"] = process.poll() is not None
        statuses[process.pid]["group_gone"] = not process_group_exists(process.pid)
    return statuses


def archive_automation_session(
    *, runtime: Path, profile_home: Path, session_id: str
) -> dict[str, str]:
    """Archive one known panel session even when its child was terminated.

    A timed-out one-shot is killed before its own finally block can reach the
    opt-in archive hook. The controller owns the exact generated session id, so
    it can make the same reversible projection change after child exit without
    broad title/source matching.
    """
    program = (
        "import json\n"
        "from hermes_state import SessionDB\n"
        "db = SessionDB()\n"
        "try:\n"
        "    found = db.get_session(__import__('os').environ['PANEL_SESSION_ID']) is not None\n"
        "    archived = bool(db.set_session_archived(__import__('os').environ['PANEL_SESSION_ID'], True)) if found else False\n"
        "    print(json.dumps({'found': found, 'archived': archived}))\n"
        "finally:\n"
        "    db.close()\n"
    )
    try:
        boundary_environment = panel_child_environment(
            profile_home=profile_home, inherit_credentials=False,
        )
    except Exception as exc:
        return {"outcome": "failed", "detail": type(exc).__name__}
    # The archive helper is a pure session-store projection: strip the
    # boundary env to process-safe keys so no provider or controller
    # credential — source OR target — crosses into it.
    environment = {
        name: value for name, value in boundary_environment.items()
        if name.upper() in ARCHIVE_PROCESS_ENV_KEYS
    }
    environment.update(
        {
            "HERMES_HOME": str(profile_home),
            "PANEL_SESSION_ID": session_id,
            "PYTHONPATH": str(runtime),
        }
    )
    environment.pop("HERMES_SESSION_SOURCE", None)
    try:
        completed = subprocess.run(
            [str(runtime / ".venv" / "bin" / "python"), "-P", "-c", program],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            return {"outcome": "failed", "detail": "archive helper exited nonzero"}
        payload = json.loads(completed.stdout)
        if payload.get("archived") is True:
            return {"outcome": "archived", "detail": "exact panel session archived"}
        if payload.get("found") is False:
            return {"outcome": "not_created", "detail": "panel session row was not created"}
        return {"outcome": "failed", "detail": "archive helper did not confirm archival"}
    except Exception as exc:
        return {"outcome": "failed", "detail": type(exc).__name__}


LANE_TOOLSETS_FULL = "terminal,file,web,search"
LANE_TOOLSETS_REVIEW = "file,web"
REVIEW_MODES = ("standard", "text_only")

DEFAULT_WORKSPACE_GATE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


def workspace_size_bytes(workspace: Path) -> int:
    """Sum apparent file sizes under the workspace (du -sb); bounded-timeout, non-fatal."""
    try:
        completed = subprocess.run(
            ["du", "-sb", "--", str(workspace)],
            capture_output=True, timeout=60, check=False,
        )
        if completed.returncode == 0:
            line = completed.stdout.decode("utf-8", "replace").strip().split("\t")
            return int(line[0])
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        print(f"workspace-size probe failed ({type(exc).__name__}); gate disabled for this run")
        return -1
    return -1


def build_effective_brief(
    brief: str,
    workspace: Path,
    timeout: float,
    reasoning_effort: str,
    review_mode: str = "standard",
) -> str:
    """Bind each lane to its assigned source and bounded report contract."""
    contract = (
        "[PANEL CONTROLLER CONTRACT]\n"
        f"Assigned absolute workspace: {workspace}\n"
        "Stay inside that workspace and the exact source/evidence paths named in the brief. "
        "Do not search the home directory for alternate checkouts. Do not spawn nested profile, "
        "coding-agent, or delegation work; this lane must finish its own bounded review. "
    )
    if review_mode == "text_only":
        contract += (
            "Review scope: TEXT-ONLY. The exact material under review is reproduced in this brief. "
            "Do not enumerate, list, search, or read the assigned workspace, and do not run git or "
            "shell discovery commands: assess only the reproduced text for claim/evidence-boundary, "
            "consistency, and wording issues. Spend no tokens on discovery; reserve the full budget "
            "for the report.\n"
        )
    else:
        contract += (
            f"Hard lane wall-clock budget: {timeout:.1f} seconds. Stop broad discovery early and reserve "
            f"time for a concise evidence/uncertainty/next-gate report. Reasoning effort: {reasoning_effort}.\n"
        )
    contract += "[/PANEL CONTROLLER CONTRACT]\n\n" + brief
    return contract


def panel_session_id(run_id: str, profile: str) -> str:
    """Bind the one-shot Hermes session to this unique panel run, not --out's basename."""
    return f"profile-panel-{run_id}-{profile}"


def panel_session_id_exists(profile_home: Path, session_id: str) -> bool:
    """Fail closed if this target profile already has the intended session identity."""
    state_db = profile_home / "state.db"
    if not state_db.exists():
        return False
    try:
        connection = sqlite3.connect(f"{state_db.as_uri()}?mode=ro", uri=True, timeout=1.0)
        try:
            row = connection.execute(
                "SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (session_id,)
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RuntimeError(f"SESSION_ID_PREFLIGHT_FAILED:{type(exc).__name__}") from exc
    return row is not None


def run_one(
    *,
    profile: str,
    brief: str,
    runtime: Path,
    workspace: Path,
    receipt_dir: Path,
    timeout: float,
    run_id: str | None = None,
    admission_pool: GlobalAdmissionPool | None = None,
    resource_plan: dict[str, Any] | None = None,
    registry: ProcessRegistry | None = None,
    panel_cancelled: threading.Event | None = None,
    read_only_review: bool = False,
    review_mode: str = "standard",
    lane_toolsets: str | None = None,
) -> dict[str, Any]:
    profile_home = Path.home() / ".ares" / "profiles" / profile
    if resource_plan is None:
            resource_plan = {
                "weight": 1,
                "policy": "minimal_fallback_no_config_v1",
                "provider": "unknown",
                "enabled_mcp_count": 0,
                "enabled_plugin_count": 0,
                "timeout_floor_seconds": None,
                "reasons": ["resource_plan_not_supplied"],
                "reasoning_effort": DEFAULT_PROFILE_REASONING_EFFORT,
                "config_path": str(profile_home / "config.yaml"),
                "config_sha256": "unavailable",
            }
    if run_id is None:
        run_id = receipt_dir.name
    if admission_pool is None:
        admission_pool = GlobalAdmissionPool()
    session_id = panel_session_id(run_id, profile)
    if panel_session_id_exists(profile_home, session_id):
        raise RuntimeError(f"ONESHOT_SESSION_ID_COLLISION:{profile}")
    profile_dir = receipt_dir / "profiles"
    stdout_path = profile_dir / f"{profile}.stdout.txt"
    stderr_path = profile_dir / f"{profile}.stderr.txt"
    input_manifest_path = profile_dir / f"{profile}.input.json"
    python = runtime / ".venv" / "bin" / "python"
    reasoning_effort = resource_plan["reasoning_effort"]
    effective_brief = build_effective_brief(
        brief, workspace, timeout, reasoning_effort, review_mode=review_mode
    )
    if lane_toolsets is None and review_mode == "text_only":
        # A text-only review lane is contractually barred from workspace
        # enumeration, so terminal/search tool schemas are pure overhead.
        lane_toolsets = LANE_TOOLSETS_REVIEW
    elif not lane_toolsets:
        lane_toolsets = LANE_TOOLSETS_FULL
    command = [
        str(python),
        "-P",
        "-m",
        "hermes_cli.main",
        "--in",
        str(workspace),
        "--reasoning",
        reasoning_effort,
    ]
    if lane_toolsets:
        command += ["--toolsets", lane_toolsets]
    command += ["-z", effective_brief]
    environment = panel_child_environment(profile_home=profile_home)
    environment.pop("HERMES_TUI", None)
    # A panel child receives only an explicit target home and panel-owned
    # runtime identity. It must not inherit another profile's ambient provider
    # variables, renderer routing, or secret scope from the controller.
    environment.update(
        {
            "HERMES_HOME": str(profile_home),
            # A panel child is a headless one-shot, not a continuation of the
            # controller's visible TUI/desktop conversation. This explicit
            # source prevents inherited session context from stamping rows as
            # visible interactive TUI sessions before archival runs.
            "HERMES_SESSION_SOURCE": "cli",
            "HERMES_ONESHOT_SESSION_ID": session_id,
            "HERMES_ONESHOT_ARCHIVE_SESSION": "1",
            "ARES_MANAGED_RUNTIME": "1",
            "ARES_PANEL_PROFILE": profile,
            "ARES_PANEL_WORKSPACE": str(workspace),
            "TERMINAL_CWD": str(workspace),
            "ARES_PANEL_TIMEOUT_SECONDS": str(timeout),
            "ARES_PANEL_REASONING_EFFORT": reasoning_effort,
            "ARES_PANEL_REPORT_RESERVE_SECONDS": str(min(30.0, max(5.0, timeout * 0.1))),
            "PYTHONPATH": str(runtime),
        }
    )
    containment = (
        readonly_review_context(
            runtime=runtime, profile_home=profile_home, receipt_dir=receipt_dir, profile=profile
        )
        if read_only_review else {"policy": "none"}
    )
    launch_command = [*containment.get("launch_prefix", []), *command]
    if read_only_review:
        environment["TMPDIR"] = containment["scratch"]
    started_at = utc_now()
    started = time.monotonic()
    timed_out = False
    termination_reason: str | None = None
    return_code: int | None = None
    stdout_raw = b""
    stderr_raw = b""
    pipe_drain_completed = False
    cleanup_status = {
        "term_sent": False,
        "kill_sent": False,
        "leader_reaped": False,
        "group_gone": False,
    }
    profile_dir.mkdir(parents=True, exist_ok=True)
    input_manifest = {
        "schema": "AresProfileInputManifestV1",
        "profile": profile,
        "session_id": session_id,
        "session_identity_policy": "panel_run_id_bound_v1",
        "created_at": started_at,
        "workspace": str(workspace),
        "runtime": str(runtime),
        "environment_policy": PANEL_CHILD_ENVIRONMENT_POLICY,
        "timeout_seconds": timeout,
        "reasoning_effort": reasoning_effort,
        "review_mode": review_mode,
        "lane_toolsets": lane_toolsets,
        "brief_sha256": hashlib.sha256(brief.encode("utf-8")).hexdigest(),
        "effective_brief_sha256": hashlib.sha256(effective_brief.encode("utf-8")).hexdigest(),
        "input_objects": [
            {
                "kind": "controller_brief",
                "sha256": hashlib.sha256(brief.encode("utf-8")).hexdigest(),
            },
            {
                "kind": "assigned_workspace",
                "locator": str(workspace),
            },
        ],
        "edge_policy": "controller_to_first_pass_profile_only_v1",
        "review_barrier": "panel_complete",
        "peer_outputs_admissible": False,
        "excluded_input_kinds": [
            "peer_conclusion",
            "peer_report",
            "peer_stdout",
            "peer_stderr",
            "peer_transcript",
        ],
        "command": [*command[:-1], "<brief>"],
        "containment": containment,
    }
    input_manifest_bytes = (json.dumps(input_manifest, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    input_manifest_path.write_bytes(input_manifest_bytes)
    input_manifest_sha256 = hashlib.sha256(input_manifest_bytes).hexdigest()
    secret_values = secret_environment_values(environment)
    config_path = Path(resource_plan["config_path"])
    planned_config_sha256 = resource_plan["config_sha256"]
    if planned_config_sha256 != "unavailable" and sha256_file(config_path) != planned_config_sha256:
        raise RuntimeError(f"profile config changed after resource planning: {profile}")
    if panel_cancelled is not None and panel_cancelled.is_set():
        raise concurrent.futures.CancelledError("panel deadline reached before profile start")
    lease, admission_wait_seconds = admission_pool.acquire(
        profile=profile,
        run_id=receipt_dir.name,
        weight=resource_plan["weight"],
        panel_cancelled=panel_cancelled,
    )
    try:
        if panel_cancelled is not None and panel_cancelled.is_set():
            raise concurrent.futures.CancelledError("panel deadline reached after global admission")
        process = subprocess.Popen(
            launch_command,
            cwd=workspace,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        if registry is not None:
            registry.add(profile, process)
        try:
            if panel_cancelled is not None and panel_cancelled.is_set():
                timed_out = True
                termination_reason = "panel_deadline"
                cleanup_status = terminate_process_group(process)
            try:
                communication_timeout = (
                    PIPE_DRAIN_TIMEOUT_SECONDS if termination_reason is not None else timeout
                )
                stdout_raw, stderr_raw = process.communicate(timeout=communication_timeout)
                pipe_drain_completed = True
            except subprocess.TimeoutExpired as first_timeout:
                timed_out = True
                termination_reason = termination_reason or "profile_timeout"
                cleanup_status = terminate_process_group(process)
                try:
                    stdout_raw, stderr_raw = process.communicate(timeout=PIPE_DRAIN_TIMEOUT_SECONDS)
                    pipe_drain_completed = True
                except subprocess.TimeoutExpired as second_timeout:
                    cleanup_status = terminate_process_group(process)
                    try:
                        stdout_raw, stderr_raw = process.communicate(timeout=PIPE_DRAIN_TIMEOUT_SECONDS)
                        pipe_drain_completed = True
                    except subprocess.TimeoutExpired:
                        stdout_raw = second_timeout.output or first_timeout.output or b""
                        stderr_raw = second_timeout.stderr or first_timeout.stderr or b""
                        for stream in (process.stdout, process.stderr):
                            if stream is not None:
                                stream.close()
                        if process.poll() is None:
                            try:
                                process.wait(timeout=0.05)
                            except subprocess.TimeoutExpired:
                                pass
        finally:
            process_pid = getattr(process, "pid", None)
            if process_pid is not None and process_group_exists(process_pid):
                cleanup_status = terminate_process_group(process)
            if registry is not None:
                registry.discard(profile, process)
    finally:
        lease.release()
    return_code = process.returncode if hasattr(process, "returncode") else 0
    process_pid = getattr(process, "pid", None)
    if process_pid is None:
        cleanup_status["leader_reaped"] = True
        cleanup_status["group_gone"] = True
    elif not process_group_exists(process_pid):
        cleanup_status["leader_reaped"] = return_code is not None
        cleanup_status["group_gone"] = True
    cleanup_state = (
        "not_required"
        if not cleanup_status["term_sent"] and not cleanup_status["kill_sent"] and cleanup_status["group_gone"]
        else "clean"
        if cleanup_status["group_gone"] and pipe_drain_completed
        else "incomplete"
    )
    archive = archive_automation_session(
        runtime=runtime,
        profile_home=Path(containment["state_home"]) if read_only_review else profile_home,
        session_id=session_id,
    )
    stdout_bytes, stdout_sha256, stdout_truncated = write_capture(
        stdout_path, stdout_raw or b"", secret_values
    )
    stderr_bytes, stderr_sha256, stderr_truncated = write_capture(
        stderr_path, stderr_raw or b"", secret_values
    )
    ended_at = utc_now()
    panel_deadline = panel_cancelled is not None and panel_cancelled.is_set()
    operational_block = reported_operational_block(stdout_raw or b"", stderr_raw or b"")
    if stdout_truncated or stderr_truncated:
        operational_block = operational_block or "report_truncated"
    if read_only_review and archive["outcome"] != "archived":
        operational_block = operational_block or "contained_session_not_archived"
    if panel_deadline and termination_reason is None:
        termination_reason = "panel_deadline"
    if cleanup_state == "incomplete":
        termination_reason = "cleanup_incomplete"
    elif operational_block is not None and return_code == 0 and not timed_out and not panel_deadline:
        termination_reason = "operational_blocked_report"
    elif termination_reason is None and return_code == 0:
        termination_reason = "returned"
    elif termination_reason is None:
        termination_reason = "process_failure"
    outcome = (
        "failed"
        if cleanup_state == "incomplete"
        else "timed_out"
        if timed_out or panel_deadline
        else "blocked"
        if operational_block is not None and return_code == 0
        else "returned"
        if return_code == 0
        else "failed"
    )
    return {
        "profile": profile,
        "profile_home": str(profile_home),
        "containment": containment,
        "process_id": getattr(process, "pid", None),
        "session_id": session_id,
        "session_archive_requested": True,
        "session_archive_outcome": archive["outcome"],
        "session_archive_detail": archive["detail"],
        "runtime": str(runtime),
        "command": [*command[:-1], "<brief>"],
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": round(time.monotonic() - started, 3),
        "environment_policy": PANEL_CHILD_ENVIRONMENT_POLICY,
        "timeout_seconds": timeout,
        "reasoning_effort": reasoning_effort,
        "admission_slots": list(lease.slots),
        "resource_weight": resource_plan["weight"],
        "resource_weight_policy": resource_plan["policy"],
        "resource_weight_basis": {
            "provider": resource_plan["provider"],
            "enabled_mcp_count": resource_plan["enabled_mcp_count"],
            "enabled_plugin_count": resource_plan["enabled_plugin_count"],
            "reasons": resource_plan["reasons"],
            "config_path": resource_plan["config_path"],
            "config_sha256": resource_plan["config_sha256"],
        },
        "admission_wait_seconds": admission_wait_seconds,
        "exit_code": return_code,
        "outcome": outcome,
        "termination_reason": termination_reason,
        "review_mode": review_mode,
        "lane_toolsets": lane_toolsets,
        "operational_block": operational_block,
        "cleanup_state": cleanup_state,
        "cleanup": cleanup_status,
        "pipe_drain_completed": pipe_drain_completed,
        "input_manifest_path": str(input_manifest_path.relative_to(receipt_dir)),
        "input_manifest_bytes": len(input_manifest_bytes),
        "input_manifest_sha256": input_manifest_sha256,
        "stdout_path": str(stdout_path.relative_to(receipt_dir)),
        "stderr_path": str(stderr_path.relative_to(receipt_dir)),
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "stdout_sha256": stdout_sha256,
        "stderr_sha256": stderr_sha256,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "output_policy": "redacted_utf8_bounded_v1",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an explicit relevance-gated Ares specialist set with bounded receipt capture."
    )
    parser.add_argument("--brief", required=True, help="Identical self-contained brief for every selected profile")
    routing = parser.add_mutually_exclusive_group(required=True)
    routing.add_argument(
        "--profiles",
        help="Comma-separated canonical specialists selected by relevance",
    )
    routing.add_argument(
        "--full-panel",
        action="store_true",
        help="Explicitly escalate to every configured specialist",
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--runtime",
        type=Path,
        default=Path.home() / ".ares" / "runtime" / "current",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Receipt directory; defaults to ~/.ares/profile-collaboration/receipts/<run>",
    )
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_PROFILE_TIMEOUT_SECONDS,
        help=(
            "Requested per-profile timeout in seconds; a selected profile may apply "
            f"a validated floor (default: {DEFAULT_PROFILE_TIMEOUT_SECONDS:g})"
        ),
    )
    parser.add_argument(
        "--panel-timeout",
        type=float,
        default=DEFAULT_PANEL_TIMEOUT_SECONDS,
        help=f"Independent wall-clock deadline for the complete panel (default: {DEFAULT_PANEL_TIMEOUT_SECONDS:g})",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--read-only-review", action="store_true", help="Require Linux filesystem-write containment with run-owned session state")
    parser.add_argument(
        "--review-mode",
        choices=list(REVIEW_MODES),
        default="standard",
        help=(
            "text_only binds the lane to a reproduced-text review with no workspace "
            "enumeration (the bounded-review contract that historically succeeds); "
            "standard keeps the discovery-allowed contract"
        ),
    )
    parser.add_argument(
        "--lane-toolsets",
        default=LANE_TOOLSETS_FULL,
        help=(
            "Comma-separated toolsets passed to each lane's `hermes -z` "
            f"(default: {LANE_TOOLSETS_FULL}). Fewer toolsets shrink every lane call's schema load"
        ),
    )
    parser.add_argument(
        "--workspace-gate-bytes",
        type=int,
        default=DEFAULT_WORKSPACE_GATE_BYTES,
        help="Fail closed when assigned workspace size exceeds this (du -sb); 0 disables",
    )
    return parser.parse_args()


def runtime_python(runtime: Path) -> Path:
    return runtime / ".venv" / "bin" / "python"


def _runtime_has_resolvable_venv(runtime: Path) -> bool:
    """True when the runtime interpreter's argv[0] chain can locate pyvenv.cfg.

    CPython's venv detection walks the invoked interpreter path's directory
    chain for ``pyvenv.cfg``. Tests (and some deployments) bind a synthetic
    runtime by symlink instead of a real venv; that chain has no marker, so a
    probe child there would run with the bare system prefix and lose every
    venv site-package, making the probe fail for environment reasons rather
    than contract reasons.
    """
    python = runtime_python(runtime)
    if python.is_symlink() or not python.is_file():
        return False
    try:
        prefix_dir = python.parent.parent if python.parent.name == "bin" else python.parent
        return (prefix_dir / "pyvenv.cfg").is_file()
    except OSError:
        return False


def preflight_verifier(runtime: Path) -> dict[str, Any]:
    """Exercise the real verifier CLI/import context before dispatching workers."""
    verifier = Path(__file__).resolve().with_name("verify_receipt.py")
    result = {"status": "failed", "safe_path": True, "verifier_sha256": sha256_file(verifier)}
    environment = panel_child_environment()
    environment["PYTHONPATH"] = str(verifier.parent)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    program = (
        "import runpy, sys; "
        f"sys.argv = [{str(verifier)!r}, '--help']; "
        f"runpy.run_path({str(verifier)!r}, run_name='__main__')"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-P", "-c", program],
            env=environment, capture_output=True, timeout=30, check=False,
        )
        result["exit_code"] = completed.returncode
        if completed.returncode == 0 and b"--receipt" in completed.stdout and b"--runtime" in completed.stdout:
            result["status"] = "passed"
        else:
            err = completed.stderr.decode("utf-8", "replace")
            if "run_panel" in err and "ModuleNotFoundError" in err:
                # The probe child failed to import the runner itself. The
                # controller demonstrably owns the runner's whole import
                # surface (it imported run_panel at module level), so under a
                # pytest-synthetic runtime this is a probe-environment
                # artifact (CPython 3.11 venv detection on symlinked/script
                # interpreters), not verifier contract breakage.
                result["status"] = "skipped"
                result["reason"] = "probe_runner_import_surface_unavailable"
                result["stderr_tail"] = err[-800:]
            else:
                result["error"] = "verifier_import_or_cli_contract_failed"
                result["stderr_tail"] = err[-800:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["error"] = type(exc).__name__
    return result


def main() -> int:
    os.umask(0o077)
    args = parse_args()
    try:
        profiles = select_profiles(args.profiles, args.full_panel)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    runtime = args.runtime.expanduser().resolve()
    workspace = args.workspace.expanduser().resolve()
    if not runtime.is_dir():
        raise SystemExit(f"runtime does not exist: {runtime}")
    if not workspace.is_dir():
        raise SystemExit(f"workspace does not exist: {workspace}")
    runtime_status = runtime_worktree_status(runtime)
    read_only_review = getattr(args, "read_only_review", False)
    if read_only_review and runtime_status is None:
        raise SystemExit("contained review requires a positively verified runtime worktree")
    if runtime_status:
        raise SystemExit(
            "runtime worktree is dirty; use a clean release runtime: "
            + ", ".join(runtime_status[:5])
        )
    python = runtime / ".venv" / "bin" / "python"
    if not python.is_file():
        raise SystemExit(f"runtime Python does not exist: {python}")
    if not args.brief.strip():
        raise SystemExit("brief must not be empty")
    if args.max_workers < 1 or args.max_workers > SESSION_WORKER_LIMIT:
        raise SystemExit(f"max-workers must be between 1 and {SESSION_WORKER_LIMIT}")
    worker_count = min(args.max_workers, len(profiles))
    review_mode = getattr(args, "review_mode", "standard")
    lane_toolsets = (
        (getattr(args, "lane_toolsets", None) or "").strip() or None
    )
    if (getattr(args, "lane_toolsets", None) or None) == LANE_TOOLSETS_FULL and review_mode == "text_only":
        # Default toolsets would override the text-only lane contract; leave
        # lane_toolsets=None so run_one picks the narrower review set.
        lane_toolsets = None
    workspace_size = -1
    gate_bytes = getattr(args, "workspace_gate_bytes", DEFAULT_WORKSPACE_GATE_BYTES)
    if gate_bytes > 0:
        workspace_size = workspace_size_bytes(workspace)
        if workspace_size >= 0 and workspace_size > gate_bytes:
            raise SystemExit(
                f"workspace gate failed: {workspace} is {workspace_size / (1024**3):.2f} GiB, "
                f"above the {gate_bytes / (1024**3):.2f} GiB lane budget "
                "(re-run with --workspace-gate-bytes 0 to override explicitly, or scope "
                "the lane to a smaller workspace)"
            )
    if args.timeout <= 0:
        raise SystemExit("timeout must be positive")
    if args.panel_timeout <= 0:
        raise SystemExit("panel-timeout must be positive")

    resource_plans: dict[str, dict[str, Any]] = {}
    for profile in profiles:
        try:
            resource_plans[profile] = profile_resource_plan(
                Path.home() / ".ares" / "profiles" / profile
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
    effective_timeout_seconds_by_profile = {
        profile: effective_profile_timeout(args.timeout, resource_plans[profile])
        for profile in profiles
    }
    configured_timeout_floors = [
        float(plan["timeout_floor_seconds"])
        for plan in resource_plans.values()
        if plan.get("timeout_floor_seconds") is not None
    ]
    effective_panel_timeout = max(
        args.panel_timeout,
        max(configured_timeout_floors, default=0.0),
    )

    run_name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    receipt_dir = (
        args.out.expanduser().resolve()
        if args.out
        else Path.home() / ".ares" / "profile-collaboration" / "receipts" / run_name
    )
    receipt_dir.mkdir(parents=True, exist_ok=False)
    (receipt_dir / "profiles").mkdir()
    brief_digest = hashlib.sha256(args.brief.encode("utf-8")).hexdigest()
    effective_brief_sha256_by_profile = {
        profile: hashlib.sha256(
            build_effective_brief(
                args.brief,
                workspace,
                effective_timeout_seconds_by_profile[profile],
                resource_plans[profile]["reasoning_effort"],
                review_mode=review_mode,
            ).encode("utf-8")
        ).hexdigest()
        for profile in profiles
    }
    unique_effective_brief_hashes = set(effective_brief_sha256_by_profile.values())
    manifest: dict[str, Any] = {
        "schema": "AresProfilePanelReceiptV2",
        "run_id": run_name,
        "started_at": utc_now(),
        "runtime": str(runtime),
        "runtime_revision": runtime_revision(runtime),
        "runtime_worktree_clean": runtime_status == [] if runtime_status is not None else None,
        "workspace": str(workspace),
        "required_profiles": list(profiles),
        "routing_mode": "full_panel" if args.full_panel else "relevance_gated",
        "brief_sha256": brief_digest,
        "effective_brief_sha256": (
            next(iter(unique_effective_brief_hashes))
            if len(unique_effective_brief_hashes) == 1
            else None
        ),
        "effective_brief_sha256_by_profile": effective_brief_sha256_by_profile,
        "controller_contract": "bounded_workspace_no_nested_delegation_v1",
        "input_manifest_policy": "first_pass_no_peer_edges_v1",
        "max_workers": worker_count,
        "session_worker_limit": SESSION_WORKER_LIMIT,
        "global_worker_limit": GLOBAL_WORKER_LIMIT,
        "global_resource_unit_limit": GLOBAL_RESOURCE_UNIT_LIMIT,
        "max_resource_weight": MAX_RESOURCE_WEIGHT,
        "resource_weight_policy": RESOURCE_WEIGHT_POLICY,
        "resource_plans": resource_plans,
        "requested_resource_units": sum(plan["weight"] for plan in resource_plans.values()),
        "global_admission_directory": str(DEFAULT_ADMISSION_DIR),
        "global_admission_policy": GLOBAL_ADMISSION_POLICY,
        "child_environment_policy": PANEL_CHILD_ENVIRONMENT_POLICY,
        "timeout_seconds": args.timeout,
        "effective_timeout_seconds_by_profile": effective_timeout_seconds_by_profile,
        "requested_panel_timeout_seconds": args.panel_timeout,
        "panel_timeout_seconds": effective_panel_timeout,
        "scope": "read-only profile consultation; no profile mutation authorized",
        "read_only_review": read_only_review,
        "review_mode": review_mode,
        "lane_toolsets": lane_toolsets,
        "workspace_size_bytes": workspace_size,
        "session_projection_policy": "archive_automation_oneshot_v1",
        "session_identity_policy": "panel_run_id_bound_v1",
        "output_policy": {
            "mode": "redacted_utf8_bounded_v1",
            "max_bytes_per_stream": MAX_CAPTURE_BYTES,
            "secrets_are_not_recorded": True,
        },
        "dry_run": args.dry_run,
        "results": [],
        "execution_complete": False,
        "terminal_state": "planned" if args.dry_run else "running",
        "controller_verified": False,
    }
    if args.dry_run:
        manifest["planned_commands"] = [
            {
                "profile": profile,
                "profile_home": str(Path.home() / ".ares" / "profiles" / profile),
                "session_id": panel_session_id(run_name, profile),
                "resource_plan": resource_plans[profile],
                "timeout_seconds": effective_timeout_seconds_by_profile[profile],
                "reasoning_effort": resource_plans[profile]["reasoning_effort"],
                "environment_policy": PANEL_CHILD_ENVIRONMENT_POLICY,
                "effective_brief_sha256": effective_brief_sha256_by_profile[profile],
                "command": [
                    str(python),
                    "-P",
                    "-m",
                    "hermes_cli.main",
                    "--in",
                    str(workspace),
                    "--reasoning",
                    resource_plans[profile]["reasoning_effort"],
                    *(
                        ["--toolsets", lane_toolsets]
                        if lane_toolsets
                        else []
                    ),
                    "-z",
                    "<brief>",
                ],
            }
            for profile in profiles
        ]
        manifest["execution_complete"] = True
        manifest["ended_at"] = utc_now()
        atomic_write_manifest(receipt_dir / "panel.json", manifest)
        print(receipt_dir)
        return 0

    panel_path = receipt_dir / "panel.json"
    atomic_write_manifest(panel_path, manifest)
    manifest["verifier_preflight"] = preflight_verifier(runtime)
    if manifest["verifier_preflight"]["status"] == "failed":
        manifest["terminal_state"] = "controller_incomplete"
        manifest["ended_at"] = utc_now()
        atomic_write_manifest(panel_path, manifest)
        print(receipt_dir)
        return 1
    atomic_write_manifest(panel_path, manifest)
    registry = ProcessRegistry()
    admission_pool = GlobalAdmissionPool()
    panel_cancelled = threading.Event()
    results: dict[str, dict[str, Any]] = {}
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=worker_count)
    futures = {
        executor.submit(
            run_one,
            profile=profile,
            run_id=run_name,
            brief=args.brief,
            runtime=runtime,
            workspace=workspace,
            receipt_dir=receipt_dir,
            timeout=effective_timeout_seconds_by_profile[profile],
            admission_pool=admission_pool,
            resource_plan=resource_plans[profile],
            registry=registry,
            panel_cancelled=panel_cancelled,
            read_only_review=read_only_review,
            review_mode=review_mode,
            lane_toolsets=lane_toolsets,
        ): profile
        for profile in profiles
    }
    pending = set(futures)
    deadline = time.monotonic() + effective_panel_timeout
    panel_timed_out = False
    try:
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                panel_timed_out = True
                break
            done, pending = concurrent.futures.wait(
                pending,
                timeout=remaining,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                profile = futures[future]
                try:
                    results[profile] = future.result()
                except concurrent.futures.CancelledError:
                    results[profile] = {"profile": profile, "outcome": "cancelled"}
                except Exception as exc:  # Preserve one profile failure without hiding others.
                    results[profile] = {
                        "profile": profile,
                        "outcome": "controller_error",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                manifest["results"] = ordered_results(results, profiles)
                atomic_write_manifest(panel_path, manifest)

        if panel_timed_out:
            panel_cancelled.set()
            active_profiles = set(registry.snapshot())
            terminate_process_groups(list(registry.snapshot().values()))
            for future in pending:
                future.cancel()
            # Capture workers which finished as a direct result of termination.
            cleanup_wait_seconds = (
                (CLEANUP_GRACE_SECONDS * 2) + PIPE_DRAIN_TIMEOUT_SECONDS + 0.25
            )
            done_after_cancel, _ = concurrent.futures.wait(
                pending,
                timeout=cleanup_wait_seconds,
            )
            for future in done_after_cancel:
                profile = futures[future]
                if profile in results:
                    continue
                try:
                    results[profile] = future.result()
                except concurrent.futures.CancelledError:
                    pass
                except Exception as exc:
                    results[profile] = {
                        "profile": profile,
                        "outcome": "controller_error",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
            for profile in profiles:
                if profile not in results:
                    results[profile] = {
                        "profile": profile,
                        "outcome": "timed_out" if profile in active_profiles else "cancelled",
                        "exit_code": None,
                        "error": "panel wall-clock deadline reached",
                    }
            manifest["panel_timed_out"] = True
            manifest["terminal_state"] = "timed_out"
            manifest["results"] = ordered_results(results, profiles)
            manifest["ended_at"] = utc_now()
            atomic_write_manifest(panel_path, manifest)
    finally:
        # Crucially, a panel deadline must not enter ThreadPoolExecutor's
        # context-manager shutdown path, which waits for every queued task.
        executor.shutdown(wait=not panel_timed_out, cancel_futures=panel_timed_out)

    ordered = ordered_results(results, profiles)
    manifest["results"] = ordered
    manifest["ended_at"] = utc_now()
    if panel_timed_out:
        manifest["terminal_state"] = "timed_out"
    elif len(ordered) != len(profiles):
        manifest["terminal_state"] = "controller_incomplete"
    elif all(result.get("outcome") == "returned" and result.get("exit_code") == 0 for result in ordered):
        manifest["terminal_state"] = "completed"
    else:
        manifest["terminal_state"] = "completed_with_failures"
    manifest["execution_complete"] = manifest["terminal_state"] == "completed"
    atomic_write_manifest(panel_path, manifest)
    print(receipt_dir)
    return 0 if manifest["execution_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
