"""Run the non-Termux installer with reviewed project policy, never user config.

Only the explicitly supported configuration subset is projected. Project-only
metadata stays in pyproject.toml. Unsupported policy is a fatal, value-free error.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import tomllib
from urllib.parse import urlsplit


class PolicyError(Exception):
    """Safe fixed-message error; never include input text."""


def local_path(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_./ -]+", value)) and not value.startswith("//")


def reject_inline_credentials(value):
    if isinstance(value, dict):
        for item in value.values():
            reject_inline_credentials(item)
    elif isinstance(value, list):
        for item in value:
            reject_inline_credentials(item)
    elif isinstance(value, str):
        for candidate in re.findall(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s]+", value):
            parsed = urlsplit(candidate)
            if parsed.username is not None or parsed.password is not None or parsed.query:
                raise PolicyError("Inline URL credentials or query parameters are unsupported")


def project_policy(data):
    reject_inline_credentials(data)
    policy = data.get("tool", {}).get("uv")
    supported = {"exclude-newer", "exclude-newer-package", "override-dependencies", "sources", "find-links", "no-index"}
    if not isinstance(policy, dict) or set(policy) - supported:
        raise PolicyError("Missing or unsupported reviewed uv policy")
    if policy.get("exclude-newer") != "14 days":
        raise PolicyError("Reviewed 14-day policy is required")
    exceptions = policy.get("exclude-newer-package", {})
    if not isinstance(exceptions, dict) or any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", key) or value is not False
        for key, value in exceptions.items()
    ):
        raise PolicyError("Unsupported package age policy")
    overrides = policy.get("override-dependencies", [])
    if not isinstance(overrides, list) or any(
        not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.<>=!,~ -]+", value)
        for value in overrides
    ):
        raise PolicyError("Unsupported native dependency override")
    sources = policy.get("sources", {})
    if not isinstance(sources, dict) or any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", key)
        or not isinstance(value, dict) or set(value) != {"path"}
        or not local_path(value["path"])
        for key, value in sources.items()
    ):
        raise PolicyError("Unsupported native source policy")
    links = policy.get("find-links", [])
    if not isinstance(links, list) or any(not local_path(value) for value in links):
        raise PolicyError("Unsupported local wheel policy")
    if "no-index" in policy and type(policy["no-index"]) is not bool:
        raise PolicyError("Unsupported index policy")
    lines = ['exclude-newer = "14 days"']
    if "find-links" in policy:
        lines.append("find-links = " + json.dumps(links))
    if "no-index" in policy:
        lines.append("no-index = " + str(policy["no-index"]).lower())
    lines.append("[exclude-newer-package]")
    lines.extend(json.dumps(key) + " = false" for key in exceptions)
    return "\n".join(lines) + "\n"


def child_environment(environment, root, python):
    env = dict(environment)
    # These controls cannot relax resolution policy or select another project.
    safe_paths = {"UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_PYTHON_BIN_DIR"}
    expected = {"UV_NO_CONFIG": "1", "UV_PYTHON": str(python),
                "UV_PROJECT_ENVIRONMENT": str(root / "venv"),
                "UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never"}
    for key, value in env.items():
        if not key.startswith("UV_"):
            continue
        if key in safe_paths and Path(value).is_absolute():
            continue
        if key not in expected or value != expected[key]:
            raise PolicyError("Unsupported inherited uv environment; remove overrides before retrying")
    env.update(UV_NO_CONFIG="1", UV_PYTHON=str(python), UV_PROJECT_ENVIRONMENT=str(root / "venv"))
    return env


def posix_capabilities(platform_name):
    """Require real POSIX process-group cancellation before creating resources."""
    if platform_name != "posix":
        raise PolicyError("Reviewed installer requires POSIX process-group support")
    group_signal = getattr(os, "killpg", None)
    signals = tuple(getattr(signal, name, None) for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGKILL"))
    if not callable(group_signal) or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in signals):
        raise PolicyError("Required POSIX cancellation capabilities are unavailable")
    return group_signal, signals


def run(root, uv, python, *, dry_run=False, check=False):
    group_signal, (interrupt_signal, terminate_signal, hangup_signal, kill_signal) = posix_capabilities(os.name)
    root = Path(root).resolve(strict=True)
    python = Path(python).absolute()
    if python != root / "venv/bin/python" or not python.is_file() or not os.access(python, os.X_OK):
        raise PolicyError("Expected an existing installer-owned venv interpreter")
    uv_path = Path(uv)
    if not uv_path.is_absolute() or not uv_path.is_file() or not os.access(uv_path, os.X_OK):
        raise PolicyError("Expected an absolute executable uv path")
    uv = str(uv_path.resolve(strict=True))
    env = child_environment(os.environ, root, python)
    inputs = [root / "pyproject.toml", root / "uv.lock"]
    if any(path.is_symlink() or not path.is_file() for path in inputs) or (root / "uv.toml").exists():
        raise PolicyError("Expected regular reviewed manifest and lock without competing uv.toml")
    original = [path.read_bytes() for path in inputs]
    try:
        projection = project_policy(tomllib.loads(original[0].decode("utf-8")))
    except (ValueError, TypeError, AttributeError, UnicodeError):
        raise PolicyError("Malformed reviewed project policy") from None
    version = subprocess.run([uv, "--version"], env=env, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="strict")
    match = re.fullmatch(r"uv (\d+)\.(\d+)\.(\d+)(?: [^\n]*)?\n?", version.stdout)
    if version.returncode or not match or not ( (0, 9, 28) <= tuple(map(int, match.groups())) < (0, 13, 0)):
        raise PolicyError("Unsupported uv version; reviewed range is 0.9.28 through 0.12.x")
    filename = None
    child = None
    previous = {}
    acquiring = False
    pending_signal = None

    def interrupted(signum, _frame):
        nonlocal pending_signal
        if acquiring:
            # Resource constructors may have created the OS handle without yet
            # returning it. Defer only until its ownership has been recorded.
            pending_signal = signum
        else:
            raise InterruptedError(signum)

    def cancellation_point():
        if pending_signal is not None:
            raise InterruptedError(pending_signal)

    try:
        for sig in (interrupt_signal, terminate_signal, hangup_signal):
            previous[sig] = signal.signal(sig, interrupted)
        acquiring = True
        try:
            fd, filename = tempfile.mkstemp(prefix=".ares-reviewed-uv-", suffix=".toml", dir=root)
            # Own and close the descriptor before resuming signal exceptions.
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    fd = None
                    output.write(projection)
            finally:
                if fd is not None:
                    os.close(fd)
        finally:
            acquiring = False
        cancellation_point()
        command = [uv, "--config-file", filename]
        command += ["lock", "--check"] if check else ["sync", "--extra", "all", "--locked"]
        if dry_run:
            command.append("--dry-run")
        acquiring = True
        try:
            child = subprocess.Popen(command, env=env, cwd=root, start_new_session=True)
        finally:
            acquiring = False
        cancellation_point()
        code = child.wait()
        code = code if code >= 0 else 128 - code
    except InterruptedError as error:
        code = 128 + error.args[0]
    finally:
        # Prevent a second catchable signal interrupting child reap/cleanup.
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        if child is not None:
            # Also clean up descendants if their leader has already exited.
            try:
                group_signal(child.pid, terminate_signal)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                group_signal(child.pid, kill_signal)
                child.wait()
            # A descendant can ignore TERM even after the leader exits.
            try:
                group_signal(child.pid, kill_signal)
            except ProcessLookupError:
                pass
        if filename is not None:
            Path(filename).unlink(missing_ok=True)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if any(path.read_bytes() != before for path, before in zip(inputs, original)):
        raise PolicyError("Reviewed project inputs changed during locked execution")
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--uv", required=True)
    parser.add_argument("--python", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        return run(args.root, args.uv, args.python, dry_run=args.dry_run, check=args.check)
    except PolicyError as error:
        print(f"Installer policy preflight failed: {error}", file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print("Installer policy execution failed; no unlocked fallback attempted", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
