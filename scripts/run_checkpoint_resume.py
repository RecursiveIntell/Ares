#!/usr/bin/env python3
"""Private read-only SessionDB resume observer, not an effect-admission API.

Run with the source checkout's supported Python and explicit --db/--run-id/
--generation/--request. The request has checkpoint (native RunCheckpoint JSON),
session_id, lease_holder, and files {plan, contract, source, members {name:path}}.
The source file is a nonempty JSON {absolute_path: sha256} inventory; its exact
bytes must match the independently read native source_digest. Every native
member has an exact UTF-8 file binding. Member historical-goal-key names the
native metadata whose raw digest is historical_goal_digest.

Observations are bounded, point-in-time reads, NOT an atomic filesystem/DB
snapshot, current effect authority, or whole runtime/compaction qualification.
There is deliberately no claim, refresh, publish, replay, or effect command.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time

# This private script belongs to this checkout, not an ambient installed copy.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_state import SessionDB
from hermes_state_runs import RunCheckpoint, RunCustodyV2, RunCustodyError

MAX_FILE_BYTES = 8_000_000
MAX_TOTAL_BYTES = 64_000_000
MAX_SOURCE_FILES = 1000


class ResumeRefusal(RuntimeError):
    """Stable codes only: never include raw state, paths, or bearer values."""


def read_file_bytes(path):
    if type(path) is not str or not Path(path).is_absolute():
        raise ResumeRefusal("INVALID_FILE")
    target = Path(path)
    try:
        # No claim of hostile parent-directory race resistance. Final symlink
        # and special-file checks prevent accidental redirection/blocking.
        if target.is_symlink():
            raise ResumeRefusal("INVALID_FILE")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
                raise ResumeRefusal("INVALID_FILE")
            raw = stream.read(MAX_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > MAX_FILE_BYTES:
            raise ResumeRefusal("INVALID_FILE")
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise ResumeRefusal("FILE_CHANGED_DURING_READ")
        return raw
    except OSError as exc:
        raise ResumeRefusal("INVALID_FILE") from exc


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ResumeRefusal("DUPLICATE_KEY")
            result[key] = value
        return result
    def bad_constant(_value):
        raise ResumeRefusal("INVALID_JSON")
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=bad_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ResumeRefusal("INVALID_JSON") from exc


def exact_keys(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ResumeRefusal("INVALID_REQUEST")


def check_lease(db, owner, request):
    sid = request["session_id"]
    if type(sid) is not str or sid != owner.current_session_id:
        raise ResumeRefusal("SESSION_MISMATCH")
    holder = request["lease_holder"]
    if type(holder) is not str or not holder:
        raise ResumeRefusal("LEASE_MISMATCH")
    # Existing SessionDB lineage resolver, in the same read context as the
    # lease observation. This does not acquire/refresh or grant run custody.
    with db._read_ctx() as conn:
        session = conn.execute("SELECT ended_at,end_reason FROM sessions WHERE id=?", (sid,)).fetchone()
        if session is None:
            raise ResumeRefusal("SESSION_MISMATCH")
        if session["ended_at"] is not None or session["end_reason"] is not None:
            raise ResumeRefusal("SESSION_NOT_CURRENT")
        root = db._session_turn_lease_key_on_conn(conn, sid)
        row = conn.execute("SELECT holder,expires_at FROM session_turn_leases "
                           "WHERE conversation_id=?", (root,)).fetchone()
    if row is None or row["holder"] != holder:
        raise ResumeRefusal("LEASE_MISMATCH")
    expiry = float(row["expires_at"])
    if not math.isfinite(expiry) or expiry <= time.time():
        raise ResumeRefusal("LEASE_MISMATCH")


def check_goal(db, owner):
    if type(owner) is RunCustodyV2:
        db.validate_run_task_binding(owner)
        return
    key = dict(owner.checkpoint.members).get("historical-goal-key")
    if not key:
        raise ResumeRefusal("HISTORICAL_GOAL_BINDING_MISSING")
    raw = db.get_meta(key)
    if raw is None or hashlib.sha256(raw.encode("utf-8")).hexdigest() != owner.historical_goal_digest:
        raise ResumeRefusal("HISTORICAL_GOAL_MISMATCH")


class BoundFileReader:
    """One bounded observation budget; no authority or atomic snapshot."""

    def __init__(self):
        self.total = 0

    def read(self, path):
        raw = read_file_bytes(path)
        self.total += len(raw)
        if self.total > MAX_TOTAL_BYTES:
            raise ResumeRefusal("READ_BUDGET_EXCEEDED")
        return raw

    def verified(self, path, digest):
        if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ResumeRefusal("INVALID_DIGEST")
        raw = self.read(path)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ResumeRefusal("FILE_DIGEST_MISMATCH")
        return raw


def verify_checkpoint_files(checkpoint, files, reader):
    """Compare caller-selected files with explicit checkpoint bindings."""
    exact_keys(files, ("plan", "contract", "source", "members"))
    reader.verified(files["plan"], checkpoint.plan_digest)
    reader.verified(files["contract"], checkpoint.contract_digest)
    inventory = strict_json(reader.verified(files["source"], checkpoint.source_digest))
    if type(inventory) is not dict or not 1 <= len(inventory) <= MAX_SOURCE_FILES:
        raise ResumeRefusal("INVALID_SOURCE_INVENTORY")
    for source, digest in inventory.items():
        reader.verified(source, digest)
    members = dict(checkpoint.members)
    if type(files["members"]) is not dict or set(files["members"]) != set(members):
        raise ResumeRefusal("MEMBER_BINDINGS_MISMATCH")
    for name, raw in members.items():
        if reader.read(files["members"][name]) != raw.encode("utf-8"):
            raise ResumeRefusal("MEMBER_MISMATCH")
    return len(inventory)


def inspect_resume(db_path, run_id, generation, request_path):
    path = Path(db_path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ResumeRefusal("INVALID_DB")
    reader = BoundFileReader()
    request = strict_json(reader.read(str(request_path)))
    exact_keys(request, ("checkpoint", "files", "session_id", "lease_holder"))
    files = request["files"]
    exact_keys(files, ("plan", "contract", "source", "members"))
    db = None
    try:
        db = SessionDB(db_path=path, read_only=True)
        owner = db.read_run_custody(run_id)
        if owner is None or owner.generation != generation or type(generation) is not int:
            raise ResumeRefusal("FENCE_MISMATCH")
        checkpoint = RunCheckpoint.from_dict(request["checkpoint"])
        if checkpoint != owner.checkpoint:
            raise ResumeRefusal("CHECKPOINT_MISMATCH")
        def native_check():
            value = db.validate_run_resume(run_id, owner_token=owner.owner_token,
                expected_generation=generation, source_digest=checkpoint.source_digest,
                plan_digest=checkpoint.plan_digest, contract_digest=checkpoint.contract_digest)
            if value != owner:
                raise ResumeRefusal("NATIVE_STATE_CHANGED")
            check_lease(db, owner, request)
            check_goal(db, owner)
        native_check()
        source_count = verify_checkpoint_files(checkpoint, files, reader)
        native_check()
        return {"status": "resume_consistency", "run_id": owner.run_id,
                "generation": owner.generation, "resume_authorized": False,
                "effects_executed": False, "source_files": source_count,
                "members": len(checkpoint.members), "unresolved_effects": len(checkpoint.unresolved_effects),
                "unresolved_findings": len(checkpoint.unresolved_findings),
                "restrictions": len(checkpoint.restrictions)}
    except RunCustodyError as exc:
        raise ResumeRefusal(exc.code) from exc
    except (sqlite3.Error, OSError) as exc:
        raise ResumeRefusal("STORE_UNAVAILABLE") from exc
    except (TypeError, ValueError, KeyError, RecursionError) as exc:
        raise ResumeRefusal("INVALID_REQUEST") from exc
    finally:
        if db is not None:
            db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--generation", required=True, type=int)
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = inspect_resume(args.db, args.run_id, args.generation, args.request)
    except ResumeRefusal as exc:
        print(json.dumps({"status": "refused", "code": str(exc),
                          "resume_authorized": False, "effects_executed": False}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
