"""Closed lineage contracts for host-managed model calls.

This module owns the nonauthorizing call-context envelope used to bind a
conversation turn to the owner-issued policy, graph, memory, route, source,
and compaction references. It carries references and digests only; it does not
authorize provider egress or own execution state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .collaboration import (
    ContractError,
    ImmutableArtifact,
    _check_ref,
    _finalize,
    _is_algorithm_digest,
    _strict_payload,
)
from .governed_context import MemoryRequirement


MANAGED_CALL_CONTEXT_SCHEMA = "ares.managed-call-context/v1"
_MANAGED_CALL_CONTEXT_FIELDS = {
    "schema_version",
    "call_ref",
    "parent_call_ref",
    "session_ref",
    "branch_ref",
    "turn_ref",
    "transcript_generation",
    "surface",
    "purpose",
    "inbound_event_ref",
    "inbound_event_digest",
    "current_instruction_ref",
    "current_instruction_digest",
    "transcript_snapshot_ref",
    "transcript_snapshot_digest",
    "compaction_state",
    "compaction_receipt_ref",
    "compaction_receipt_digest",
    "policy_basis_ref",
    "policy_basis_digest",
    "graph_obligation_ref",
    "graph_obligation_digest",
    "memory_requirement",
    "authorized_memory_namespaces",
    "route_resolution_ref",
    "route_resolution_digest",
    "source_revision",
    "authority_origin",
    "derived_authority_ref",
    "derived_authority_digest",
    "context_digest",
}
_MANAGED_CALL_CONTEXT_REQUIRED = _MANAGED_CALL_CONTEXT_FIELDS - {"context_digest"}
_MANAGED_CALL_CONTEXT_PURPOSES = {
    "conversation",
    "planning",
    "specialist",
    "peer_review",
    "synthesis",
    "final_verification",
    "repair",
    "model_routing",
    "background_summary",
}


def _validate_managed_call_context(value: Mapping[str, Any]) -> None:
    if value.get("schema_version") != MANAGED_CALL_CONTEXT_SCHEMA:
        raise ContractError("UNSUPPORTED_MANAGED_CALL_CONTEXT")
    for name in (
        "call_ref",
        "session_ref",
        "branch_ref",
        "turn_ref",
        "surface",
        "purpose",
        "inbound_event_ref",
        "current_instruction_ref",
        "transcript_snapshot_ref",
        "policy_basis_ref",
        "graph_obligation_ref",
        "route_resolution_ref",
        "source_revision",
        "authority_origin",
    ):
        _check_ref(value.get(name), name)
    for name in (
        "inbound_event_digest",
        "current_instruction_digest",
        "transcript_snapshot_digest",
        "policy_basis_digest",
        "graph_obligation_digest",
        "route_resolution_digest",
    ):
        if not _is_algorithm_digest(value.get(name)):
            raise ContractError("INVALID_DIGEST", name)
    parent = value.get("parent_call_ref")
    if parent is not None:
        _check_ref(parent, "parent_call_ref")
    generation = value.get("transcript_generation")
    if type(generation) is not int or generation < 0:
        raise ContractError("INVALID_TRANSCRIPT_GENERATION")
    purpose = value.get("purpose")
    if purpose not in _MANAGED_CALL_CONTEXT_PURPOSES:
        raise ContractError("UNSUPPORTED_CALL_PURPOSE")
    if value.get("surface") == "normal_chat" and purpose != "conversation":
        raise ContractError("NORMAL_CHAT_PURPOSE_MISMATCH")

    compaction_state = value.get("compaction_state")
    compaction_ref = value.get("compaction_receipt_ref")
    compaction_digest = value.get("compaction_receipt_digest")
    if compaction_state not in {"not_compacted", "committed"}:
        raise ContractError("INVALID_COMPACTION_STATE")
    if compaction_state == "not_compacted":
        if compaction_ref is not None or compaction_digest is not None:
            raise ContractError("UNCOMMITTED_COMPACTION")
    else:
        if compaction_ref is None or not _is_algorithm_digest(compaction_digest):
            raise ContractError("COMMITTED_COMPACTION_MISSING")
        _check_ref(compaction_ref, "compaction_receipt_ref")

    memory_requirement = value.get("memory_requirement")
    if memory_requirement not in {item.value for item in MemoryRequirement}:
        raise ContractError("INVALID_MEMORY_REQUIREMENT")
    namespaces = value.get("authorized_memory_namespaces")
    if not isinstance(namespaces, list) or not all(
        isinstance(item, str) for item in namespaces
    ):
        raise ContractError("INVALID_MEMORY_SCOPE")
    for namespace in namespaces:
        _check_ref(namespace, "authorized_memory_namespace")
    if memory_requirement == MemoryRequirement.NOT_REQUIRED.value and namespaces:
        raise ContractError("MEMORY_SCOPE_WITHOUT_REQUIREMENT")
    if memory_requirement != MemoryRequirement.NOT_REQUIRED.value and not namespaces:
        raise ContractError("MEMORY_AUTHORITY_MISSING")

    derived = value.get("derived_authority_ref")
    if derived is not None:
        _check_ref(derived, "derived_authority_ref")
    derived_digest = value.get("derived_authority_digest")
    if derived_digest is not None and not _is_algorithm_digest(derived_digest):
        raise ContractError("INVALID_DIGEST", "derived_authority_digest")


@dataclass(frozen=True)
class ManagedCallContextV1(ImmutableArtifact):
    """Closed, nonauthorizing lineage for one host-managed model call."""

    @classmethod
    def create(cls, values: Mapping[str, Any]) -> "ManagedCallContextV1":
        value = dict(values)
        value.setdefault("schema_version", MANAGED_CALL_CONTEXT_SCHEMA)
        value.pop("context_digest", None)
        value = _finalize(value, "context_digest")
        _strict_payload(
            value,
            _MANAGED_CALL_CONTEXT_REQUIRED | {"context_digest"},
            _MANAGED_CALL_CONTEXT_FIELDS,
            "context_digest",
        )
        _validate_managed_call_context(value)
        return cls(value, "context_digest")

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> "ManagedCallContextV1":
        value = _strict_payload(
            raw,
            _MANAGED_CALL_CONTEXT_REQUIRED,
            _MANAGED_CALL_CONTEXT_FIELDS,
            "context_digest",
        )
        full = {**value, "context_digest": raw["context_digest"]}
        _validate_managed_call_context(full)
        return cls(full, "context_digest")


__all__ = ["MANAGED_CALL_CONTEXT_SCHEMA", "ManagedCallContextV1"]
