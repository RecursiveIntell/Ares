"""Bounded, allowlisted npm failure facts. Never emit untrusted log text."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

PREFIX = "HERMES_NPM_DIAGNOSTIC "
SCHEMA = "npm-install-diagnostic/v1"
LIMIT = 256 * 1024
CAUSES = {
    "ERESOLVE": "Dependency resolution conflict",
    "EBADENGINE": "Unsupported Node or npm engine",
    "EACCES": "Filesystem permission denied",
    "EPERM": "Operation not permitted",
    "ENOSPC": "Insufficient disk space",
    "ENOENT": "Required file or executable missing",
    "EINTEGRITY": "Package integrity verification failed",
    "E401": "Registry authentication required or rejected",
    "E403": "Registry access forbidden",
    "E404": "Package or resource not found",
    "ETIMEDOUT": "Network request timed out",
    "ESOCKETTIMEDOUT": "Network socket timed out",
    "ECONNRESET": "Network connection reset",
    "ECONNREFUSED": "Network connection refused",
    "ENOTFOUND": "Network name resolution failed",
    "EAI_AGAIN": "Temporary network name resolution failure",
    "CERT_HAS_EXPIRED": "TLS certificate expired",
    "SELF_SIGNED_CERT_IN_CHAIN": "TLS certificate chain is not trusted",
    "DEPTH_ZERO_SELF_SIGNED_CERT": "TLS peer certificate is self-signed",
    "UNABLE_TO_VERIFY_LEAF_SIGNATURE": "TLS certificate issuer cannot be verified",
    "UNABLE_TO_GET_ISSUER_CERT_LOCALLY": "TLS issuer unavailable in configured trust",
    "ERR_TLS_CERT_ALTNAME_INVALID": "TLS certificate hostname mismatch",
    "ELIFECYCLE": "Package lifecycle script failed",
}
CODE_LINE = re.compile(r"^npm (?:ERR!|error) code ([A-Z0-9_]+)\s*$", re.MULTILINE)


def bounded_tail(path: Path) -> tuple[str, bool]:
    # Refuse links and special files. Do not follow an arbitrary log path into
    # credentials or block indefinitely reading a FIFO.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if path.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise ValueError("unsupported input")
        stream.seek(max(0, info.st_size - LIMIT))
        return stream.read(LIMIT).decode("utf-8", "replace"), info.st_size > LIMIT


def record(stage: str, exit_code: int, elapsed: int, codes: list[str], truncated: bool) -> dict:
    if stage not in ("root", "tui") or type(exit_code) is not int or not 1 <= exit_code <= 255:
        raise ValueError("invalid status")
    if type(elapsed) is not int or not 0 <= elapsed <= 86400 or type(truncated) is not bool:
        raise ValueError("invalid timing")
    if not isinstance(codes, list) or any(not isinstance(code, str) or code not in CAUSES for code in codes):
        raise ValueError("invalid codes")
    codes = sorted(set(codes))
    return {"schema": SCHEMA, "stage": stage, "exit_code": exit_code,
            "elapsed_seconds": elapsed, "timeout_status": exit_code == 124,
            "npm_codes": codes, "safe_causes": [CAUSES[code] for code in codes],
            "output_truncated": truncated,
            "detail": "Raw npm output intentionally omitted; no recognized error code" if not codes else "Allowlisted npm error codes only"}


def summarize(path: Path, stage: str, exit_code: int, elapsed: int) -> dict:
    text, truncated = bounded_tail(path)
    codes = [code for code in CODE_LINE.findall(text) if code in CAUSES]
    return record(stage, exit_code, elapsed, codes, truncated)


def collect(path: Path) -> list[dict]:
    # The installer transcript is current-run input supplied by the test harness.
    # Reconstruct every emitted record; never copy attacker-supplied extra fields
    # or even the purportedly sanitized strings from the transcript.
    text, _ = bounded_tail(path)
    records = []
    for line in text.splitlines():
        if not line.startswith(PREFIX) or len(line) > 8192:
            continue
        try:
            value = json.loads(line[len(PREFIX):])
            if value.get("schema") != SCHEMA:
                continue
            rebuilt = record(value["stage"], value["exit_code"], value["elapsed_seconds"],
                             value["npm_codes"], value["output_truncated"])
            if value != rebuilt:
                continue
            records.append(rebuilt)
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return records[-4:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    summary = sub.add_parser("summarize")
    summary.add_argument("--input", required=True, type=Path)
    summary.add_argument("--stage", required=True, choices=("root", "tui"))
    summary.add_argument("--exit-code", required=True, type=int)
    summary.add_argument("--elapsed", required=True, type=int)
    run = sub.add_parser("new-run")
    run.add_argument("--parent", required=True, type=Path)
    collector = sub.add_parser("collect")
    collector.add_argument("--input", required=True, type=Path)
    collector.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "new-run":
            args.parent.mkdir(parents=True, exist_ok=True)
            print(tempfile.mkdtemp(prefix="run-", dir=args.parent))
        elif args.command == "summarize":
            print(PREFIX + json.dumps(summarize(args.input, args.stage, args.exit_code, args.elapsed), sort_keys=True))
        else:
            data = json.dumps(collect(args.input), indent=2) + "\n"
            # Exclusive creation refuses both old-run files and symlink targets.
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(data)
        return 0
    except (OSError, ValueError, TypeError):
        print("Sanitized npm diagnostics unavailable", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
