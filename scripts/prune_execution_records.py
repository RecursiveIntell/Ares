#!/usr/bin/env python3
"""Prune the rebuildable event projection of execution records.

The head of each execution record is never deleted.  This tool removes only
event rows beyond ``--keep`` and reports the head that remains authoritative.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hermes_state import SessionDB


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--execution-id")
    parser.add_argument("--keep", type=int, default=32)
    args = parser.parse_args(argv)
    if args.keep < 1:
        parser.error("--keep must be at least 1")
    db = SessionDB(db_path=args.db)
    try:
        ids = [args.execution_id] if args.execution_id else db.list_execution_ids()
        report = []
        for execution_id in ids:
            record = db.read_execution_record(execution_id)
            removed = db.prune_execution_events(execution_id, keep=args.keep)
            report.append({
                "execution_id": execution_id,
                "state": record.state,
                "head_sequence": record.sequence,
                "removed_events": removed,
                "remaining_events": len(db.read_execution_events(execution_id)),
            })
        print(
            json.dumps(
                {"schema": "ares.execution-record-prune/v1", "records": report},
                indent=2,
            )
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
