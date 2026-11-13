#!/usr/bin/env python3
"""Opt-in POSIX scientific runner. Fixed operations, explicit trusted backends."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any

MAX_INPUT = 65_536
MAX_CAPTURE = 262_144
CHECKER_FILES = (
    "claim_ledger/__init__.py", "claim_ledger/io_json.py",
    "claim_ledger/science/__init__.py", "claim_ledger/science/contracts.py",
    "claim_ledger/science/checks.py", "claim_ledger/science/export.py",
    "claim_ledger/science/__main__.py",
)
BOOTSTRAP = (
    "import runpy,sys;root=sys.argv.pop(1);sys.path.insert(0,root);"
    "runpy.run_module('claim_ledger.science',run_name='__main__')"
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_file(path: Path, limit: int = MAX_INPUT) -> bytes:
    require(not path.is_symlink(), "symlink input rejected")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        require(stat.S_ISREG(os.fstat(handle.fileno()).st_mode), "regular file required")
        raw = handle.read(limit + 1)
    require(len(raw) <= limit, "file exceeds limit")
    return raw


def backend_identity(root: Path) -> dict[str, Any]:
    sources = {name: digest(read_file(root / name)) for name in CHECKER_FILES}
    raw = json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()
    return {"source_sha256": sources, "sha256": digest(raw)}


def executable_identity(path: Path) -> str:
    require(path.is_absolute() and path.is_file() and os.access(path, os.X_OK),
            "explicit executable path required")
    return digest(read_file(path.resolve(strict=True), 128 * 1024 * 1024))


def inside(workspace: Path, path: Path) -> Path:
    require(not path.is_symlink(), "symlink input rejected")
    resolved = path.resolve(strict=True)
    require(resolved.is_relative_to(workspace), "input outside selected workspace")
    return resolved


def bounded_process(command: list[str], home: Path, timeout: float,
                    payload: bytes = b"", cap: int = MAX_CAPTURE) -> dict[str, Any]:
    """Bound output while reading; terminate the process group even after leader exit.

    This is resource control, not an OS sandbox. Trusted backends must not daemonize.
    """
    require(os.name == "posix", "POSIX required")
    require(math.isfinite(timeout) and 0 < timeout <= 120, "invalid timeout")
    require(0 < cap <= MAX_CAPTURE and len(payload) <= MAX_INPUT, "resource limit")
    started = time.monotonic()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    state = "completed"
    environment = {"PATH": os.defpath, "HOME": str(home), "LANG": "C.UTF-8"}
    with tempfile.TemporaryFile() as input_file:
        input_file.write(payload)
        input_file.seek(0)
        process = subprocess.Popen(command, cwd=home, env=environment, stdin=input_file,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        try:
            with selectors.DefaultSelector() as selector:
                for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                while selector.get_map() or process.poll() is None:
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        state = "timeout"
                        break
                    for key, _ in selector.select(min(remaining, 0.1)):
                        chunk = os.read(key.fileobj.fileno(), 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        target = output[key.data]
                        remaining_bytes = cap - len(target)
                        target.extend(chunk[:remaining_bytes])
                        if len(chunk) > remaining_bytes:
                            state = "output_limit"
                            break
                    if state != "completed":
                        break
        finally:
            # A descendant may retain the pipes after its parent has exited.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
    if state == "completed" and process.returncode != 0:
        state = "failed"
    return {"state": state, "returncode": process.returncode,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "stdout": bytes(output["stdout"]), "stderr": bytes(output["stderr"])}


def run(args: argparse.Namespace) -> dict[str, Any]:
    require(os.name == "posix", "POSIX required")
    require(1 <= args.timeout <= 120 and 1 <= args.budget <= 10_000, "invalid resource bound")
    workspace = args.workspace.resolve(strict=True)
    root = args.claimledger_root.resolve(strict=True)
    identity = backend_identity(root)
    require(identity["sha256"] == args.checker_sha256, "checker identity mismatch")
    python = args.python.absolute()
    python_sha = executable_identity(python)
    names = {"verify": ("statement", "problem", "candidate"),
             "solve": ("statement", "problem"), "diff": ("before", "after")}[args.mode]
    raw = {}
    for name in names:
        path = getattr(args, name)
        require(path is not None, "missing operation input")
        raw[name] = read_file(inside(workspace, path))
    kernel_sha = None
    if args.mode == "solve":
        require(args.kernel is not None and args.kernel_sha256 is not None, "explicit kernel identity required")
        kernel_sha = executable_identity(args.kernel.absolute())
        require(kernel_sha == args.kernel_sha256, "kernel identity mismatch")
    out = args.out.absolute()
    out.mkdir(mode=0o700, parents=False, exist_ok=False)
    private_home = out / "home"
    private_home.mkdir(mode=0o700)
    for name, data in raw.items():
        (out / f"{name}.json").write_bytes(data)
    command = [str(python), "-I", "-c", BOOTSTRAP, str(root)]
    report = {"schema": "AresFalsifyRunV1", "mode": args.mode, "status": "running", "steps": [],
              "checker": identity, "python_sha256": python_sha, "kernel_sha256": kernel_sha,
              "input_sha256": {name: digest(data) for name, data in raw.items()},
              "support_admission": "not_performed", "sandboxed": False}

    def step(name: str, argv: list[str], payload: bytes = b"") -> bytes:
        require(backend_identity(root) == identity, "checker changed during run")
        require(executable_identity(python) == python_sha, "Python executable changed")
        result = bounded_process(argv, private_home, args.timeout, payload)
        stdout, stderr = result.pop("stdout"), result.pop("stderr")
        (out / f"{name}.stdout").write_bytes(stdout)
        (out / f"{name}.stderr").write_bytes(stderr)
        report["steps"].append({"name": name, "argv": list(argv), **result,
                                "stdout_sha256": digest(stdout), "stderr_sha256": digest(stderr)})
        require(result["state"] == "completed", f"{name} did not complete")
        return stdout

    try:
        if args.mode == "diff":
            step("check", command + ["diff", "--before", str(out / "before.json"),
                                     "--after", str(out / "after.json"), "--out", str(out / "evidence")])
        else:
            base = ["--statement", str(out / "statement.json"), "--problem", str(out / "problem.json")]
            if args.mode == "solve":
                step("request", command + ["request", *base, "--budget", str(args.budget),
                                            "--out", str(out / "request")])
                step("request-replay", command + ["verify-export", "--directory", str(out / "request")])
                require(executable_identity(args.kernel.absolute()) == kernel_sha, "kernel changed")
                candidate_raw = step("kernel", [str(args.kernel.absolute())], read_file(out / "request/request.cw"))
                require(len(candidate_raw) <= MAX_INPUT, "candidate exceeds byte limit")
                (out / "candidate.json").write_bytes(candidate_raw)
            step("check", command + ["verify", *base, "--candidate", str(out / "candidate.json"),
                                     "--out", str(out / "evidence")])
        replay_raw = step("replay", command + ["verify-export", "--directory", str(out / "evidence")])
        replay = json.loads(replay_raw)
        require(type(replay) is dict and replay.get("schema") == "science.replay.v1"
                and replay.get("replayed") is True
                and replay.get("support_admission") == "not_performed", "invalid replay result")
        evidence_raw = read_file(out / "evidence/evidence.json")
        require(replay.get("artifact_sha256") == digest(evidence_raw), "evidence changed after replay")
        require(backend_identity(root) == identity and executable_identity(python) == python_sha,
                "backend changed during run")
        report["status"] = "checked"
        report["evidence_sha256"] = digest(evidence_raw)
        report["evidence_verdict"] = json.loads(evidence_raw)["verdict"]
    except (ValueError, OSError, subprocess.SubprocessError, UnicodeError, KeyError, TypeError) as exc:
        report["status"] = "failed"
        report["failure_type"] = type(exc).__name__
    encoded = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode()
    (out / "run.json").write_bytes(encoded)
    (out / "run.sha256").write_text(digest(encoded) + "\n", encoding="ascii")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    identity = sub.add_parser("identity")
    identity.add_argument("--claimledger-root", type=Path, required=True)
    execute = sub.add_parser("run")
    execute.add_argument("--mode", choices=("verify", "diff", "solve"), required=True)
    execute.add_argument("--workspace", type=Path, required=True)
    execute.add_argument("--claimledger-root", type=Path, required=True)
    execute.add_argument("--checker-sha256", required=True)
    execute.add_argument("--python", type=Path, default=Path(sys.executable))
    execute.add_argument("--out", type=Path, required=True)
    for name in ("statement", "problem", "candidate", "before", "after", "kernel"):
        execute.add_argument(f"--{name}", type=Path)
    execute.add_argument("--kernel-sha256")
    execute.add_argument("--timeout", type=int, default=20)
    execute.add_argument("--budget", type=int, default=1000)
    args = parser.parse_args(argv)
    try:
        result = backend_identity(args.claimledger_root.resolve(strict=True)) if args.operation == "identity" else run(args)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("status") != "failed" else 2
    except (ValueError, OSError) as exc:
        print(f"falsify rejected: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
