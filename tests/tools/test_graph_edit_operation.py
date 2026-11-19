"""EDIT card (P4): bounded edit with interruption reconciliation.

F3 fixture: one expected preimage + one intended diff bound to a stable
operation ID. Interrupt after applying but before the outward receipt;
recover by querying the operation owner and comparing actual artifacts.
"""
import hashlib
import json
from pathlib import Path

import pytest

from tools.delegate_tool import (
    run_graph_edit_operation,
    query_graph_edit_operation,
)

# Repo-local copy of the frozen F3 preimage (byte-identical to the kit fixture,
# sha256 0d660a528c998d6eaed8ef757445be4e5137219f7a86f7d1d72e8f9b008a0998).
# Tests must not depend on an operator-home path.
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "graph_edit"


def _parent():
    from types import SimpleNamespace
    return SimpleNamespace(session_id="edit-owner", enabled_toolsets=["file"], _delegate_depth=0)


@pytest.fixture
def workdir(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    pre = (FIXTURES / "F3_edit_preimage.txt").read_bytes()
    (work / "fixture.txt").write_bytes(pre)
    return work


def _preimage_sha():
    return hashlib.sha256((FIXTURES / "F3_edit_preimage.txt").read_bytes()).hexdigest()


def _intended():
    # The intended postimage: apply the frozen one-line change.
    pre = (FIXTURES / "F3_edit_preimage.txt").read_text()
    return pre.replace("beta two\n", "beta TWO edited\n")


def test_exact_intended_diff_applied_once(workdir):
    result = run_graph_edit_operation(
        operation_id="op-edit-0001", workspace=workdir, path="fixture.txt",
        expected_preimage_sha256=_preimage_sha(),
        intended_postimage=_intended(),
        parent_agent=_parent(),
    )
    assert result["status"] == "completed"
    assert result["effect_count"] == 1
    actual = (workdir / "fixture.txt").read_text()
    assert actual == _intended()
    assert result["postimage_sha256"] == hashlib.sha256(actual.encode()).hexdigest()


def test_wrong_preimage_refuses_without_write(workdir):
    before = (workdir / "fixture.txt").read_bytes()
    result = run_graph_edit_operation(
        operation_id="op-edit-wrong", workspace=workdir, path="fixture.txt",
        expected_preimage_sha256="0" * 64,
        intended_postimage=_intended(),
        parent_agent=_parent(),
    )
    assert result["status"] == "blocked"
    assert result["code"] == "edit_preimage_mismatch"
    assert (workdir / "fixture.txt").read_bytes() == before


def test_interrupt_after_apply_before_receipt_reconciles_without_replay(workdir):
    # First attempt: apply, then "crash" before the outward receipt.
    result = run_graph_edit_operation(
        operation_id="op-edit-crash", workspace=workdir, path="fixture.txt",
        expected_preimage_sha256=_preimage_sha(),
        intended_postimage=_intended(),
        parent_agent=_parent(),
        crash_before_receipt=True,
    )
    assert result["status"] == "unknown"
    assert result["exit_state"] == "interrupted_before_receipt"
    # The file WAS edited (effect happened).
    assert (workdir / "fixture.txt").read_text() == _intended()

    # Recovery: query the operation owner, compare actual vs expected.
    recovered = query_graph_edit_operation("op-edit-crash", workspace=workdir)
    assert recovered["status"] == "completed"
    assert recovered["reconciled"] is True
    assert recovered["effect_count"] == 1

    # A repeated notification / retry cannot cause another write.
    again = run_graph_edit_operation(
        operation_id="op-edit-crash", workspace=workdir, path="fixture.txt",
        expected_preimage_sha256=_preimage_sha(),
        intended_postimage=_intended(),
        parent_agent=_parent(),
    )
    assert again["effect_count"] == 1
    assert again["status"] == "completed"
    assert (workdir / "fixture.txt").read_text() == _intended()


def test_conflicting_actual_state_is_quarantined_not_reapplied(workdir):
    run_graph_edit_operation(
        operation_id="op-edit-conflict", workspace=workdir, path="fixture.txt",
        expected_preimage_sha256=_preimage_sha(),
        intended_postimage=_intended(),
        parent_agent=_parent(),
        crash_before_receipt=True,
    )
    # Someone else changed the file after the crash.
    (workdir / "fixture.txt").write_text("third-party change\n")
    recovered = query_graph_edit_operation("op-edit-conflict", workspace=workdir)
    assert recovered["status"] == "unknown"
    assert recovered["quarantined"] is True
    # Retry refuses; it cannot blindly apply again.
    retry = run_graph_edit_operation(
        operation_id="op-edit-conflict", workspace=workdir, path="fixture.txt",
        expected_preimage_sha256=_preimage_sha(),
        intended_postimage=_intended(),
        parent_agent=_parent(),
    )
    assert retry["status"] == "blocked"
    assert retry["code"] == "edit_outcome_quarantined"
    assert (workdir / "fixture.txt").read_text() == "third-party change\n"


def test_path_outside_workspace_refused(workdir, tmp_path):
    result = run_graph_edit_operation(
        operation_id="op-edit-escape", workspace=workdir, path="../outside.txt",
        expected_preimage_sha256=_preimage_sha(),
        intended_postimage="x",
        parent_agent=_parent(),
    )
    assert result["status"] == "blocked"
    assert result["code"] == "edit_path_out_of_scope"


def test_missing_owner_refused(workdir):
    result = run_graph_edit_operation(
        operation_id="op-edit-noowner", workspace=workdir, path="fixture.txt",
        expected_preimage_sha256=_preimage_sha(),
        intended_postimage=_intended(),
        parent_agent=None,
    )
    assert result["status"] == "blocked"
    assert result["code"] == "policy_owner_required"
