"""Native metadata CAS witnesses; these do not certify run custody."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

import hermes_state
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    store = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield store
    finally:
        store.close()


@pytest.mark.parametrize("existing", [None, "old"])
def test_duplicate_batch_keys_refused_without_any_write(db, existing):
    if existing is not None:
        db.set_meta("head", existing)
    with pytest.raises(ValueError, match="duplicate"):
        db.compare_and_set_meta_many([
            ("unrelated", None, "must-not-land"),
            ("head", existing, "first"),
            ("head", existing, "second"),
        ])
    assert db.get_meta("head") == existing
    assert db.get_meta("unrelated") is None


def test_keys_colliding_after_existing_string_conversion_are_duplicates(db):
    with pytest.raises(ValueError, match="duplicate"):
        db.compare_and_set_meta_many([(1, None, "first"), ("1", None, "second")])
    assert db.get_meta("1") is None


def test_failed_preimage_keeps_whole_batch_unchanged(db):
    db.set_meta("head", "old")
    assert not db.compare_and_set_meta_many([
        ("member", None, "candidate"), ("head", "stale", "new")
    ])
    assert db.get_meta("head") == "old"
    assert db.get_meta("member") is None


def test_valid_batch_and_lost_ack_readback(db):
    assert db.compare_and_set_meta_many([
        ("head", None, "generation:1"), ("member:1", None, "payload")
    ])
    # A retry after losing acknowledgement cannot append a second generation.
    assert not db.compare_and_set_meta_many([
        ("head", None, "generation:1"), ("member:1", None, "payload")
    ])
    assert db.get_meta("head") == "generation:1"
    assert db.get_meta("member:1") == "payload"
    assert db.compare_and_set_meta_many([])


def test_sql_failure_rolls_back_earlier_batch_members(db):
    db.set_meta("head", "old")
    db._conn.execute(
        "CREATE TRIGGER reject_member BEFORE INSERT ON state_meta "
        "WHEN NEW.key='member:bad' BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        db.compare_and_set_meta_many([
            ("head", "old", "new"), ("member:bad", None, "payload")
        ])
    assert db.get_meta("head") == "old"
    assert db.get_meta("member:bad") is None


_CONTENDER = r'''
import json, sys
from pathlib import Path
from hermes_state import SessionDB
store = SessionDB(db_path=Path(sys.argv[1]))
print("ready", flush=True)
assert sys.stdin.readline().strip() == "go"
won = store.compare_and_set_meta_many([
    ("head", "old", sys.argv[2]),
    ("member:" + sys.argv[2], None, "payload:" + sys.argv[2]),
])
print(json.dumps({"won": won}), flush=True)
store.close()
'''


def test_two_processes_one_winner_and_no_losing_member(tmp_path):
    path = tmp_path / "state.db"
    with_store = SessionDB(db_path=path)
    with_store.set_meta("head", "old")
    with_store.close()
    workers = []
    try:
        for label in ("first", "second"):
            workers.append(subprocess.Popen(
                [sys.executable, "-c", _CONTENDER, str(path), label],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
                env={
                    **os.environ,
                    "HERMES_HOME": str(tmp_path / label),
                    "PYTHONPATH": str(Path(hermes_state.__file__).resolve().parent),
                },
            ))
        # Parent-controlled barrier: both independent stores are open before CAS.
        for worker in workers:
            ready = worker.stdout.readline().strip()
            if ready != "ready":
                stdout, stderr = worker.communicate(timeout=10)
                pytest.fail(f"contender setup failed: {ready!r} {stdout} {stderr}")
        for worker in workers:
            worker.stdin.write("go\n")
            worker.stdin.flush()
        results = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=20)
            assert worker.returncode == 0, stderr
            results.append(json.loads(stdout)["won"])
        assert sorted(results) == [False, True]
        store = SessionDB(db_path=path)
        try:
            winner = ("first", "second")[results.index(True)]
            loser = ("first", "second")[results.index(False)]
            assert store.get_meta("head") == winner
            assert store.get_meta("member:" + winner) == "payload:" + winner
            assert store.get_meta("member:" + loser) is None
        finally:
            store.close()
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
            worker.communicate(timeout=10)
