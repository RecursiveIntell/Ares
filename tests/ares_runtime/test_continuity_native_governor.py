"""Paired Ares/native Governor checks on disposable, explicitly isolated state."""
import os
from pathlib import Path

import pytest

from hermes_state import SessionDB
from plugins.context_engine._context_governor import ContextGovernorEngine
from plugins.context_engine._context_governor.key_state import ContextGovernorKeyState
from tests.ares_runtime.test_context_rebase_state import _publish


@pytest.fixture
def native(tmp_path, monkeypatch):
    binary = os.environ.get("HERMES_TEST_CONTEXT_GOVERNOR_BIN")
    if not binary:
        pytest.skip("exact native binary must be supplied by the paired qualification job")
    assert Path(binary).is_file(), "configured native binary is missing"
    home = tmp_path / "isolated-profile"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    ContextGovernorKeyState(home, binary).initialize_first_install().close()
    engine = ContextGovernorEngine(binary=binary, store_dir=str(home / "context-governor/store"))
    engine.store_dir.mkdir(mode=0o700)
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s0", source="cli", profile_name="isolated")
    db.append_message("s0", "user", "Preserve the original requirement")
    assert db.try_acquire_session_turn_lease("s0", "holder", ttl_seconds=300)
    engine.on_session_start("s0", session_db=db)
    try:
        yield engine, db
    finally:
        db.close()


def compact(engine):
    return engine._run_certified_json(["compact-v2", "--dir", str(engine.store_dir)], {
        "session_id": "s0", "messages": [
            {"role": "user", "content": "Keep the fixture requirement"},
            {"role": "tool", "content": "old " * 1000 + "EXACT_NATIVE_REBASE_MARKER" + " tail" * 1000},
            {"role": "assistant", "content": "Inspected"},
            {"role": "user", "content": "Continue"},
        ], "policy": {"target_tokens": 180, "protect_first_n": 0, "protect_last_n": 1,
                      "summary_max_chars": 8000, "allocator": "deterministic_v1"},
    })


def test_real_pending_receipt_blocks_rebase_acknowledgment(native):
    engine, db = native
    response = compact(engine)
    engine._run_certified_json(["prepare-v2", "--dir", str(engine.store_dir)], response)
    _publish(db)
    engine.on_session_start("s1", session_db=db, boundary_reason="context_rebase", old_session_id="s0")
    with pytest.raises(RuntimeError, match="GOVERNOR_PENDING_RECONCILIATION_REQUIRED"):
        engine.validate_context_rebase_binding(session_db=db, session_id="s1")
    assert db.read_context_rebase_transition("tx1").state == "committed_pending_activation"


def test_real_activated_receipt_remains_exactly_recoverable_after_rebase(native):
    engine, db = native
    response = compact(engine)
    receipt_id = response["receipt"]["receipt_id"]
    source_id = next(item["source_id"] for item in response["source_evidence"]
                     if "EXACT_NATIVE_REBASE_MARKER" in item["message"]["content"])
    engine._run_certified_json(["prepare-v2", "--dir", str(engine.store_dir)], response)
    engine._run_certified_json(["activate-v2", "--dir", str(engine.store_dir)], {
        "receipt_id": receipt_id, "committed_messages": response["compacted_messages"],
    })
    _publish(db)
    engine.on_session_start("s1", session_db=db, boundary_reason="context_rebase", old_session_id="s0")
    engine.validate_context_rebase_binding(session_db=db, session_id="s1")
    assert engine._lineage_session_id == "s0"
    recovered = engine._run_certified_json([
        "expand", "--dir", str(engine.store_dir), "--receipt", receipt_id, "--item", source_id,
    ], {})
    assert "EXACT_NATIVE_REBASE_MARKER" in str(recovered)


def test_real_key_failure_cannot_become_owner_acknowledgment(native):
    engine, db = native
    _publish(db)
    engine.on_session_start("s1", session_db=db, boundary_reason="context_rebase", old_session_id="s0")
    current = Path(engine._key_state.hermes_home) / "context-governor/keys/current.json"
    current.unlink()
    with pytest.raises(Exception, match="MissingGovernedKey"):
        engine.validate_context_rebase_binding(session_db=db, session_id="s1")
