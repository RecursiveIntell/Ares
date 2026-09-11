import pytest

from ares_runtime.governed_context import ContractError
from ares_runtime.managed_calls import ManagedCallContextV1


def _context_values():
    return {
        "schema_version": "ares.managed-call-context/v1",
        "call_ref": "call:root-1",
        "parent_call_ref": None,
        "session_ref": "session:test-1",
        "branch_ref": "branch:main",
        "turn_ref": "turn:1",
        "transcript_generation": 3,
        "surface": "normal_chat",
        "purpose": "conversation",
        "inbound_event_ref": "event:test-1",
        "inbound_event_digest": "sha256:" + "a" * 64,
        "current_instruction_ref": "instruction:test-1",
        "current_instruction_digest": "sha256:" + "b" * 64,
        "transcript_snapshot_ref": "transcript:test-1",
        "transcript_snapshot_digest": "sha256:" + "c" * 64,
        "compaction_state": "not_compacted",
        "compaction_receipt_ref": None,
        "compaction_receipt_digest": None,
        "policy_basis_ref": "policy:test-1",
        "policy_basis_digest": "blake3:" + "d" * 64,
        "graph_obligation_ref": "graph-obligation:test-1",
        "graph_obligation_digest": "blake3:" + "e" * 64,
        "memory_requirement": "not_required",
        "authorized_memory_namespaces": [],
        "route_resolution_ref": "route:test-1",
        "route_resolution_digest": "sha256:" + "f" * 64,
        "source_revision": "source:test-1",
        "authority_origin": "host:session-owner",
        "derived_authority_ref": None,
        "derived_authority_digest": None,
    }


def test_managed_call_context_is_closed_and_digest_bound():
    context = ManagedCallContextV1.create(_context_values())
    assert context.to_dict()["schema_version"] == "ares.managed-call-context/v1"
    assert context.to_dict()["purpose"] == "conversation"
    assert context.to_dict()["compaction_state"] == "not_compacted"
    assert ManagedCallContextV1.parse(context.to_dict()).to_dict() == context.to_dict()


def test_managed_call_context_rejects_unknown_or_missing_fields():
    values = _context_values()
    values["unexpected"] = True
    with pytest.raises(ContractError):
        ManagedCallContextV1.create(values)

    values = _context_values()
    values.pop("policy_basis_ref")
    with pytest.raises(ContractError):
        ManagedCallContextV1.create(values)


def test_managed_call_context_rejects_digest_and_purpose_drift():
    context = ManagedCallContextV1.create(_context_values())
    tampered = context.to_dict()
    tampered["purpose"] = "planning"
    with pytest.raises(ContractError):
        ManagedCallContextV1.parse(tampered)

    tampered = context.to_dict()
    tampered["transcript_generation"] = 4
    with pytest.raises(ContractError):
        ManagedCallContextV1.parse(tampered)


def test_managed_call_context_requires_explicit_compaction_and_memory_state():
    values = _context_values()
    values["compaction_state"] = "pending"
    with pytest.raises(ContractError):
        ManagedCallContextV1.create(values)

    values = _context_values()
    values["memory_requirement"] = "not_required"
    values["authorized_memory_namespaces"] = ["namespace:should-not-be-used"]
    with pytest.raises(ContractError):
        ManagedCallContextV1.create(values)

    values = _context_values()
    values["memory_requirement"] = "required"
    values["authorized_memory_namespaces"] = []
    with pytest.raises(ContractError):
        ManagedCallContextV1.create(values)

    values["authorized_memory_namespaces"] = ["namespace:memory"]
    context = ManagedCallContextV1.create(values)
    assert context.to_dict()["memory_requirement"] == "required"
    assert context.to_dict()["authorized_memory_namespaces"] == ["namespace:memory"]
