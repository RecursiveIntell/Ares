"""Local dispatch task binding uses canonical SessionDB owners, not labels."""
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_state import SessionDB
from hermes_state_continuity import ContextContinuationError
from hermes_state_runs import RunCheckpoint, RunCustodyError
from tests.ares_runtime.test_context_authority_binding import (  # noqa: F401
    db, native, enroll, sign,
)


@pytest.fixture
def owner(tmp_path):
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("s", source="cli", profile_name="p")
        row = db.append_message("s", "user", "Do the bounded work", timestamp=1.0)
        assert db.try_acquire_session_turn_lease("s", "holder", ttl_seconds=300)
        yield db, row


def claim(db, row, run_id="run-one"):
    binding, control = db.read_run_task_basis(run_id=run_id,
        origin_session_id="s", current_session_id="s", input_row_id=row)
    checkpoint = RunCheckpoint("a" * 64, "b" * 64, "c" * 64,
        "Inspect", (), (), (), ())
    return db.claim_run_task_custody_checked(run_id, lease_holder="holder",
        expected_generation=0, checkpoint=checkpoint, task_binding=binding,
        current_session_id="s", expected_control_digest=control)


def admit(db):
    snapshot = db.read_context_rebase_snapshot("s")
    result = db.admit_context_dispatch("s", turn_lease_holder="holder", attempt_id="a",
        expected_snapshot_digest=snapshot.digest, payload_digest="sha256:" + "d" * 64,
        route_ref="local-test")
    db.settle_context_dispatch_response("a", turn_lease_holder="holder")
    return result


def assert_current(db):
    db.assert_context_dispatch_control_current("a", session_id="s", turn_lease_holder="holder")


@pytest.mark.parametrize("mutation", ["claim", "refresh", "release", "checkpoint", "profile"])
def test_settled_response_refuses_changed_canonical_task_owner(owner, mutation):
    db, row = owner
    run = None if mutation in {"claim", "profile"} else claim(db, row)
    admit(db)
    assert_current(db)
    # An independent SessionDB connection is the writer. No process-local
    # cache or mock callback can supply the current owner state.
    with SessionDB(db.db_path) as writer:
        if mutation == "claim":
            claim(writer, row)
        elif mutation == "profile":
            writer._execute_write(lambda conn: conn.execute(
                "UPDATE sessions SET profile_name='other' WHERE id='s'"))
        else:
            args = dict(owner_token=run.owner_token, expected_generation=run.generation)
            if mutation == "refresh":
                writer.refresh_run_custody(run.run_id, **args)
            elif mutation == "release":
                writer.release_run_custody(run.run_id, **args)
            else:
                writer.publish_run_checkpoint(run.run_id, **args,
                    expected_source_digest=run.checkpoint.source_digest,
                    checkpoint=replace(run.checkpoint, next_action="Review"))
    with pytest.raises(ContextContinuationError, match="CONTEXT_TOOL_TASK_SUPERSEDED"):
        assert_current(db)


def test_task_binding_survives_assistant_rows_and_reopen_without_token_disclosure(owner):
    db, row = owner
    run = claim(db, row)
    record = admit(db)
    snapshot = db.read_context_rebase_snapshot("s")
    assert record["task_binding_digest"] == snapshot.dispatch_task_digest
    serialized = json.dumps(snapshot.run_task_bindings)
    assert run.owner_token not in serialized
    assert "owner_token" not in serialized
    assert snapshot.run_task_bindings[0]["input_row_id"] == row
    db.append_message("s", "assistant", "Checking", turn_lease_holder="holder")
    assert_current(db)
    with SessionDB(db.db_path) as reopened:
        assert_current(reopened)
        assert reopened.read_context_rebase_snapshot("s").dispatch_task_digest == record["task_binding_digest"]


@pytest.mark.parametrize("column,value,code", [
    ("content", "substituted", "TASK_BINDING_MISMATCH"),
    ("active", 0, "TASK_INPUT_MISSING"),
    ("display_kind", "hidden", "TASK_AUTHENTIC_INPUT_REQUIRED"),
])
def test_archived_task_provenance_is_revalidated_at_tool_boundary(owner, column, value, code):
    db, row = owner
    run = claim(db, row)
    db.release_run_custody(run.run_id, owner_token=run.owner_token, expected_generation=run.generation)
    db.append_message("s", "user", "Keep the task scope", turn_lease_holder="holder")
    admit(db)
    db._execute_write(lambda conn: conn.execute(f"UPDATE messages SET {column}=? WHERE id=?", (value, row)))
    # Keep the session nonempty for the inactive-anchor case.
    db.append_message("s", "assistant", "Observation", turn_lease_holder="holder")
    with pytest.raises(RunCustodyError, match=code):
        db.read_context_rebase_snapshot("s")
    with pytest.raises(RunCustodyError, match=code):
        assert_current(db)


def test_missing_task_binding_in_old_receipt_never_reauthorizes_tools(owner):
    db, _ = owner
    record = admit(db)
    record.pop("task_binding_digest", None)
    db.set_meta("context-dispatch:a", json.dumps(record))
    with pytest.raises(ContextContinuationError, match="CONTEXT_TOOL_TASK_SUPERSEDED"):
        assert_current(db)


def test_recovery_control_basis_does_not_self_invalidate_after_custody_refresh(owner):
    db, row = owner
    run = claim(db, row)
    before = db.read_context_rebase_snapshot("s")
    db.refresh_run_custody(run.run_id, owner_token=run.owner_token, expected_generation=run.generation)
    after = db.read_context_rebase_snapshot("s")
    assert after.action_control_digest == before.action_control_digest
    assert after.dispatch_task_digest != before.dispatch_task_digest


@pytest.mark.linux_only
def test_native_signer_rechecks_task_after_prepare_before_signature(db, native, monkeypatch):
    from ares_runtime.collaboration import DaemonPermitReceiptAdapter
    registration = enroll(db)
    admit(db)
    original = native[2]
    row = db.read_context_rebase_snapshot("s").authentic_users[0]["row_id"]

    def prepare_then_change(adapter, kind, **fields):
        result = original(adapter, kind, **fields)
        if kind == "context_call_prepare":
            with SessionDB(db.db_path) as writer:
                claim(writer, row)
        return result

    monkeypatch.setattr(DaemonPermitReceiptAdapter, "context_request", prepare_then_change)
    with pytest.raises(ContextContinuationError, match="CONTEXT_TOOL_TASK_SUPERSEDED"):
        sign(db, registration)


def test_actual_tool_registry_denies_stale_task_before_effect(owner):
    import model_tools
    from ares_runtime.continuity.runtime import context_tool_control_scope
    db, row = owner
    admit(db)
    agent = SimpleNamespace(session_id="s", _session_db=db, context_rebase_enabled=True,
        _context_response_admission={"attempt_id": "a"}, _active_session_turn_lease_holder="holder",
        _context_response_native_authority=None)
    claim(db, row)
    with (context_tool_control_scope(agent),
          patch("agent.context_input.validate_turn_input_authority"),
          patch.object(model_tools.registry, "dispatch", return_value='{"ok":true}') as effect):
        result = model_tools.handle_function_call("write_file", {"path": "/tmp/not-written", "content": "no"},
            session_id="s", task_id="display-label-not-authority")
    assert "CONTEXT_TOOL_TASK_SUPERSEDED" in result
    effect.assert_not_called()
