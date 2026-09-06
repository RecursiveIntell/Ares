"""Governed final context materialization for candidate managed Ares calls.

`ContextPacketV1` remains a reference manifest.  This module resolves the
manifest under current policy, consumes only witnessed semantic-memory owner
responses, renders the exact native provider request, accounts its real bytes
and tokens, and seals a distinct materialization receipt before egress.

The module performs no provider call and owns no durable memory.  Its adapters
are fail-closed projections over the profile-runtime, semantic-memory, Agent
Graph, and Recursive Agent owners.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .collaboration import (
    ContractError,
    ContextMaterializer,
    ContextPacketV1,
    ImmutableArtifact,
    ResolvedPolicyBasisV1,
    SealedInvocationV1,
    _check_ref,
    _finalize,
    _freeze,
    _is_algorithm_digest,
    _parse_utc,
    _strict_payload,
    _thaw,
    canonical_json,
    digest,
)

SEMANTIC_MEMORY_OWNER = "semantic-memory"
SEMANTIC_MEMORY_WITNESSED_TOOL = "sm_search_governed_witnessed"
MATERIALIZER_VERSION = "ares-governed-context-materializer-1"
_RENDER_SCHEMA = "ares.managed-context-render/v1"
_MEMORY_SCHEMA = "ares.semantic-memory-observation/v1"
_MATERIALIZATION_SCHEMA = "ares.context-materialization/v1"
_LEGACY_PLAN_SCHEMA = "ares.legacy-memory-import-plan/v1"
_MEMORY_OWNER_CAPABILITY = object()


class ManagedCallKind(str, Enum):
    PLANNING = "planning"
    SPECIALIST = "specialist"
    PEER_REVIEW = "peer_review"
    SYNTHESIS = "synthesis"
    FINAL_VERIFICATION = "final_verification"
    REPAIR = "repair"
    MODEL_ROUTING = "model_routing"
    BACKGROUND_SUMMARY = "background_summary"


MANAGED_MODEL_CALL_KINDS = tuple(ManagedCallKind)


class MemoryRequirement(str, Enum):
    NOT_REQUIRED = "not_required"
    OPTIONAL = "optional"
    REQUIRED = "required"


class MemoryResolutionState(str, Enum):
    NOT_REQUIRED = "not_required"
    APPLIED = "applied"
    NO_MATCH = "no_match"
    UNAVAILABLE = "unavailable"
    STALE = "stale_or_degraded"
    CONFLICT = "conflict"
    BUDGET_LIMITED = "budget_limited"
    FORBIDDEN = "forbidden"


_MEMORY_FIELDS = {
    "schema_version",
    "owner",
    "owner_tool",
    "state",
    "requirement",
    "request_id",
    "receipt_ref",
    "authority_snapshot_ref",
    "retrieval_epoch",
    "query_digest",
    "filter_digest",
    "config_digest",
    "result_refs",
    "result_digests",
    "results",
    "degradations",
    "complete_replay_available",
    "observation_digest",
}


@dataclass(frozen=True)
class SemanticMemoryObservationV1(ImmutableArtifact):
    """Exact witnessed semantic-memory result accepted from the live owner.

    Serialized observations are evidence only.  The private capability is set
    only after the owner response and durable receipt readback agree.
    """

    _owner_capability: object | None = None

    @property
    def owner_verified(self) -> bool:
        return self._owner_capability is _MEMORY_OWNER_CAPABILITY

    @classmethod
    def create_live(cls, values: Mapping[str, Any]) -> "SemanticMemoryObservationV1":
        value = dict(values)
        value.setdefault("schema_version", _MEMORY_SCHEMA)
        value.pop("observation_digest", None)
        value = _finalize(value, "observation_digest")
        _strict_payload(value, _MEMORY_FIELDS, _MEMORY_FIELDS, "observation_digest")
        return cls(value, "observation_digest", _MEMORY_OWNER_CAPABILITY)

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> "SemanticMemoryObservationV1":
        value = _strict_payload(
            raw,
            _MEMORY_FIELDS - {"observation_digest"},
            _MEMORY_FIELDS,
            "observation_digest",
        )
        if value.get("schema_version") != _MEMORY_SCHEMA:
            raise ContractError("UNSUPPORTED_MEMORY_OBSERVATION")
        return cls(
            {**value, "observation_digest": raw["observation_digest"]},
            "observation_digest",
        )


class SemanticMemoryWitnessedPort:
    """Narrow adapter to semantic-memory's governed witnessed retrieval.

    There is intentionally no fallback callback. The fixed owner tool performs
    recall-authority filtering before returning content and binds the admitted
    rows plus decisions to a coherent authority epoch. Ares only validates and
    materializes that owner response.
    """

    def __init__(
        self,
        *,
        call_owner_tool: Callable[[str, dict[str, Any]], Mapping[str, Any]],
        resolve_current_state: Callable[[], Mapping[str, Any]],
    ) -> None:
        self._call_owner_tool = call_owner_tool
        self._resolve_current_state = resolve_current_state

    def current_state(self) -> dict[str, Any]:
        try:
            raw = self._resolve_current_state()
        except Exception:
            raise ContractError("MEMORY_OWNER_STATE_UNAVAILABLE") from None
        if not isinstance(raw, Mapping):
            raise ContractError("MEMORY_OWNER_STATE_UNAVAILABLE")
        snapshot = raw.get("snapshot_id")
        epoch = raw.get("retrieval_epoch")
        if not isinstance(snapshot, str) or not snapshot.startswith("epoch:"):
            raise ContractError("MEMORY_OWNER_STATE_MALFORMED")
        if type(epoch) is not int or epoch < 0:
            raise ContractError("MEMORY_OWNER_STATE_MALFORMED")
        return {"snapshot_id": snapshot, "retrieval_epoch": epoch}

    def resolve(
        self,
        *,
        requirement: MemoryRequirement,
        request_id: str,
        query: str,
        top_k: int,
        requested_namespaces: Sequence[str],
        authorized_namespaces: Sequence[str],
        caller: str,
        subject: str,
        audiences: Sequence[str],
    ) -> SemanticMemoryObservationV1:
        if not isinstance(requirement, MemoryRequirement):
            raise ContractError("INVALID_MEMORY_REQUIREMENT")
        if requirement is MemoryRequirement.NOT_REQUIRED:
            raise ContractError("MEMORY_QUERY_NOT_REQUIRED")
        _check_ref(request_id, "memory_request_id")
        _check_ref(caller, "memory_caller")
        _check_ref(subject, "memory_subject")
        if not isinstance(query, str) or not query.strip():
            raise ContractError("MEMORY_QUERY_MISSING")
        if type(top_k) is not int or top_k < 1:
            raise ContractError("INVALID_MEMORY_LIMIT")
        requested = _string_set(
            requested_namespaces, "requested_memory_namespaces", allow_empty=False
        )
        authorized = _string_set(
            authorized_namespaces, "authorized_memory_namespaces", allow_empty=False
        )
        normalized_audiences = _string_set(
            audiences, "memory_audiences", allow_empty=False
        )
        if not set(requested).issubset(authorized):
            raise ContractError("MEMORY_SCOPE_WIDENING")
        if len(requested) != 1:
            raise ContractError("MEMORY_MULTI_NAMESPACE_SPLIT_REQUIRED")
        arguments = {
            "query": query,
            "top_k": top_k,
            "request_id": request_id,
            "caller": caller,
            "subject": subject,
            "audiences": normalized_audiences,
            "scope": {"namespace": requested[0]},
        }
        try:
            raw = self._call_owner_tool(SEMANTIC_MEMORY_WITNESSED_TOOL, arguments)
        except Exception:
            if requirement is MemoryRequirement.REQUIRED:
                raise ContractError("MEMORY_REQUIRED_UNAVAILABLE") from None
            return self._unavailable(requirement, request_id)
        try:
            return self._validate_owner_response(
                raw,
                requirement,
                request_id,
                requested[0],
                top_k,
                caller,
                subject,
                normalized_audiences,
            )
        except ContractError:
            raise
        except Exception:
            if requirement is MemoryRequirement.REQUIRED:
                raise ContractError("MEMORY_REQUIRED_UNAVAILABLE") from None
            return self._unavailable(requirement, request_id)

    def _unavailable(
        self, requirement: MemoryRequirement, request_id: str
    ) -> SemanticMemoryObservationV1:
        return SemanticMemoryObservationV1.create_live({
            "owner": SEMANTIC_MEMORY_OWNER,
            "owner_tool": SEMANTIC_MEMORY_WITNESSED_TOOL,
            "state": MemoryResolutionState.UNAVAILABLE.value,
            "requirement": requirement.value,
            "request_id": request_id,
            "receipt_ref": None,
            "authority_snapshot_ref": None,
            "retrieval_epoch": None,
            "query_digest": None,
            "filter_digest": None,
            "config_digest": None,
            "result_refs": [],
            "result_digests": [],
            "results": [],
            "degradations": ["owner_unavailable"],
            "complete_replay_available": False,
        })

    def _validate_owner_response(
        self,
        raw: Mapping[str, Any],
        requirement: MemoryRequirement,
        request_id: str,
        namespace: str,
        top_k: int,
        caller: str,
        subject: str,
        audiences: Sequence[str],
    ) -> SemanticMemoryObservationV1:
        if not isinstance(raw, Mapping):
            raise ContractError("MEMORY_OWNER_RESPONSE_MALFORMED")
        if (
            raw.get("schema_version") != "governed_witnessed_search_response_v1"
            or raw.get("ok") is not True
        ):
            raise ContractError("MEMORY_OWNER_RESPONSE_MALFORMED")
        if raw.get("state_view") != "Current":
            raise ContractError("MEMORY_OWNER_VIEW_UNSUPPORTED")
        authority = raw.get("authority_state")
        witness = raw.get("retrieval_witness")
        response = raw.get("response")
        if (
            not isinstance(authority, Mapping)
            or not isinstance(witness, Mapping)
            or not isinstance(response, Mapping)
        ):
            raise ContractError("MEMORY_OWNER_RESPONSE_MALFORMED")
        snapshot = authority.get("snapshot_id")
        epoch = authority.get("retrieval_epoch")
        if (
            not isinstance(snapshot, str)
            or not snapshot.startswith("epoch:")
            or type(epoch) is not int
            or epoch < 0
            or witness.get("request_id") != request_id
            or witness.get("authority_snapshot_id") != snapshot
            or witness.get("retrieval_epoch") != epoch
        ):
            raise ContractError("MEMORY_OWNER_STATE_MALFORMED")
        stage_rows = witness.get("stage_outcomes")
        if not isinstance(stage_rows, list):
            raise ContractError("MEMORY_OWNER_RESPONSE_MALFORMED")
        stage_map = {
            row[0]: row[1]
            for row in stage_rows
            if isinstance(row, list) and len(row) == 2 and isinstance(row[0], str)
        }
        if (
            stage_map.get("authority_filter") != "applied"
            or stage_map.get("coherence_recheck") != "applied"
        ):
            raise ContractError("MEMORY_OWNER_AUTHORITY_UNAVAILABLE")
        results = response.get("results")
        decisions = response.get("decisions")
        result_ids = witness.get("ordered_result_ids")
        result_digests = witness.get("ordered_result_digests")
        if not isinstance(results, list):
            raise ContractError("MEMORY_OWNER_RESPONSE_MALFORMED")
        if not isinstance(decisions, list):
            raise ContractError("MEMORY_OWNER_RESPONSE_MALFORMED")
        if not isinstance(result_ids, list):
            raise ContractError("MEMORY_OWNER_RESPONSE_MALFORMED")
        if not isinstance(result_digests, list):
            raise ContractError("MEMORY_OWNER_RESPONSE_MALFORMED")
        if (
            len(results) > top_k
            or len(results) != len(result_ids)
            or len(results) != len(result_digests)
        ):
            raise ContractError("MEMORY_OWNER_RESULT_COUNT_MISMATCH")
        normalized_results: list[dict[str, Any]] = []
        allowed_decisions: list[Mapping[str, Any]] = []
        denied_decisions: list[Mapping[str, Any]] = []
        for decision in decisions:
            if not isinstance(decision, Mapping):
                raise ContractError("MEMORY_OWNER_RESPONSE_MALFORMED")
            if decision.get("allowed") is True:
                allowed_decisions.append(decision)
            else:
                denied_decisions.append(decision)
        for index, result in enumerate(results):
            if not isinstance(result, Mapping):
                raise ContractError("MEMORY_OWNER_RESPONSE_MALFORMED")
            source = result.get("source")
            fact = source.get("fact") if isinstance(source, Mapping) else None
            if not isinstance(fact, Mapping):
                raise ContractError("MEMORY_OWNER_RESULT_MISMATCH")
            bare_id = fact.get("fact_id")
            result_namespace = fact.get("namespace")
            result_id = f"fact:{bare_id}" if isinstance(bare_id, str) else None
            result_digest = _qualified_blake3(result_digests[index])
            decision = next(
                (
                    item
                    for item in allowed_decisions
                    if item.get("fact_id") == bare_id
                    and item.get("purpose") == "recall"
                    and item.get("outcome") == "allow"
                ),
                None,
            )
            if (
                result_id is None
                or result_ids[index] != result_id
                or result_namespace != namespace
                or not isinstance(result.get("content"), str)
                or result_digest is None
                or not isinstance(decision, Mapping)
                or decision.get("caller") != caller
                or decision.get("subject") != subject
                or decision.get("audience") != list(audiences)
                or not isinstance(decision.get("scope"), Mapping)
                or decision["scope"].get("namespace") != namespace
                or not _is_algorithm_digest(decision.get("decision_digest"), "blake3")
                or not _is_algorithm_digest(decision.get("policy_digest"), "blake3")
            ):
                raise ContractError("MEMORY_OWNER_RESULT_MISMATCH")
            normalized_results.append({
                "result_ref": result_id,
                "result_digest": result_digest,
                "namespace": namespace,
                "content": result["content"],
                "source": f"semantic-memory:{result_id}",
                "trust": "owner_authorized_unjudged",
                "state": "current",
            })
        degradations = witness.get("degradations")
        if not isinstance(degradations, list) or not all(
            isinstance(item, str) for item in degradations
        ):
            raise ContractError("MEMORY_OWNER_RESPONSE_MALFORMED")
        state = (
            MemoryResolutionState.NO_MATCH
            if not results
            else MemoryResolutionState.APPLIED
        )
        if not results and denied_decisions:
            state = MemoryResolutionState.FORBIDDEN
        if degradations:
            if requirement is MemoryRequirement.REQUIRED:
                raise ContractError("MEMORY_REQUIRED_DEGRADED")
            state = MemoryResolutionState.STALE
        current = self.current_state()
        if current != {"snapshot_id": snapshot, "retrieval_epoch": epoch}:
            raise ContractError("MEMORY_AUTHORITY_CHANGED")
        query_digest = _qualified_blake3(witness.get("query_digest"))
        config_digest = _qualified_blake3(witness.get("config_digest"))
        if query_digest is None or config_digest is None:
            raise ContractError("MEMORY_OWNER_RESPONSE_MALFORMED")
        policy_digests = sorted({
            str(decision["policy_digest"])
            for decision in [*allowed_decisions, *denied_decisions]
            if _is_algorithm_digest(decision.get("policy_digest"), "blake3")
        })
        filter_digest = policy_digests[0] if len(policy_digests) == 1 else None
        return SemanticMemoryObservationV1.create_live({
            "owner": SEMANTIC_MEMORY_OWNER,
            "owner_tool": SEMANTIC_MEMORY_WITNESSED_TOOL,
            "state": state.value,
            "requirement": requirement.value,
            "request_id": request_id,
            "receipt_ref": f"witness:{request_id}",
            "authority_snapshot_ref": snapshot,
            "retrieval_epoch": epoch,
            "query_digest": query_digest,
            "filter_digest": filter_digest,
            "config_digest": config_digest,
            "result_refs": list(result_ids),
            "result_digests": [_qualified_blake3(item) for item in result_digests],
            "results": normalized_results,
            "degradations": sorted(set(degradations)),
            "complete_replay_available": False,
        })


_MATERIALIZATION_FIELDS = {
    "schema_version",
    "materialization_id",
    "mission_ref",
    "role_contract_ref",
    "call_kind",
    "context_packet_ref",
    "context_packet_digest",
    "policy_basis_ref",
    "policy_basis_digest",
    "source_revision",
    "graph_obligation_ref",
    "graph_obligation_digest",
    "memory",
    "source_slices",
    "included_slices",
    "omissions",
    "mandatory_obligations",
    "required_checks",
    "materializer_ref",
    "template_ref",
    "tokenizer_ref",
    "tokenizer_mode",
    "tool_schema_digest",
    "tool_schema_tokens",
    "tool_schema_bytes",
    "route_class",
    "provider_identity",
    "model_ref",
    "provider_serialization_ref",
    "provider_request_digest",
    "input_tokens",
    "input_bytes",
    "output_reserve",
    "provider_hard_input_tokens",
    "provider_hard_request_bytes",
    "policy_input_cap",
    "policy_output_cap",
    "replay_disposition",
    "materialized_at",
    "materialization_digest",
}


class MaterializedContextV1(ImmutableArtifact):
    @classmethod
    def create(cls, values: Mapping[str, Any]) -> "MaterializedContextV1":
        value = dict(values)
        value.setdefault("schema_version", _MATERIALIZATION_SCHEMA)
        value.pop("materialization_digest", None)
        value = _finalize(value, "materialization_digest")
        _strict_payload(
            value,
            _MATERIALIZATION_FIELDS,
            _MATERIALIZATION_FIELDS,
            "materialization_digest",
        )
        return cls(value, "materialization_digest")

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> "MaterializedContextV1":
        value = _strict_payload(
            raw,
            _MATERIALIZATION_FIELDS - {"materialization_digest"},
            _MATERIALIZATION_FIELDS,
            "materialization_digest",
        )
        if value.get("schema_version") != _MATERIALIZATION_SCHEMA:
            raise ContractError("UNSUPPORTED_MATERIALIZATION")
        return cls(
            {**value, "materialization_digest": raw["materialization_digest"]},
            "materialization_digest",
        )


@dataclass(frozen=True)
class ManagedMaterialization:
    materialization: MaterializedContextV1
    sealed_invocation: SealedInvocationV1
    provider_request: Mapping[str, Any]
    serialized_request: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_request", _freeze(self.provider_request))
        object.__setattr__(self, "serialized_request", bytes(self.serialized_request))


@dataclass(frozen=True)
class PersistedManagedMaterialization(ManagedMaterialization):
    receipt_path: Path
    _receipt_store: "MaterializationReceiptStore"


class MaterializationReceiptStore:
    """Durable content-addressed owner for final materialization receipts."""

    _SCHEMA = "ares.context-materialization-store/v1"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.root.is_dir():
            raise ContractError("MATERIALIZATION_STORE_UNAVAILABLE")

    def _path(self, materialization_digest: str) -> Path:
        if not _is_algorithm_digest(materialization_digest, "sha256"):
            raise ContractError("INVALID_DIGEST", "materialization_digest")
        return self.root / f"{materialization_digest.removeprefix('sha256:')}.json"

    def persist(
        self, managed: ManagedMaterialization
    ) -> PersistedManagedMaterialization:
        if not isinstance(managed, ManagedMaterialization):
            raise ContractError("INVALID_MATERIALIZATION_INPUT")
        path = self._path(managed.materialization.artifact_digest)
        serialized = bytes(managed.serialized_request)
        envelope = {
            "schema": self._SCHEMA,
            "materialization": managed.materialization.to_dict(),
            "sealed_invocation": managed.sealed_invocation.to_dict(),
            "provider_request": _thaw(managed.provider_request),
            "serialized_request_base64": base64.b64encode(serialized).decode("ascii"),
            "serialized_request_sha256": "sha256:"
            + hashlib.sha256(serialized).hexdigest(),
        }
        encoded = canonical_json(envelope)
        if path.exists():
            try:
                current = path.read_bytes()
            except OSError:
                raise ContractError("MATERIALIZATION_STORE_UNAVAILABLE") from None
            if current != encoded:
                raise ContractError("MATERIALIZATION_RECEIPT_CONFLICT")
        else:
            temporary = self.root / f".{path.stem}.{os.getpid()}.{id(managed)}.tmp"
            try:
                descriptor = os.open(
                    temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                directory = os.open(
                    self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                raise ContractError("MATERIALIZATION_STORE_UNAVAILABLE") from None
        loaded = self.load(managed.materialization.artifact_digest)
        return PersistedManagedMaterialization(
            loaded.materialization,
            loaded.sealed_invocation,
            loaded.provider_request,
            loaded.serialized_request,
            path,
            self,
        )

    def load(self, materialization_digest: str) -> ManagedMaterialization:
        path = self._path(materialization_digest)
        try:
            raw = _strict_json_bytes(path.read_bytes())
        except ContractError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise ContractError("MATERIALIZATION_RECEIPT_UNAVAILABLE") from None
        if (
            not isinstance(raw, Mapping)
            or set(raw)
            != {
                "schema",
                "materialization",
                "sealed_invocation",
                "provider_request",
                "serialized_request_base64",
                "serialized_request_sha256",
            }
            or raw.get("schema") != self._SCHEMA
        ):
            raise ContractError("MATERIALIZATION_RECEIPT_MALFORMED")
        materialization = MaterializedContextV1.parse(raw["materialization"])
        sealed = SealedInvocationV1.parse(raw["sealed_invocation"])
        if materialization.artifact_digest != materialization_digest:
            raise ContractError("MATERIALIZATION_BINDING_MISMATCH")
        provider_request = raw["provider_request"]
        if (
            not isinstance(provider_request, Mapping)
            or sealed.to_dict()["rendered_payload"] != provider_request
        ):
            raise ContractError("MATERIALIZATION_BINDING_MISMATCH")
        try:
            serialized = base64.b64decode(
                raw["serialized_request_base64"], validate=True
            )
        except (ValueError, TypeError):
            raise ContractError("MATERIALIZATION_RECEIPT_MALFORMED") from None
        serialized_digest = "sha256:" + hashlib.sha256(serialized).hexdigest()
        if (
            raw["serialized_request_sha256"] != serialized_digest
            or materialization.to_dict()["provider_request_digest"] != serialized_digest
            or sealed.to_dict()["context_digest"] != materialization_digest
        ):
            raise ContractError("MATERIALIZATION_BINDING_MISMATCH")
        return ManagedMaterialization(
            materialization, sealed, provider_request, serialized
        )


class GovernedContextMaterializer:
    """One candidate boundary used by every managed model-call class."""

    def materialize(
        self,
        context: ContextPacketV1,
        basis: ResolvedPolicyBasisV1,
        *,
        call_kind: ManagedCallKind,
        current_instruction: str,
        role_purpose: str,
        route_class: str,
        provider_identity: str,
        provider_config: Mapping[str, Any],
        model_ref: str,
        source_revision: str,
        graph_obligation_ref: str,
        graph_obligation_digest: str,
        output_reserve: int,
        tool_schemas: Sequence[Mapping[str, Any]],
        resolve_source_ref: Callable[[str, str], Mapping[str, Any]],
        resolve_graph_obligation: Callable[[str, str], bool | Mapping[str, Any]],
        memory_port: SemanticMemoryWitnessedPort | None,
        memory_requirement: MemoryRequirement,
        memory_query: str | None,
        requested_memory_namespaces: Sequence[str],
        authorized_memory_namespaces: Sequence[str],
        memory_caller: str,
        memory_subject: str,
        memory_audiences: Sequence[str],
        serialize_provider_request: Callable[[Mapping[str, Any]], bytes],
        count_tokens: Callable[[bytes], int],
        tokenizer_ref: str,
        tokenizer_mode: str,
        template_ref: str,
        provider_hard_input_tokens: int,
        provider_hard_request_bytes: int,
        attachments: Sequence[Mapping[str, Any]],
        now_utc: str,
        memory_request_id: str | None = None,
    ) -> ManagedMaterialization:
        if not isinstance(context, ContextPacketV1) or not isinstance(
            call_kind, ManagedCallKind
        ):
            raise ContractError("INVALID_MATERIALIZATION_INPUT")
        if not isinstance(memory_requirement, MemoryRequirement):
            raise ContractError("INVALID_MEMORY_REQUIREMENT")
        if tokenizer_mode not in {"exact", "conservative"}:
            raise ContractError("UNSUPPORTED_CONTEXT_ACCOUNTING")
        for ref_name, ref_value in (
            ("provider_identity", provider_identity),
            ("model_ref", model_ref),
            ("source_revision", source_revision),
            ("graph_obligation_ref", graph_obligation_ref),
            ("tokenizer_ref", tokenizer_ref),
            ("template_ref", template_ref),
        ):
            _check_ref(ref_value, ref_name)
        if not _is_algorithm_digest(graph_obligation_digest):
            raise ContractError("INVALID_DIGEST", "graph_obligation_digest")
        _parse_utc(now_utc, "now_utc")
        owner = ContextMaterializer._owner_payload(basis)
        basis_payload = basis.to_dict()
        context_payload = context.to_dict()
        if owner["status"] != "admitted":
            raise ContractError("POLICY_BASIS_BLOCKED")
        if context_payload["mission_ref"] != owner["mission_ref"]:
            raise ContractError("POLICY_MISSION_MISMATCH")
        if source_revision != owner["source_revision"]:
            raise ContractError("SOURCE_REVISION_MISMATCH")
        if not isinstance(current_instruction, str) or not current_instruction.strip():
            raise ContractError("MANDATORY_INSTRUCTION_MISSING")
        if digest(current_instruction) != owner["instruction_digest"]:
            raise ContractError("CURRENT_INSTRUCTION_MISMATCH")
        if not isinstance(role_purpose, str) or not role_purpose.strip():
            raise ContractError("MANDATORY_ROLE_PURPOSE_MISSING")
        if _parse_utc(owner["not_before"], "not_before") > _parse_utc(
            now_utc, "now_utc"
        ):
            raise ContractError("POLICY_NOT_YET_VALID")
        if _parse_utc(owner["not_after"], "not_after") <= _parse_utc(
            now_utc, "now_utc"
        ):
            raise ContractError("POLICY_EXPIRED")
        if route_class not in owner["allowed_route_classes"]:
            raise ContractError("ROUTE_DENIED")
        if "sealed_completion" not in owner["allowed_effect_classes"]:
            raise ContractError("EFFECT_DENIED")
        if not _graph_current(
            graph_obligation_ref, graph_obligation_digest, resolve_graph_obligation
        ):
            raise ContractError("GRAPH_OBLIGATION_MISMATCH")
        _reject_credentials(provider_config)
        _validate_provider_config(provider_config, provider_identity, model_ref)
        if (
            type(output_reserve) is not int
            or output_reserve < 1
            or output_reserve > owner["limits"]["max_output_tokens"]
        ):
            raise ContractError("OUTPUT_RESERVE_DENIED")
        if (
            type(provider_hard_input_tokens) is not int
            or provider_hard_input_tokens < 1
        ):
            raise ContractError("INVALID_PROVIDER_LIMIT")
        if (
            type(provider_hard_request_bytes) is not int
            or provider_hard_request_bytes < 1
        ):
            raise ContractError("INVALID_PROVIDER_LIMIT")
        _validate_attachments(attachments, tokenizer_mode)

        included_sources: list[dict[str, Any]] = []
        included_slice_refs: list[str] = []
        omissions: dict[tuple[str, str], int] = {}
        for item in context_payload["included_refs"]:
            required = item["purpose"] in {
                "required",
                "mandatory",
                "current_instruction",
            }
            try:
                resolved = resolve_source_ref(item["ref"], item["digest"])
            except Exception:
                resolved = {"status": "unavailable"}
            if not isinstance(resolved, Mapping):
                resolved = {"status": "unavailable"}
            status = resolved.get("status", "resolved")
            if status != "resolved":
                if required:
                    code = (
                        "MANDATORY_SOURCE_FORBIDDEN"
                        if status == "forbidden"
                        else "SOURCE_REFERENCE_MISMATCH"
                    )
                    raise ContractError(code)
                _add_omission(
                    omissions,
                    "source",
                    str(
                        status
                        if status in {"forbidden", "unavailable", "stale"}
                        else "unavailable"
                    ),
                )
                continue
            if resolved.get("digest") != item["digest"]:
                raise ContractError("SOURCE_REFERENCE_MISMATCH")
            if resolved.get("source_generation") not in (None, source_revision):
                raise ContractError("SOURCE_REVISION_MISMATCH")
            content = resolved.get("content")
            classification = resolved.get("classification")
            if not isinstance(content, str):
                raise ContractError("SOURCE_REFERENCE_MISMATCH")
            if classification not in owner["allowed_disclosure_classes"]:
                if required:
                    raise ContractError("MANDATORY_SOURCE_FORBIDDEN")
                _add_omission(omissions, "source", "forbidden")
                continue
            included_sources.append({
                "ref": item["ref"],
                "digest": item["digest"],
                "purpose": item["purpose"],
                "classification": classification,
                "trust": "untrusted_data",
                "content": content,
            })
            included_slice_refs.append(item["ref"])

        memory_values: dict[str, Any]
        memory_results: list[dict[str, Any]] = []
        if memory_requirement is MemoryRequirement.NOT_REQUIRED:
            if memory_query not in (None, ""):
                raise ContractError("MEMORY_QUERY_NOT_REQUIRED")
            memory_values = {
                "owner": SEMANTIC_MEMORY_OWNER,
                "state": MemoryResolutionState.NOT_REQUIRED.value,
                "requirement": memory_requirement.value,
                "receipt_ref": None,
                "authority_snapshot_ref": None,
                "retrieval_epoch": None,
                "query_digest": None,
                "result_refs": [],
                "observation_digest": None,
            }
        else:
            if not isinstance(memory_port, SemanticMemoryWitnessedPort):
                code = (
                    "MEMORY_REQUIRED_UNAVAILABLE"
                    if memory_requirement is MemoryRequirement.REQUIRED
                    else "MEMORY_OWNER_UNAVAILABLE"
                )
                raise ContractError(code)
            if not isinstance(memory_query, str) or not memory_query.strip():
                raise ContractError("MEMORY_QUERY_MISSING")
            if memory_request_id is None:
                memory_request_id = (
                    "memory-request:"
                    + hashlib.sha256(
                        canonical_json({
                            "context": context_payload["context_digest"],
                            "basis": basis_payload["basis_digest"],
                            "query": memory_query,
                            "namespaces": list(requested_memory_namespaces),
                        })
                    ).hexdigest()
                )
            observation = memory_port.resolve(
                requirement=memory_requirement,
                request_id=memory_request_id,
                query=memory_query,
                top_k=3,
                requested_namespaces=requested_memory_namespaces,
                authorized_namespaces=authorized_memory_namespaces,
                caller=memory_caller,
                subject=memory_subject,
                audiences=memory_audiences,
            )
            if not observation.owner_verified:
                raise ContractError("MEMORY_OWNER_UNVERIFIED")
            raw_memory = observation.to_dict()
            if raw_memory["state"] == MemoryResolutionState.UNAVAILABLE.value:
                if memory_requirement is MemoryRequirement.REQUIRED:
                    raise ContractError("MEMORY_REQUIRED_UNAVAILABLE")
                _add_omission(omissions, "memory", "unavailable")
            if raw_memory["state"] == MemoryResolutionState.STALE.value:
                if memory_requirement is MemoryRequirement.REQUIRED:
                    raise ContractError("MEMORY_REQUIRED_DEGRADED")
                _add_omission(omissions, "memory", "stale_or_degraded")
            if raw_memory["state"] == MemoryResolutionState.FORBIDDEN.value:
                if memory_requirement is MemoryRequirement.REQUIRED:
                    raise ContractError("MEMORY_REQUIRED_FORBIDDEN")
                _add_omission(omissions, "memory", "forbidden")
            memory_results = [
                {
                    "ref": result["result_ref"],
                    "digest": result["result_digest"],
                    "namespace": result["namespace"],
                    "source": result["source"],
                    "trust": "untrusted_data",
                    "content": result["content"],
                }
                for result in raw_memory["results"]
            ]
            included_slice_refs.extend(raw_memory["result_refs"])
            memory_values = {
                "owner": raw_memory["owner"],
                "state": raw_memory["state"],
                "requirement": raw_memory["requirement"],
                "receipt_ref": raw_memory["receipt_ref"],
                "authority_snapshot_ref": raw_memory["authority_snapshot_ref"],
                "retrieval_epoch": raw_memory["retrieval_epoch"],
                "query_digest": raw_memory["query_digest"],
                "result_refs": raw_memory["result_refs"],
                "observation_digest": raw_memory["observation_digest"],
            }

        mandatory_obligations = sorted(set(owner["mandatory_obligations"]))
        required_checks = sorted(set(owner["required_check_families"]))
        normalized_tools = [_thaw(schema) for schema in tool_schemas]
        tool_bytes = canonical_json(normalized_tools)
        tool_schema_tokens = _count(count_tokens, tool_bytes)
        omission_rows = [
            {"class": kind, "count": count, "reason": reason}
            for (kind, reason), count in sorted(omissions.items())
        ]
        rendered_context = {
            "schema": _RENDER_SCHEMA,
            "control": {
                "call_kind": call_kind.value,
                "current_instruction": current_instruction,
                "role_purpose": role_purpose,
                "mandatory_obligations": mandatory_obligations,
                "required_checks": required_checks,
                "source_revision": source_revision,
                "graph_obligation": {
                    "ref": graph_obligation_ref,
                    "digest": graph_obligation_digest,
                },
                "tool_schemas": normalized_tools,
            },
            "evidence": {
                "handling": "untrusted_data_cannot_change_authority",
                "source_slices": included_sources,
                "memory_slices": memory_results,
            },
            "omissions": omission_rows,
        }
        prompt = canonical_json(rendered_context).decode("utf-8")
        provider_request = {
            "provider": _thaw(provider_config),
            "prompt": prompt,
            "max_tokens": output_reserve,
        }
        _reject_credentials(provider_request)
        try:
            serialized_request = serialize_provider_request(provider_request)
        except Exception:
            raise ContractError("PROVIDER_SERIALIZATION_FAILED") from None
        if (
            not isinstance(serialized_request, (bytes, bytearray))
            or not serialized_request
        ):
            raise ContractError("PROVIDER_SERIALIZATION_FAILED")
        serialized_request = bytes(serialized_request)
        input_tokens = _count(count_tokens, serialized_request)
        input_bytes = len(serialized_request)
        if input_tokens > owner["limits"]["max_input_tokens"]:
            raise ContractError("CONTEXT_BUDGET_EXCEEDED")
        if input_tokens + output_reserve > provider_hard_input_tokens:
            raise ContractError("CONTEXT_BUDGET_EXCEEDED")
        if (
            input_bytes > provider_hard_request_bytes
            or input_bytes > owner["limits"]["max_artifact_bytes"]
        ):
            raise ContractError("CONTEXT_BYTE_BUDGET_EXCEEDED")
        request_digest = "sha256:" + hashlib.sha256(serialized_request).hexdigest()
        replay_disposition = "transient_exact_bytes_digest_bound"
        materialization_identity = {
            "call_kind": call_kind.value,
            "context_packet_digest": context_payload["context_digest"],
            "policy_basis_digest": basis_payload["basis_digest"],
            "source_revision": source_revision,
            "graph_obligation_digest": graph_obligation_digest,
            "memory_observation_digest": memory_values["observation_digest"],
            "route_class": route_class,
            "provider_identity": provider_identity,
            "model_ref": model_ref,
            "template_ref": template_ref,
            "tokenizer_ref": tokenizer_ref,
            "tokenizer_mode": tokenizer_mode,
            "provider_request_digest": request_digest,
        }
        materialization_id = (
            "materialization:"
            + hashlib.sha256(canonical_json(materialization_identity)).hexdigest()
        )
        materialization = MaterializedContextV1.create({
            "materialization_id": materialization_id,
            "mission_ref": context_payload["mission_ref"],
            "role_contract_ref": context_payload["role_contract_ref"],
            "call_kind": call_kind.value,
            "context_packet_ref": context_payload["context_packet_id"],
            "context_packet_digest": context_payload["context_digest"],
            "policy_basis_ref": basis_payload["basis_ref"],
            "policy_basis_digest": basis_payload["basis_digest"],
            "source_revision": source_revision,
            "graph_obligation_ref": graph_obligation_ref,
            "graph_obligation_digest": graph_obligation_digest,
            "memory": memory_values,
            "source_slices": [
                {
                    "ref": item["ref"],
                    "digest": item["digest"],
                    "purpose": item["purpose"],
                    "classification": item["classification"],
                }
                for item in included_sources
            ],
            "included_slices": sorted(set(included_slice_refs)),
            "omissions": omission_rows,
            "mandatory_obligations": mandatory_obligations,
            "required_checks": required_checks,
            "materializer_ref": MATERIALIZER_VERSION,
            "template_ref": template_ref,
            "tokenizer_ref": tokenizer_ref,
            "tokenizer_mode": tokenizer_mode,
            "tool_schema_digest": digest(normalized_tools),
            "tool_schema_tokens": tool_schema_tokens,
            "tool_schema_bytes": len(tool_bytes),
            "route_class": route_class,
            "provider_identity": provider_identity,
            "model_ref": model_ref,
            "provider_serialization_ref": "canonical-native-completion-request-v1",
            "provider_request_digest": request_digest,
            "input_tokens": input_tokens,
            "input_bytes": input_bytes,
            "output_reserve": output_reserve,
            "provider_hard_input_tokens": provider_hard_input_tokens,
            "provider_hard_request_bytes": provider_hard_request_bytes,
            "policy_input_cap": owner["limits"]["max_input_tokens"],
            "policy_output_cap": owner["limits"]["max_output_tokens"],
            "replay_disposition": replay_disposition,
            "materialized_at": now_utc,
        })
        sealed = SealedInvocationV1.create({
            "invocation_id": "invocation:"
            + materialization.artifact_digest.removeprefix("sha256:"),
            "mission_ref": context_payload["mission_ref"],
            "context_packet_ref": context_payload["context_packet_id"],
            # Native context binding now identifies the final materialization,
            # not the earlier reference-only ContextPacket.
            "context_digest": materialization.artifact_digest,
            "basis_ref": basis_payload["basis_ref"],
            "basis_digest": basis_payload["basis_digest"],
            "source_revision": source_revision,
            "graph_obligation_ref": graph_obligation_ref,
            "graph_obligation_digest": graph_obligation_digest,
            "route_class": route_class,
            "provider_identity": provider_identity,
            "model_ref": model_ref,
            "included_refs": sorted(set(included_slice_refs))
            or [context_payload["context_packet_id"]],
            "input_tokens": input_tokens,
            "output_reserve": output_reserve,
            "rendered_payload": provider_request,
            "payload_digest": digest(provider_request),
            "sealed_at": now_utc,
        })
        return ManagedMaterialization(
            materialization, sealed, provider_request, serialized_request
        )

    def authorize_egress(
        self,
        managed: PersistedManagedMaterialization,
        basis: ResolvedPolicyBasisV1,
        *,
        route_class: str,
        provider_identity: str,
        model_ref: str,
        serialized_request: bytes,
        now_utc: str,
        resolve_owner: Callable[[str, str], Mapping[str, Any] | None],
        resolve_graph_obligation: Callable[[str, str], bool | Mapping[str, Any]],
        memory_port: SemanticMemoryWitnessedPort,
        resolve_source_authorization: Callable[[str, str], Mapping[str, Any]],
    ) -> bytes:
        if not isinstance(managed, PersistedManagedMaterialization):
            raise ContractError("INVALID_EGRESS_INPUT")
        reloaded = managed._receipt_store.load(managed.materialization.artifact_digest)
        if (
            reloaded.materialization.canonical_bytes()
            != managed.materialization.canonical_bytes()
            or reloaded.sealed_invocation.canonical_bytes()
            != managed.sealed_invocation.canonical_bytes()
            or reloaded.serialized_request != managed.serialized_request
        ):
            raise ContractError("MATERIALIZATION_BINDING_MISMATCH")
        materialization = managed.materialization.to_dict()
        sealed = managed.sealed_invocation.to_dict()
        if sealed["context_digest"] != managed.materialization.artifact_digest:
            raise ContractError("MATERIALIZATION_BINDING_MISMATCH")
        if sealed["rendered_payload"] != _thaw(managed.provider_request):
            raise ContractError("SEALED_PAYLOAD_MISMATCH")
        if not isinstance(serialized_request, (bytes, bytearray)):
            raise ContractError("SERIALIZED_REQUEST_MISMATCH")
        current_digest = (
            "sha256:" + hashlib.sha256(bytes(serialized_request)).hexdigest()
        )
        if current_digest != materialization["provider_request_digest"]:
            raise ContractError("SERIALIZED_REQUEST_MISMATCH")
        ContextMaterializer().authorize_egress(
            managed.sealed_invocation,
            basis,
            route_class=route_class,
            provider_identity=provider_identity,
            model_ref=model_ref,
            rendered_payload=managed.provider_request,
            now_utc=now_utc,
            resolve_owner=resolve_owner,
            resolve_graph_obligation=resolve_graph_obligation,
        )
        for source in materialization["source_slices"]:
            try:
                current_source = resolve_source_authorization(
                    source["ref"], source["digest"]
                )
            except Exception:
                raise ContractError("SOURCE_AUTHORITY_CHANGED") from None
            if (
                not isinstance(current_source, Mapping)
                or current_source.get("status", "resolved") != "resolved"
                or current_source.get("digest") != source["digest"]
                or current_source.get("classification") != source["classification"]
                or current_source.get("source_generation")
                not in (None, materialization["source_revision"])
            ):
                raise ContractError("SOURCE_AUTHORITY_CHANGED")
        memory = materialization["memory"]
        if memory["state"] not in {
            MemoryResolutionState.NOT_REQUIRED.value,
            MemoryResolutionState.UNAVAILABLE.value,
        }:
            if not isinstance(memory_port, SemanticMemoryWitnessedPort):
                raise ContractError("MEMORY_OWNER_STATE_UNAVAILABLE")
            current = memory_port.current_state()
            expected = {
                "snapshot_id": memory["authority_snapshot_ref"],
                "retrieval_epoch": memory["retrieval_epoch"],
            }
            if not isinstance(current, Mapping) or dict(current) != expected:
                raise ContractError("MEMORY_AUTHORITY_CHANGED")
        return bytes(serialized_request)


def plan_legacy_memory_import(
    records: Sequence[Mapping[str, Any]], *, source_ref: str
) -> ImmutableArtifact:
    """Create a no-write migration plan without inventing temporal/authority facts."""
    _check_ref(source_ref, "source_ref")
    if not isinstance(records, Sequence) or isinstance(
        records, (str, bytes, bytearray)
    ):
        raise ContractError("INVALID_LEGACY_EXPORT")
    planned: list[dict[str, Any]] = []
    for index, raw in enumerate(records):
        if not isinstance(raw, Mapping):
            raise ContractError("INVALID_LEGACY_EXPORT")
        legacy_id = raw.get("id")
        content = raw.get("content")
        if (
            not isinstance(legacy_id, str)
            or not legacy_id
            or not isinstance(content, str)
        ):
            raise ContractError("INVALID_LEGACY_EXPORT")
        planned.append({
            "ordinal": index,
            "legacy_id": legacy_id,
            "content_digest": digest(content),
            "source_ref": source_ref,
            "source_label": str(raw.get("source") or "legacy_unknown"),
            "valid_time": "unknown",
            "recorded_time": str(raw.get("recorded_at") or "unknown"),
            "supersession_state": "unknown",
            "authority_state": "unverified",
            "support_state": "unjudged",
        })
    value = {
        "schema_version": _LEGACY_PLAN_SCHEMA,
        "mode": "dry_run_no_write",
        "source_ref": source_ref,
        "record_count": len(planned),
        "records": planned,
        "dual_write": False,
        "live_store_mutated": False,
    }
    value = _finalize(value, "plan_digest")
    return ImmutableArtifact(value, "plan_digest")


def _qualified_blake3(value: Any) -> str | None:
    if (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    ):
        return "blake3:" + value.lower()
    if _is_algorithm_digest(value, "blake3"):
        return str(value).lower()
    return None


def _strict_json_bytes(payload: bytes) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ContractError("MATERIALIZATION_RECEIPT_DUPLICATE_KEY")
            result[key] = value
        return result

    return json.loads(payload.decode("utf-8"), object_pairs_hook=object_pairs)


def _string_set(values: Sequence[str], field: str, *, allow_empty: bool) -> list[str]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise ContractError("INVALID_MEMORY_SCOPE", field)
    normalized = sorted(set(values))
    if (not allow_empty and not normalized) or not all(
        isinstance(value, str) and value for value in normalized
    ):
        raise ContractError("INVALID_MEMORY_SCOPE", field)
    return normalized


def _graph_current(
    ref: str,
    value_digest: str,
    resolver: Callable[[str, str], bool | Mapping[str, Any]],
) -> bool:
    try:
        result = resolver(ref, value_digest)
    except Exception:
        return False
    return result is True or (
        isinstance(result, Mapping)
        and result.get("ref") == ref
        and result.get("digest") == value_digest
    )


def _validate_provider_config(
    provider_config: Mapping[str, Any], provider_identity: str, model_ref: str
) -> None:
    if not isinstance(provider_config, Mapping) or set(provider_config) != {
        "kind",
        "base_url",
        "model",
    }:
        raise ContractError("INVALID_PROVIDER_CONFIG")
    kind = provider_config.get("kind")
    base_url = provider_config.get("base_url")
    model = provider_config.get("model")
    if not isinstance(kind, str) or not kind:
        raise ContractError("INVALID_PROVIDER_CONFIG")
    if not isinstance(base_url, str) or not base_url:
        raise ContractError("INVALID_PROVIDER_CONFIG")
    if not isinstance(model, str) or not model:
        raise ContractError("INVALID_PROVIDER_CONFIG")
    if (
        provider_identity != f"{kind}:{base_url.rstrip('/')}"
        or model_ref != f"model:{model}"
    ):
        raise ContractError("PROVIDER_ROUTE_MISMATCH")


def _reject_credentials(value: Any) -> None:
    forbidden = {
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "credentials",
        "password",
        "secret",
        "token",
    }
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in forbidden:
                raise ContractError("CREDENTIAL_MATERIAL_FORBIDDEN")
            _reject_credentials(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_credentials(item)


def _validate_attachments(
    attachments: Sequence[Mapping[str, Any]], tokenizer_mode: str
) -> None:
    if not isinstance(attachments, Sequence) or isinstance(
        attachments, (str, bytes, bytearray)
    ):
        raise ContractError("UNSUPPORTED_CONTEXT_ACCOUNTING")
    # This candidate's native CompletionRequestV1 has no attachment field.
    # Pretending to account metadata while dropping the actual bytes would be
    # a false bound, so every non-empty attachment request is blocked.
    if attachments:
        raise ContractError("UNSUPPORTED_CONTEXT_ACCOUNTING")


def _count(counter: Callable[[bytes], int], payload: bytes) -> int:
    try:
        value = counter(payload)
    except Exception:
        raise ContractError("UNSUPPORTED_CONTEXT_ACCOUNTING") from None
    if type(value) is not int or value < 1:
        raise ContractError("UNSUPPORTED_CONTEXT_ACCOUNTING")
    return value


def _add_omission(
    omissions: dict[tuple[str, str], int], kind: str, reason: str
) -> None:
    key = (kind, reason)
    omissions[key] = omissions.get(key, 0) + 1
