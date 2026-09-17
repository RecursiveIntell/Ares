"""Immutable compact refreshes retain checkpoint history without payload copies."""
from dataclasses import replace
import hashlib
import json

import pytest

from hermes_state import SessionDB
from hermes_state_runs import RunCheckpoint, RunCustodyError, generation_key


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def db(tmp_path):
    store = SessionDB(db_path=tmp_path / "state.db")
    yield store
    store.close()


def claim(db):
    cp = RunCheckpoint(digest("plan"), digest("contract"), digest("source"), "observe",
                       (("large-member", "x" * 200000),), ("effect:unknown",),
                       ("finding:open",), ("no effects",))
    return db.claim_run_custody("compact-test", expected_generation=0, checkpoint=cp,
        origin_session_id="origin", current_session_id="current",
        historical_goal_digest=digest("cancelled"), ttl_seconds=300)


def refresh(db, owner):
    return db.refresh_run_custody(owner.run_id, owner_token=owner.owner_token,
                                 expected_generation=owner.generation, ttl_seconds=300)


def logical_bytes(db):
    return db._conn.execute("SELECT coalesce(sum(length(cast(value AS BLOB))),0) "
        "FROM state_meta WHERE key LIKE 'run-custody:compact-test:%'").fetchone()[0]


def test_repeated_refresh_bounds_metadata_and_preserves_every_checkpoint(db):
    owner = claim(db)
    original = owner
    initial = logical_bytes(db)
    first_raw = db.get_meta(generation_key(owner.run_id, 1))
    history = [owner]
    for _ in range(12):
        owner = refresh(db, owner)
        history.append(owner)
    assert logical_bytes(db) - initial <= 12 * 8192
    assert db.get_meta(generation_key(owner.run_id, 1)) == first_raw
    assert owner.checkpoint == original.checkpoint
    assert owner.owner_token == original.owner_token
    for value in history:
        assert db.read_run_checkpoint(owner.run_id, generation=value.generation) == value


def test_refresh_after_publication_uses_new_checkpoint_and_retains_old(db):
    first = claim(db)
    second = refresh(db, first)
    updated = replace(second.checkpoint, next_action="new checkpoint",
                      unresolved_findings=second.checkpoint.unresolved_findings + ("next",))
    third = db.publish_run_checkpoint(first.run_id, owner_token=second.owner_token,
        expected_generation=second.generation, expected_source_digest=updated.source_digest,
        checkpoint=updated)
    initial = logical_bytes(db)
    fourth = refresh(db, third)
    assert logical_bytes(db) - initial <= 8192
    assert db.read_run_checkpoint(first.run_id, generation=1).checkpoint == first.checkpoint
    assert fourth.checkpoint == updated
    assert db.read_run_custody(first.run_id) == fourth


def republish_raw(db, generation, document):
    raw = json.dumps(document, sort_keys=True, separators=(",", ":"))
    db.set_meta(generation_key("compact-test", generation), raw)
    db.set_meta("run-custody:compact-test:head", json.dumps({"generation": generation, "digest": digest(raw)}))


@pytest.mark.parametrize("fault", ["cycle", "wrong-digest", "ownership-change", "unknown-field"])
def test_well_hashed_invalid_compact_record_is_refused(db, fault):
    second = refresh(db, claim(db))
    record = json.loads(db.get_meta(generation_key(second.run_id, second.generation)))
    assert record["schema"] == "SessionDBRunCustodyRefreshV1"
    if fault == "cycle":
        record["checkpoint_reference"]["generation"] = second.generation
    elif fault == "wrong-digest":
        record["checkpoint_reference"]["digest"] = "0" * 64
    elif fault == "ownership-change":
        record["custody"]["current_session_id"] = "substituted"
    else:
        record["unrecognized"] = True
    republish_raw(db, second.generation, record)
    with pytest.raises(RunCustodyError, match="INTEGRITY"):
        db.read_run_custody(second.run_id)


