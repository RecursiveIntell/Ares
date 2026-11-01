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
try:
    import tomllib  # Python 3.11+; only needed by the locked policy path
except ImportError:  # pragma: no cover — py<3.11 can still run --check-uv-version
    tomllib = None
from urllib.parse import urlsplit


class PolicyError(Exception):
    """Safe fixed-message error; never include input text."""


def local_path(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_./ -]+", value)) and not value.startswith("//")


# PEP 508 environment-marker variables. A marker may only compare these to
# short quoted literals; it can never introduce a URL, path, or direct reference.
MARKER_VARIABLES = frozenset({
    "os_name", "sys_platform", "platform_machine", "platform_python_implementation",
    "platform_release", "platform_system", "platform_version", "python_version",
    "python_full_version", "implementation_name", "implementation_version",
})
_MARKER_TOKEN = re.compile(r"""\s*(?:(?P<lit>'[A-Za-z0-9_.*+ -]*'|"[A-Za-z0-9_.*+ -]*")|(?P<op>===|==|!=|<=|>=|~=|<|>)|(?P<paren>[()])|(?P<word>[A-Za-z_]+))""")


def supported_marker(marker):
    """Accept only `variable op 'literal'` comparisons joined by and/or."""
    position, tokens = 0, []
    marker = marker.strip()
    while position < len(marker):
        match = _MARKER_TOKEN.match(marker, position)
        if not match or match.end() == position:
            return False
        position = match.end()
        kind = match.lastgroup
        tokens.append((kind, match.group(kind)))
    depth, expect_operand, index = 0, True, 0
    while index < len(tokens):
        kind, text = tokens[index]
        if expect_operand:
            if kind == "paren" and text == "(":
                depth += 1
                index += 1
                continue
            # operand op operand, where one side is a marker variable and the other a literal
            if index + 2 >= len(tokens):
                return False
            left, op, right = tokens[index], tokens[index + 1], tokens[index + 2]
            if op[0] != "op":
                return False
            sides = {left[0], right[0]}
            variable = left if left[0] == "word" else right
            if sides != {"word", "lit"} or variable[1] not in MARKER_VARIABLES:
                return False
            index += 3
            expect_operand = False
        else:
            if kind == "paren" and text == ")":
                depth -= 1
                if depth < 0:
                    return False
                index += 1
                continue
            if kind == "word" and text in ("and", "or"):
                expect_operand = True
                index += 1
                continue
            return False
    return depth == 0 and not expect_operand


def supported_override(value):
    """A registry requirement string, optionally with a PEP 508 environment marker."""
    if not isinstance(value, str) or value.count(";") > 1:
        return False
    requirement, separator, marker = value.partition(";")
    if not re.fullmatch(r"[A-Za-z0-9_.<>=!,~ -]+", requirement) or not requirement.strip():
        return False
    return not separator or supported_marker(marker)


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
    if not isinstance(overrides, list) or any(not supported_override(value) for value in overrides):
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
    if version.returncode or not uv_version_supported(parse_uv_version(version.stdout)):
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


UV_VERSION_RANGE = ((0, 9, 28), (0, 13, 0))
UV_VERSION_RANGE_TEXT = "0.9.28 through 0.12.x"


def parse_uv_version(stdout):
    """Parse ``uv --version`` output into a (major, minor, patch) tuple, or None."""
    match = re.fullmatch(r"uv (\d+)\.(\d+)\.(\d+)(?: [^\n]*)?\n?", stdout or "")
    if not match:
        return None
    return tuple(map(int, match.groups()))


def uv_version_supported(version):
    """True when ``version`` (a parsed tuple, or None) is inside the reviewed range."""
    return version is not None and UV_VERSION_RANGE[0] <= version < UV_VERSION_RANGE[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root")
    parser.add_argument("--uv", required=True)
    parser.add_argument("--python")
    parser.add_argument("--check-uv-version", action="store_true",
                        help="Only validate the managed uv version against the reviewed range")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check_uv_version:
        version = subprocess.run([args.uv, "--version"], capture_output=True,
                                 text=True, encoding="utf-8", errors="strict")
        if version.returncode or not uv_version_supported(parse_uv_version(version.stdout)):
            print(f"Unsupported uv version; reviewed range is {UV_VERSION_RANGE_TEXT}", file=sys.stderr)
            return 2
        return 0
    if not args.root or not args.python:
        parser.error("--root and --python are required unless --check-uv-version is given")
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
