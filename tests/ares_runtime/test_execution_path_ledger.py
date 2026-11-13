"""Structural ledger for the execution-path surfaces the plan requires.

This is intentionally a shape/ownership check, not a duplicate behavioural
suite: removing or renaming a required surface must fail here even if the
happy-path tests are updated elsewhere.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from ares_runtime.execution_record import FIELDS, ExecutionRecordV1
from ares_runtime.managed_calls import produce_dispatch_materialization
from hermes_state import SessionDB
from plugins.dispatch_governor import DispatchGovernor

ROOT = Path(__file__).resolve().parents[2]


def test_record_exposes_the_required_lineage_fields():
    for field in (
        "execution_id",
        "admission_ref",
        "materialization_ref",
        "logical_request_digest",
        "wire_request_digest",
        "wire_bytes",
        "governor_qualified",
        "governor_mode",
        "dispatch_intent_refs",
    ):
        assert field in FIELDS
    assert hasattr(ExecutionRecordV1, "transition")
    assert hasattr(ExecutionRecordV1, "evolve")


def test_session_owner_exposes_the_record_and_recovery_methods():
    assert (
        "execution_id"
        in inspect.signature(SessionDB.record_model_dispatch_materialization).parameters
    )
    for name in (
        "create_execution_record",
        "transition_execution_record",
        "read_execution_record",
        "append_dispatch_intent",
        "reconcile_execution_record",
        "abandon_unsent_context_dispatch",
        "prune_execution_events",
    ):
        assert hasattr(SessionDB, name), name


def test_producer_and_governor_surfaces_exist():
    assert (
        "execution_id" in inspect.signature(produce_dispatch_materialization).parameters
    )
    assert (ROOT / "plugins" / "dispatch_governor" / "plugin.yaml").exists()
    assert (ROOT / "plugins" / "dispatch_governor" / "__init__.py").exists()
    assert (ROOT / "scripts" / "verify_execution_record.py").exists()
    assert DispatchGovernor(mode="observe") is not None