def test_checkpoint_reference_cannot_target_a_compact_predecessor(db):
    second = refresh(db, claim(db))
    third = refresh(db, second)
    second_raw = db.get_meta(generation_key(second.run_id, second.generation))
    record = json.loads(db.get_meta(generation_key(third.run_id, third.generation)))
    record["checkpoint_reference"] = {"generation": second.generation, "digest": digest(second_raw)}
    republish_raw(db, third.generation, record)
    with pytest.raises(RunCustodyError, match="INTEGRITY_SCHEMA"):
        db.read_run_custody(third.run_id)


def test_referenced_checkpoint_corruption_is_detected_after_many_refreshes(db):
    owner = claim(db)
    for _ in range(3):
        owner = refresh(db, owner)
    key = generation_key(owner.run_id, 1)
    raw = json.loads(db.get_meta(key))
    raw["checkpoint"]["next_action"] = "tampered"
    db.set_meta(key, json.dumps(raw))
    with pytest.raises(RunCustodyError, match="INTEGRITY"):
        db.read_run_custody(owner.run_id)


def test_reference_is_rechecked_in_write_transaction_and_fault_rolls_back(db, monkeypatch):
    first = claim(db)
    key = generation_key(first.run_id, 1)
    original = db.get_meta(key)
    execute = db._execute_write

    def with_fault(fn, *args, **kwargs):
        def fault(conn):
            conn.execute("UPDATE state_meta SET value=? WHERE key=?", ("{}", key))
            return fn(conn)
        return execute(fault, *args, **kwargs)

    monkeypatch.setattr(db, "_execute_write", with_fault)
    with pytest.raises(RunCustodyError, match="INTEGRITY"):
        refresh(db, first)
    assert db.get_meta(key) == original
    assert db.get_meta(generation_key(first.run_id, 2)) is None
    assert db.read_run_custody(first.run_id) == first


def test_compact_refresh_reopens_without_rewriting_v1_or_compact_rows(db):
    first = claim(db)
    current = refresh(db, refresh(db, first))
    # Writable SessionDB reopen may initialize its independent FTS version key.
    # This contract protects all native rows of this run, not unrelated owners.
    before = db._conn.execute("SELECT key,value FROM state_meta WHERE key LIKE 'run-custody:compact-test:%' ORDER BY key").fetchall()
    assert before
    with SessionDB(db_path=db.db_path) as reopened:
        assert reopened.read_run_custody(first.run_id) == current
        assert reopened.read_run_checkpoint(first.run_id, generation=1) == first
    assert db._conn.execute("SELECT key,value FROM state_meta WHERE key LIKE 'run-custody:compact-test:%' ORDER BY key").fetchall() == before


def test_concurrent_refresh_has_one_fenced_winner(db):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    first = claim(db)
    barrier = threading.Barrier(2)

    def compete():
        other = SessionDB(db_path=db.db_path)
        try:
            barrier.wait(timeout=10)
            try:
                return refresh(other, first).generation
            except RunCustodyError as exc:
                return exc.code
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: compete(), range(2)))
    assert results.count(2) == 1
    assert results.count("FENCE_MISMATCH") == 1
    assert db.read_run_custody(first.run_id).generation == 2


def test_compact_refresh_release_and_fenced_reclaim(db):
    first = claim(db)
    second = refresh(db, first)
    with pytest.raises(RunCustodyError, match="FENCE_MISMATCH"):
        refresh(db, first)
    released = db.release_run_custody(second.run_id, owner_token=second.owner_token,
                                      expected_generation=second.generation)
    successor = db.claim_run_custody(second.run_id, expected_generation=released.generation,
        checkpoint=released.checkpoint, origin_session_id=released.origin_session_id,
        current_session_id="successor", historical_goal_digest=released.historical_goal_digest)
    assert successor.owner_token != first.owner_token
    assert db.read_run_checkpoint(first.run_id, generation=2) == second
