#!/usr/bin/env python3
"""Inspect a source-bound cold context; never resume, send, claim or mutate.

The request file is a JSON object containing only ``files`` with the same file
binding shape as run_checkpoint_resume.py. The read-only SessionDB checkpoint,
not this request, supplies expected digests and recorded scope. Default output
is a safe summary. --output writes the private derived brief to a NEW file;
there is no overwrite option and no provider/credential lookup.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ares_runtime.continuity.checkpoint import CheckpointContextError, read_checkpoint_context
from hermes_state import SessionDB
from scripts.run_checkpoint_resume import ResumeRefusal, read_file_bytes, strict_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--generation", required=True, type=int)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-brief-bytes", type=int, default=65536)
    args = parser.parse_args(argv)
    db = None
    try:
        if not args.db.is_absolute() or args.db.is_symlink() or not args.db.is_file():
            raise CheckpointContextError("INVALID_DB")
        if args.output is not None and not args.output.is_absolute():
            raise CheckpointContextError("INVALID_OUTPUT")
        request = strict_json(read_file_bytes(str(args.request)))
        if type(request) is not dict or set(request) != {"files"}:
            raise CheckpointContextError("INVALID_REQUEST")
        db = SessionDB(db_path=args.db, read_only=True)
        result = read_checkpoint_context(db, run_id=args.run_id,
            expected_generation=args.generation, files=request["files"], max_brief_bytes=args.max_brief_bytes)
        if args.output is not None:
            raw = json.dumps({"inspection": result.summary(),
                "manifest": json.loads(result.brief.manifest),
                "messages": result.brief.messages()}, ensure_ascii=True, sort_keys=True).encode() + b"\n"
            # O_EXCL refuses both existing files and symlink targets. The chosen
            # parent directory is trusted operator input, not hostile-path isolation.
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        print(json.dumps(result.summary(), sort_keys=True))
        return 0
    except (CheckpointContextError, ResumeRefusal, OSError, sqlite3.Error, ValueError):
        print(json.dumps({"status": "refused", "code": "CHECKPOINT_CONTEXT_REFUSED",
                          "resume_authorized": False, "effects_executed": False}))
        return 2
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
