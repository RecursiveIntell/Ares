"""Deterministic non-activation specialist selection decisions.

This module deliberately produces an auditable *non-dispatch* decision. It
does not read provider health, capacity, credentials, model configuration, or
time-varying profile state, and it cannot start a profile or reserve a slot.
Electron remains the future owner of any capacity admission.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from .collaboration import (
    ContractError,
    ImmutableArtifact,
    SpecialistDescriptorV1,
    canonical_json,
    digest,
    specialist_descriptor_ref,
)

if TYPE_CHECKING:
    from hermes_cli.profiles import ProfileEvidenceRead


DECISION_SCHEMA = "AresSpecialistDecisionReceiptV1"


_SPECIALIST_MAX_BYTES = 262_144
_SPECIALIST_MAX_TEXT_BYTES = 65_536
_SPECIALIST_MAX_ARRAY_ITEMS = 128
_SPECIALIST_MAX_DEPTH = 16
_SPECIALIST_MAX_SOURCE_BYTES = 67_108_864
_SPECIALIST_REF_RE = re.compile(r"^[A-Za-z0-9._:/@#-]+$")
_SPECIALIST_SHA_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SPECIALIST_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SPECIALIST_ROLE_RE = re.compile(r"^role\.[A-Za-z0-9._-]+$")

_DISPOSITIONS = {
    "no_material_gap",
    "direct_evidence_or_skill_sufficient",
    "existing_role_covers_need",
    "existing_role_unindexed_or_unavailable",
    "coverage_unknown",
    "independent_review_needed",
    "evidence_blocked",
    "candidate_proposal",
}
_FILE_STATUSES = {
    "missing",
    "unreadable",
    "malformed",
    "valid_empty",
    "valid_populated",
}
_SOURCE_KINDS = {
    "descriptor_manifest",
    "panel_roster",
    "role_registry",
    "profile_registry",
    "binding_index",
    "other",
}


def _specialist_error(code: str, field: str = "") -> None:
    raise ContractError(code, field)


def _check_specialist_tree(value: Any, depth: int = 0) -> None:
    if depth > _SPECIALIST_MAX_DEPTH:
        _specialist_error("JSON_TOO_DEEP")
    if isinstance(value, float) and not math.isfinite(value):
        _specialist_error("NONFINITE_NUMBER")
    if isinstance(value, str):
        try:
            size = len(value.encode("utf-8", "strict"))
        except UnicodeEncodeError:
            _specialist_error("INVALID_UTF8")
        if size > _SPECIALIST_MAX_TEXT_BYTES:
            _specialist_error("STRING_TOO_LARGE")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                _specialist_error("INVALID_OBJECT_KEY")
            _check_specialist_tree(key, depth + 1)
            _check_specialist_tree(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        if len(value) > _SPECIALIST_MAX_ARRAY_ITEMS:
            _specialist_error("LIST_TOO_LARGE")
        for item in value:
            _check_specialist_tree(item, depth + 1)


def _json_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _specialist_error("DUPLICATE_JSON_KEY", key)
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    _specialist_error("NONFINITE_NUMBER")


def _text(value: Any, field: str, *, empty: bool = False) -> None:
    if not isinstance(value, str) or (not empty and not value):
        _specialist_error("INVALID_TEXT", field)
    try:
        if len(value.encode("utf-8", "strict")) > _SPECIALIST_MAX_TEXT_BYTES:
            _specialist_error("STRING_TOO_LARGE", field)
    except UnicodeEncodeError:
        _specialist_error("INVALID_UTF8", field)


def _ref(value: Any, field: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if (
        not isinstance(value, str)
        or len(value.encode("utf-8", "strict")) > 256
        or not _SPECIALIST_REF_RE.fullmatch(value)
    ):
        _specialist_error("INVALID_REFERENCE", field)


def _sha(value: Any, field: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or not _SPECIALIST_SHA_RE.fullmatch(value):
        _specialist_error("INVALID_DIGEST", field)


def _obj(value: Any, required: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _specialist_error("INVALID_OBJECT", field)
    keys = set(value)
    missing, unknown = required - keys, keys - required
    if missing:
        _specialist_error("MISSING_FIELD", f"{field}.{sorted(missing)[0]}")
    if unknown:
        _specialist_error("UNKNOWN_FIELD", f"{field}.{sorted(unknown)[0]}")
    return dict(value)


def _array(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        _specialist_error("INVALID_ARRAY", field)
    if len(value) > _SPECIALIST_MAX_ARRAY_ITEMS:
        _specialist_error("LIST_TOO_LARGE", field)
    return value


def _enum(value: Any, choices: set[str], field: str) -> None:
    if not isinstance(value, str) or value not in choices:
        _specialist_error("INVALID_ENUM", field)


def _sorted_unique(items: list[Any], field: str, *, key=lambda value: value) -> None:
    try:
        values = [key(item) for item in items]
        is_canonical = values == sorted(values) and len(values) == len(set(values))
    except (KeyError, TypeError):
        _specialist_error("INVALID_ARRAY_ITEM", field)
    if not is_canonical:
        _specialist_error("NONCANONICAL_ARRAY", field)


def _text_list(
    value: Any, field: str, *, refs: bool = False, sorted_unique: bool = False
) -> list[str]:
    items = _array(value, field)
    for item in items:
        if refs:
            _ref(item, field)
        else:
            _text(item, field)
    if sorted_unique:
        _sorted_unique(items, field)
    return items


def _input_manifest(value: Any) -> tuple[list[dict[str, Any]], set[str]]:
    rows = _array(value, "approved_input_manifest")
    ids: list[str] = []
    for index, row in enumerate(rows):
        item = _obj(
            row, {"artifact_ref", "byte_digest"}, f"approved_input_manifest[{index}]"
        )
        _ref(item["artifact_ref"], "artifact_ref")
        _sha(item["byte_digest"], "byte_digest")
        ids.append(item["artifact_ref"])
    _sorted_unique(rows, "approved_input_manifest", key=lambda row: row["artifact_ref"])
    return rows, set(ids)


def _validate_need(value: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "task_id",
        "task_ref",
        "task_digest",
        "obligation_id",
        "obligation_text",
        "affected_decision",
        "consequence",
        "evidence_refs",
        "approved_input_manifest",
        "coverage_snapshot_digest",
        "alternatives",
        "reviewer_assessments",
        "need_digest",
    }
    _obj(value, required, "need")
    _version(value)
    for key in (
        "task_id",
        "obligation_id",
        "obligation_text",
        "affected_decision",
        "consequence",
    ):
        _text(value[key], key)
    _ref(value["task_ref"], "task_ref")
    _sha(value["task_digest"], "task_digest")
    _sha(value["coverage_snapshot_digest"], "coverage_snapshot_digest")
    _sha(value["need_digest"], "need_digest")
    _manifest, allowed_refs = _input_manifest(value["approved_input_manifest"])
    refs = _text_list(
        value["evidence_refs"], "evidence_refs", refs=True, sorted_unique=True
    )
    if not set(refs) <= allowed_refs:
        _specialist_error("UNAPPROVED_EVIDENCE_REF", "evidence_refs")
    alternatives = _array(value["alternatives"], "alternatives")
    alt_ids: list[str] = []
    for index, row in enumerate(alternatives):
        item = _obj(
            row,
            {"alternative_ref", "kind", "description", "assessment", "evidence_refs"},
            f"alternatives[{index}]",
        )
        _ref(item["alternative_ref"], "alternative_ref")
        _enum(
            item["kind"],
            {
                "direct_evidence",
                "procedure",
                "skill",
                "existing_role",
                "independent_pass",
                "other",
            },
            "kind",
        )
        _text(item["description"], "description")
        _enum(
            item["assessment"], {"sufficient", "insufficient", "unknown"}, "assessment"
        )
        citations = _text_list(
            item["evidence_refs"],
            "alternative.evidence_refs",
            refs=True,
            sorted_unique=True,
        )
        if not set(citations) <= allowed_refs:
            _specialist_error("UNAPPROVED_EVIDENCE_REF", "alternatives.evidence_refs")
        alt_ids.append(item["alternative_ref"])
    if len(alt_ids) != len(set(alt_ids)):
        _specialist_error("DUPLICATE_REFERENCE", "alternatives")
    assessments = _array(value["reviewer_assessments"], "reviewer_assessments")
    _sorted_unique(
        assessments, "reviewer_assessments", key=lambda row: row["reviewer_ref"]
    )
    for index, row in enumerate(assessments):
        item = _obj(
            row,
            {
                "reviewer_ref",
                "materiality",
                "direct_sufficiency",
                "semantic_coverage",
                "substantive_method_coverage",
                "independent_pass_requirement",
                "method_feasibility",
                "disposition",
                "reason",
                "evidence_refs",
                "uncertainty",
            },
            f"reviewer_assessments[{index}]",
        )
        _ref(item["reviewer_ref"], "reviewer_ref")
        _enum(
            item["materiality"], {"material", "not_material", "unknown"}, "materiality"
        )
        _enum(
            item["direct_sufficiency"],
            {"sufficient", "insufficient", "unknown"},
            "direct_sufficiency",
        )
        _enum(
            item["semantic_coverage"],
            {"covered", "not_covered", "unknown"},
            "semantic_coverage",
        )
        _enum(
            item["substantive_method_coverage"],
            {"covered", "uncovered", "unknown"},
            "substantive_method_coverage",
        )
        _enum(
            item["independent_pass_requirement"],
            {"required", "not_required", "unknown"},
            "independent_pass_requirement",
        )
        _enum(
            item["method_feasibility"],
            {"specified", "undefined", "not_assessed"},
            "method_feasibility",
        )
        _enum(item["disposition"], _DISPOSITIONS, "disposition")
        _text(item["reason"], "reason")
        citations = _text_list(
            item["evidence_refs"],
            "reviewer.evidence_refs",
            refs=True,
            sorted_unique=True,
        )
        if not set(citations) <= allowed_refs:
            _specialist_error(
                "UNAPPROVED_EVIDENCE_REF", "reviewer_assessments.evidence_refs"
            )
        _text_list(item["uncertainty"], "uncertainty")


def _version(value: Mapping[str, Any]) -> None:
    if value.get("schema_version") != "1.0.0":
        _specialist_error("UNSUPPORTED_SCHEMA", "schema_version")


def _file_state(status: Any, byte_digest: Any, byte_length: Any, field: str) -> None:
    _enum(status, _FILE_STATUSES, f"{field}.status")
    if status in {"missing", "unreadable"}:
        if byte_digest is not None or byte_length is not None:
            _specialist_error("INVALID_MISSINGNESS", field)
    else:
        _sha(byte_digest, f"{field}.byte_digest")
        if (
            isinstance(byte_length, bool)
            or not isinstance(byte_length, (int, float))
            or (
                isinstance(byte_length, float)
                and (not math.isfinite(byte_length) or not byte_length.is_integer())
            )
            or byte_length < 0
            or byte_length > _SPECIALIST_MAX_SOURCE_BYTES
        ):
            _specialist_error("INVALID_BYTE_LENGTH", field)
        if status == "valid_populated" and byte_length == 0:
            _specialist_error("INVALID_MISSINGNESS", field)


def _validate_coverage(value: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "snapshot_id",
        "source_revision",
        "cutoff",
        "declared_scope",
        "sources",
        "roster_manifest",
        "roster_identity_digest",
        "snapshot_digest",
    }
    _obj(value, required, "coverage")
    _version(value)
    for key in ("snapshot_id", "source_revision", "cutoff"):
        _text(value[key], key)
    _sha(value["roster_identity_digest"], "roster_identity_digest")
    _sha(value["snapshot_digest"], "snapshot_digest")
    scope = _obj(
        value["declared_scope"],
        {
            "scope_ref",
            "description",
            "source_refs",
            "completeness",
            "gaps",
            "source_profile_applicability",
        },
        "declared_scope",
    )
    _ref(scope["scope_ref"], "scope_ref")
    _text(scope["description"], "scope.description")
    source_refs = _text_list(
        scope["source_refs"], "scope.source_refs", refs=True, sorted_unique=True
    )
    if not source_refs:
        _specialist_error("EMPTY_ARRAY", "scope.source_refs")
    _enum(
        scope["completeness"],
        {
            "complete_within_declared_sources",
            "incomplete",
            "stale_or_concurrent_drift",
            "unknown",
        },
        "scope.completeness",
    )
    gaps = _text_list(scope["gaps"], "scope.gaps", sorted_unique=True)
    completeness = scope["completeness"]
    complete = completeness == "complete_within_declared_sources"
    if complete and gaps:
        _specialist_error("COMPLETENESS_GAP_MISMATCH", "declared_scope.gaps")
    if not complete and not gaps:
        _specialist_error("MISSING_COVERAGE_GAP", "declared_scope.gaps")
    sources = _array(value["sources"], "sources")
    _sorted_unique(sources, "sources", key=lambda row: row["source_ref"])
    source_map: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(sources):
        item = _obj(
            row,
            {
                "source_ref",
                "source_kind",
                "revision",
                "cutoff",
                "status",
                "byte_digest",
                "byte_length",
                "profile_ids",
            },
            f"sources[{index}]",
        )
        _ref(item["source_ref"], "source_ref")
        _enum(item["source_kind"], _SOURCE_KINDS, "source_kind")
        _text(item["revision"], "source.revision")
        _text(item["cutoff"], "source.cutoff")
        _file_state(
            item["status"],
            item["byte_digest"],
            item["byte_length"],
            f"sources[{index}]",
        )
        profile_ids = _text_list(
            item["profile_ids"], "source.profile_ids", sorted_unique=True
        )
        for profile_id in profile_ids:
            if not _SPECIALIST_PROFILE_RE.fullmatch(profile_id):
                _specialist_error("INVALID_PROFILE_ID", "source.profile_ids")
        if (
            item["status"] in {"missing", "unreadable", "malformed", "valid_empty"}
            and profile_ids
        ):
            _specialist_error("INVALID_SOURCE_PROFILES", item["source_ref"])
        source_map[item["source_ref"]] = item
    if set(source_map) != set(source_refs):
        _specialist_error("SOURCE_SCOPE_MISMATCH", "sources")
    roster = _array(value["roster_manifest"], "roster_manifest")
    _sorted_unique(roster, "roster_manifest", key=lambda row: row["profile_id"])
    union = sorted({profile_id for row in sources for profile_id in row["profile_ids"]})
    roster_ids: list[str] = []
    roster_files: dict[str, set[tuple[str, str]]] = {}
    for index, row in enumerate(roster):
        item = _obj(
            row,
            {"profile_id", "source_files", "roles", "exclusions"},
            f"roster_manifest[{index}]",
        )
        profile_id = item["profile_id"]
        _text(profile_id, "profile_id")
        if not _SPECIALIST_PROFILE_RE.fullmatch(profile_id):
            _specialist_error("INVALID_PROFILE_ID", "profile_id")
        roster_ids.append(profile_id)
        expected_pair_ids: set[tuple[str, str]] = set()
        files = _array(item["source_files"], "source_files")
        _sorted_unique(
            files,
            "source_files",
            key=lambda fact: (fact["source_ref"], fact["file_ref"]),
        )
        for file_index, fact in enumerate(files):
            source_file = _obj(
                fact,
                {"source_ref", "file_ref", "status", "byte_digest", "byte_length"},
                f"source_files[{file_index}]",
            )
            _ref(source_file["source_ref"], "source_ref")
            _ref(source_file["file_ref"], "file_ref")
            _file_state(
                source_file["status"],
                source_file["byte_digest"],
                source_file["byte_length"],
                f"source_files[{file_index}]",
            )
            if source_file["source_ref"] not in source_refs:
                _specialist_error("SOURCE_PROFILE_MISMATCH", profile_id)
            expected_pair_ids.add((source_file["source_ref"], source_file["file_ref"]))
        roles = _array(item["roles"], "roles")
        _sorted_unique(roles, "roles", key=lambda fact: fact["semantic_role_id"])
        for role in roles:
            fact = _obj(
                role,
                {
                    "semantic_role_id",
                    "descriptor_status",
                    "descriptor_ref",
                    "binding_status",
                    "binding_ref",
                    "index_status",
                    "enabled_state",
                    "availability",
                    "semantic_coverage",
                },
                "role",
            )
            role_id = fact["semantic_role_id"]
            _text(role_id, "semantic_role_id")
            if not _SPECIALIST_ROLE_RE.fullmatch(role_id):
                _specialist_error("INVALID_ROLE_ID", "semantic_role_id")
            _enum(
                fact["descriptor_status"],
                {"missing", "unreadable", "malformed", "valid", "unknown"},
                "descriptor_status",
            )
            _ref(fact["descriptor_ref"], "descriptor_ref", nullable=True)
            if (fact["descriptor_status"] == "valid") != (
                fact["descriptor_ref"] is not None
            ):
                _specialist_error("INVALID_MISSINGNESS", "descriptor_ref")
            _enum(
                fact["binding_status"],
                {"unbound", "bound", "mismatch", "unknown"},
                "binding_status",
            )
            _ref(fact["binding_ref"], "binding_ref", nullable=True)
            if (fact["binding_status"] in {"bound", "mismatch"}) != (
                fact["binding_ref"] is not None
            ):
                _specialist_error("INVALID_MISSINGNESS", "binding_ref")
            _enum(
                fact["index_status"],
                {"indexed", "unindexed", "unknown"},
                "index_status",
            )
            _enum(
                fact["enabled_state"],
                {"enabled", "disabled", "unknown"},
                "enabled_state",
            )
            _enum(
                fact["availability"],
                {
                    "available",
                    "disabled_by_owner",
                    "temporarily_unavailable",
                    "unknown",
                },
                "availability",
            )
            _enum(
                fact["semantic_coverage"],
                {"covered", "not_covered", "unknown"},
                "semantic_coverage",
            )
            if not complete and fact["semantic_coverage"] == "not_covered":
                _specialist_error("INCOMPLETE_CANNOT_ASSERT_NOT_COVERED", profile_id)
        _text_list(item["exclusions"], "exclusions", sorted_unique=True)
        roster_files[profile_id] = expected_pair_ids
    if roster_ids != union:
        _specialist_error("ROSTER_UNION_MISMATCH", "roster_manifest")
    if value["roster_identity_digest"] != digest(roster_ids):
        _specialist_error("ROSTER_IDENTITY_MISMATCH", "roster_identity_digest")

    applicability = _array(
        scope["source_profile_applicability"], "source_profile_applicability"
    )
    _sorted_unique(
        applicability,
        "source_profile_applicability",
        key=lambda row: (row["profile_id"], row["source_ref"]),
    )
    applicability_map: dict[tuple[str, str], dict[str, Any]] = {}
    for index, row in enumerate(applicability):
        item = _obj(
            row,
            {
                "profile_id",
                "source_ref",
                "applicability",
                "file_ref",
                "basis",
                "basis_refs",
            },
            f"source_profile_applicability[{index}]",
        )
        profile_id = item["profile_id"]
        _text(profile_id, "applicability.profile_id")
        if not _SPECIALIST_PROFILE_RE.fullmatch(profile_id):
            _specialist_error("INVALID_PROFILE_ID", "applicability.profile_id")
        source_ref = item["source_ref"]
        _ref(source_ref, "applicability.source_ref")
        if source_ref not in source_map:
            _specialist_error("SOURCE_PROFILE_MISMATCH", profile_id)
        if profile_id not in roster_files:
            _specialist_error("SOURCE_PROFILE_MATRIX_MISMATCH", profile_id)
        _enum(
            item["applicability"],
            {"required", "not_applicable", "unresolved"},
            "applicability",
        )
        _ref(item["file_ref"], "applicability.file_ref", nullable=True)
        if (item["applicability"] == "required") != (item["file_ref"] is not None):
            _specialist_error("INVALID_MISSINGNESS", "applicability.file_ref")
        _text(item["basis"], "applicability.basis")
        basis_refs = _text_list(
            item["basis_refs"],
            "applicability.basis_refs",
            refs=True,
            sorted_unique=True,
        )
        if not basis_refs or not set(basis_refs) <= set(source_refs):
            _specialist_error("INVALID_APPLICABILITY_BASIS", profile_id)
        identity_is_observed = any(
            source_map[basis_ref]["status"] == "valid_populated"
            and profile_id in source_map[basis_ref]["profile_ids"]
            for basis_ref in basis_refs
        )
        if not identity_is_observed:
            _specialist_error("INVALID_APPLICABILITY_BASIS", profile_id)
        key = (profile_id, source_ref)
        if key in applicability_map:
            _specialist_error("SOURCE_PROFILE_MATRIX_MISMATCH", profile_id)
        applicability_map[key] = item

    all_pairs = {
        (profile_id, source_ref)
        for profile_id in roster_ids
        for source_ref in source_refs
    }
    if set(applicability_map) != all_pairs:
        _specialist_error(
            "SOURCE_PROFILE_MATRIX_MISMATCH", "source_profile_applicability"
        )

    for profile_id in roster_ids:
        required_pairs = {
            (row["source_ref"], row["file_ref"])
            for (mapped_profile_id, _source_ref), row in applicability_map.items()
            if mapped_profile_id == profile_id and row["applicability"] == "required"
        }
        if roster_files[profile_id] != required_pairs:
            _specialist_error("SOURCE_PROFILE_MATRIX_MISMATCH", profile_id)

    if complete:
        if any(
            row["applicability"] == "unresolved" for row in applicability_map.values()
        ):
            _specialist_error(
                "UNRESOLVED_APPLICABILITY", "source_profile_applicability"
            )
        if any(
            row["status"] not in {"valid_empty", "valid_populated"}
            for row in source_map.values()
        ):
            _specialist_error("INCOMPLETE_REQUIRED_SOURCE", "sources")
        for row in roster:
            for source_file in row["source_files"]:
                if source_file["status"] not in {"valid_empty", "valid_populated"}:
                    _specialist_error("INCOMPLETE_REQUIRED_SOURCE", "source_files")


def _validate_proposal(value: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "need_digest",
        "coverage_snapshot_digest",
        "protocol_digest",
        "disposition",
        "reasons",
        "role_refs_considered",
        "proposed_responsibilities",
        "proposed_methods",
        "explicit_exclusions",
        "draft_soul",
        "curated_skill_refs",
        "requested_input_classes",
        "requested_tool_classes",
        "requested_model_class",
        "output_schema_ref",
        "output_schema_digest",
        "comparator",
        "expected_checkable_output",
        "falsifiers",
        "trial_readiness",
        "trial_blockers",
        "supersedes_proposal_ref",
        "proposal_digest",
    }
    _obj(value, required, "proposal")
    _version(value)
    for key in (
        "need_digest",
        "coverage_snapshot_digest",
        "protocol_digest",
        "proposal_digest",
    ):
        _sha(value[key], key)
    _enum(value["disposition"], _DISPOSITIONS, "disposition")
    for key in (
        "reasons",
        "proposed_responsibilities",
        "proposed_methods",
        "explicit_exclusions",
        "falsifiers",
        "trial_blockers",
    ):
        _text_list(value[key], key)
    _text_list(
        value["role_refs_considered"],
        "role_refs_considered",
        refs=True,
        sorted_unique=True,
    )
    _text(value["draft_soul"], "draft_soul", empty=True)
    skills = _array(value["curated_skill_refs"], "curated_skill_refs")
    _sorted_unique(skills, "curated_skill_refs", key=lambda row: row["skill_ref"])
    for index, row in enumerate(skills):
        item = _obj(row, {"skill_ref", "byte_digest"}, f"curated_skill_refs[{index}]")
        _ref(item["skill_ref"], "skill_ref")
        _sha(item["byte_digest"], "byte_digest")
    for key in ("requested_input_classes", "requested_tool_classes"):
        _text_list(value[key], key, sorted_unique=True)
        if not value[key]:
            _specialist_error("EMPTY_ARRAY", key)
    _text(
        value["requested_model_class"], "requested_model_class", empty=False
    ) if value["requested_model_class"] is not None else None
    _ref(value["output_schema_ref"], "output_schema_ref", nullable=True)
    _sha(value["output_schema_digest"], "output_schema_digest", nullable=True)
    if (value["output_schema_ref"] is None) != (value["output_schema_digest"] is None):
        _specialist_error("INCOMPLETE_NULL_PAIR", "output_schema_ref")
    comparator = value["comparator"]
    if comparator is not None:
        item = _obj(
            comparator, {"alternative_ref", "residual", "evidence_refs"}, "comparator"
        )
        _ref(item["alternative_ref"], "comparator.alternative_ref", nullable=True)
        _text(item["residual"], "comparator.residual")
        _text_list(
            item["evidence_refs"],
            "comparator.evidence_refs",
            refs=True,
            sorted_unique=True,
        )
    if value["expected_checkable_output"] is not None:
        _text(value["expected_checkable_output"], "expected_checkable_output")
    _enum(
        value["trial_readiness"],
        {"not_assessed", "blocked", "eligible_for_owner_review"},
        "trial_readiness",
    )
    _ref(value["supersedes_proposal_ref"], "supersedes_proposal_ref", nullable=True)


class _SpecialistArtifact(ImmutableArtifact):
    digest_field_name = ""
    validator = staticmethod(lambda _value: None)

    def __init__(self, payload: Mapping[str, Any], digest_field: str) -> None:
        if not isinstance(payload, Mapping):
            _specialist_error("INVALID_ARTIFACT")
        value = dict(payload)
        _check_specialist_tree(value)
        if digest_field != self.digest_field_name:
            _specialist_error("INVALID_DIGEST_FIELD")
        self.validator(value)
        supplied = value.get(self.digest_field_name)
        _sha(supplied, self.digest_field_name)
        material = dict(value)
        material.pop(self.digest_field_name)
        if supplied != digest(material):
            _specialist_error("DIGEST_MISMATCH", self.digest_field_name)
        if len(canonical_json(value)) > _SPECIALIST_MAX_BYTES:
            _specialist_error("ARTIFACT_TOO_LARGE")
        super().__init__(value, digest_field)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.digest_field != self.digest_field_name:
            _specialist_error("INVALID_DIGEST_FIELD")
        value = self.to_dict()
        _check_specialist_tree(value)
        self.validator(value)
        supplied = value.get(self.digest_field_name)
        _sha(supplied, self.digest_field_name)
        material = dict(value)
        material.pop(self.digest_field_name)
        if supplied != digest(material):
            _specialist_error("DIGEST_MISMATCH", self.digest_field_name)
        if len(canonical_json(value)) > _SPECIALIST_MAX_BYTES:
            _specialist_error("ARTIFACT_TOO_LARGE")

    @classmethod
    def create(cls, values: Mapping[str, Any]) -> "_SpecialistArtifact":
        if not isinstance(values, Mapping):
            _specialist_error("INVALID_ARTIFACT")
        payload = dict(values)
        payload[cls.digest_field_name] = "sha256:" + "0" * 64
        _check_specialist_tree(payload)
        cls.validator(payload)
        payload.pop(cls.digest_field_name, None)
        payload[cls.digest_field_name] = digest(payload)
        if len(canonical_json(payload)) > _SPECIALIST_MAX_BYTES:
            _specialist_error("ARTIFACT_TOO_LARGE")
        return cls(payload, cls.digest_field_name)

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> "_SpecialistArtifact":
        if not isinstance(raw, Mapping):
            _specialist_error("INVALID_ARTIFACT")
        value = dict(raw)
        _check_specialist_tree(value)
        cls.validator(value)
        supplied = value.get(cls.digest_field_name)
        _sha(supplied, cls.digest_field_name)
        material = dict(value)
        material.pop(cls.digest_field_name)
        if supplied != digest(material):
            _specialist_error("DIGEST_MISMATCH", cls.digest_field_name)
        if len(canonical_json(value)) > _SPECIALIST_MAX_BYTES:
            _specialist_error("ARTIFACT_TOO_LARGE")
        return cls(value, cls.digest_field_name)

    @classmethod
    def parse_json(cls, raw: bytes | str) -> "_SpecialistArtifact":
        if isinstance(raw, bytes):
            if len(raw) > _SPECIALIST_MAX_BYTES:
                _specialist_error("ARTIFACT_TOO_LARGE")
            try:
                text = raw.decode("utf-8", "strict")
            except UnicodeDecodeError:
                _specialist_error("INVALID_UTF8")
        elif isinstance(raw, str):
            try:
                encoded = raw.encode("utf-8", "strict")
            except UnicodeEncodeError:
                _specialist_error("INVALID_UTF8")
            if len(encoded) > _SPECIALIST_MAX_BYTES:
                _specialist_error("ARTIFACT_TOO_LARGE")
            text = raw
        else:
            _specialist_error("INVALID_JSON_INPUT")
        try:
            value = json.loads(
                text,
                object_pairs_hook=_json_object_pairs,
                parse_constant=_reject_json_constant,
            )
        except ContractError:
            raise
        except (json.JSONDecodeError, RecursionError, ValueError):
            _specialist_error("INVALID_JSON")
        if not isinstance(value, dict):
            _specialist_error("INVALID_ARTIFACT")
        _check_specialist_tree(value)
        return cls.parse(value)


class SpecialistNeedV1(_SpecialistArtifact):
    digest_field_name = "need_digest"
    validator = staticmethod(_validate_need)


class SpecialistProposalV1(_SpecialistArtifact):
    digest_field_name = "proposal_digest"
    validator = staticmethod(_validate_proposal)


class SpecialistCoverageSnapshotV1(_SpecialistArtifact):
    digest_field_name = "snapshot_digest"
    validator = staticmethod(_validate_coverage)


class CoverageCaptureError(ValueError):
    """A stable, content-safe refusal from coverage capture or compilation."""

    def __init__(self, code: str, field: str = "") -> None:
        self.code = code
        self.field = field
        super().__init__(f"{code}{(': ' + field) if field else ''}")


_COVERAGE_SCOPE_FIELDS = {
    "schema_version",
    "scope_ref",
    "description",
    "source_revision",
    "cutoff",
    "approved_root",
    "roster_root",
    "roster_source_ref",
    "sources",
    "profile_files",
    "source_profile_applicability",
    "profile_assessments",
}


def _coverage_fail(code: str, field: str = "") -> None:
    raise CoverageCaptureError(code, field)


def _coverage_file_ref(value: Any, field: str) -> None:
    _ref(value, field)
    if "/" in value or "\\" in value:
        raise ContractError("INVALID_REFERENCE", field)


def _coverage_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _coverage_fail("INVALID_SCOPE_MANIFEST")
    keys = set(value)
    if keys != _COVERAGE_SCOPE_FIELDS:
        _coverage_fail("INVALID_SCOPE_MANIFEST")
    scope = dict(value)
    if scope["schema_version"] != "1.0.0":
        _coverage_fail("INVALID_SCOPE_MANIFEST", "schema_version")
    try:
        _ref(scope["scope_ref"], "scope_ref")
        _text(scope["description"], "description")
        _text(scope["source_revision"], "source_revision")
        _text(scope["cutoff"], "cutoff")
        _ref(scope["roster_source_ref"], "roster_source_ref")
        if not isinstance(scope["approved_root"], (str, Path)):
            _coverage_fail("INVALID_SCOPE_MANIFEST", "approved_root")
        approved_root = Path(scope["approved_root"])
        if not approved_root.is_absolute() or ".." in approved_root.parts:
            _coverage_fail("INVALID_SCOPE_MANIFEST", "approved_root")
        if not isinstance(scope["roster_root"], str):
            _coverage_fail("INVALID_SCOPE_MANIFEST", "roster_root")
    except ContractError as exc:
        _coverage_fail("INVALID_SCOPE_MANIFEST", exc.field)

    sources = scope["sources"]
    profile_files = scope["profile_files"]
    applicability = scope["source_profile_applicability"]
    assessments = scope["profile_assessments"]
    if (
        not isinstance(sources, list)
        or not 1 <= len(sources) <= _SPECIALIST_MAX_ARRAY_ITEMS
        or not isinstance(profile_files, list)
        or len(profile_files) > _SPECIALIST_MAX_ARRAY_ITEMS
        or not isinstance(applicability, list)
        or len(applicability) > _SPECIALIST_MAX_ARRAY_ITEMS
        or not isinstance(assessments, list)
        or len(assessments) > _SPECIALIST_MAX_ARRAY_ITEMS
    ):
        _coverage_fail("INVALID_SCOPE_MANIFEST")
    source_refs: set[str] = set()
    file_refs: set[str] = set()
    for row in sources:
        if not isinstance(row, Mapping) or set(row) != {
            "source_ref",
            "source_kind",
            "revision",
            "cutoff",
            "file_ref",
            "relative_path",
            "profile_ids_field",
        }:
            _coverage_fail("INVALID_SCOPE_MANIFEST", "sources")
        try:
            _ref(row["source_ref"], "source_ref")
            _coverage_file_ref(row["file_ref"], "file_ref")
            _enum(row["source_kind"], _SOURCE_KINDS, "source_kind")
            _text(row["revision"], "source.revision")
            _text(row["cutoff"], "source.cutoff")
        except ContractError as exc:
            _coverage_fail("INVALID_SCOPE_MANIFEST", exc.field)
        if row["source_ref"] in source_refs or row["file_ref"] in file_refs:
            _coverage_fail("INVALID_SCOPE_MANIFEST", "sources")
        source_refs.add(row["source_ref"])
        file_refs.add(row["file_ref"])
        field = row["profile_ids_field"]
        if field is not None and (
            not isinstance(field, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", field)
        ):
            _coverage_fail("INVALID_SCOPE_MANIFEST", "profile_ids_field")
        if not isinstance(row["relative_path"], str) or not row["relative_path"]:
            _coverage_fail("INVALID_SCOPE_MANIFEST", "relative_path")
    if scope["roster_source_ref"] not in source_refs or not any(
        row["source_ref"] == scope["roster_source_ref"]
        and row["source_kind"] == "profile_registry"
        for row in sources
    ):
        _coverage_fail("INVALID_SCOPE_MANIFEST", "roster_source_ref")

    profile_file_keys: set[tuple[str, str]] = set()
    profile_file_refs: set[str] = set()
    for row in profile_files:
        if not isinstance(row, Mapping) or set(row) != {
            "profile_id",
            "source_ref",
            "file_ref",
            "relative_path",
            "format",
        }:
            _coverage_fail("INVALID_SCOPE_MANIFEST", "profile_files")
        try:
            _text(row["profile_id"], "profile_id")
            if not _SPECIALIST_PROFILE_RE.fullmatch(row["profile_id"]):
                _coverage_fail("INVALID_SCOPE_MANIFEST", "profile_id")
            _ref(row["source_ref"], "source_ref")
            _coverage_file_ref(row["file_ref"], "file_ref")
        except ContractError as exc:
            _coverage_fail("INVALID_SCOPE_MANIFEST", exc.field)
        if (
            row["source_ref"] not in source_refs
            or row["file_ref"] in file_refs
            or row["file_ref"] in profile_file_refs
            or not isinstance(row["format"], str)
            or row["format"] not in {"json", "yaml"}
            or not isinstance(row["relative_path"], str)
            or not row["relative_path"]
        ):
            _coverage_fail("INVALID_SCOPE_MANIFEST", "profile_files")
        profile_file_refs.add(row["file_ref"])
        pair = (row["profile_id"], row["source_ref"])
        if pair in profile_file_keys:
            _coverage_fail("INVALID_APPLICABILITY_MATRIX", "profile_files")
        profile_file_keys.add(pair)

    applicability_by_pair: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in applicability:
        if not isinstance(row, Mapping) or set(row) != {
            "profile_id",
            "source_ref",
            "applicability",
            "file_ref",
            "basis",
            "basis_refs",
        }:
            _coverage_fail("INVALID_APPLICABILITY_MATRIX")
        profile_id, source_ref = row["profile_id"], row["source_ref"]
        if (
            not isinstance(profile_id, str)
            or not _SPECIALIST_PROFILE_RE.fullmatch(profile_id)
            or not isinstance(source_ref, str)
            or source_ref not in source_refs
            or (profile_id, source_ref) in applicability_by_pair
        ):
            _coverage_fail("INVALID_APPLICABILITY_MATRIX")
        if not isinstance(row["applicability"], str) or row["applicability"] not in {
            "required",
            "not_applicable",
            "unresolved",
        }:
            _coverage_fail("INVALID_APPLICABILITY_MATRIX")
        try:
            _text(row["basis"], "applicability.basis")
            basis_refs = _text_list(
                row["basis_refs"],
                "applicability.basis_refs",
                refs=True,
                sorted_unique=True,
            )
        except ContractError as exc:
            _coverage_fail("INVALID_APPLICABILITY_MATRIX")
        if not basis_refs or not set(basis_refs) <= source_refs:
            _coverage_fail("INVALID_APPLICABILITY_MATRIX")
        pair = (profile_id, source_ref)
        file_row = next(
            (
                item
                for item in profile_files
                if (item["profile_id"], item["source_ref"]) == pair
            ),
            None,
        )
        if row["applicability"] == "required":
            try:
                _coverage_file_ref(row["file_ref"], "applicability.file_ref")
            except ContractError as exc:
                _coverage_fail("INVALID_APPLICABILITY_MATRIX")
            if file_row is None or file_row["file_ref"] != row["file_ref"]:
                _coverage_fail("INVALID_APPLICABILITY_MATRIX")
        elif row["file_ref"] is not None or file_row is not None:
            _coverage_fail("INVALID_APPLICABILITY_MATRIX")
        applicability_by_pair[pair] = row
    for pair in profile_file_keys:
        row = applicability_by_pair.get(pair)
        if row is None or row["applicability"] != "required":
            _coverage_fail("INVALID_APPLICABILITY_MATRIX")

    assessment_ids: set[str] = set()
    role_fields = {
        "semantic_role_id",
        "descriptor_status",
        "descriptor_ref",
        "binding_status",
        "binding_ref",
        "index_status",
        "enabled_state",
        "availability",
        "semantic_coverage",
    }
    for assessment in assessments:
        if not isinstance(assessment, Mapping) or set(assessment) != {
            "profile_id",
            "roles",
            "exclusions",
        }:
            _coverage_fail("MISSING_REVIEWED_ASSESSMENT")
        profile_id = assessment["profile_id"]
        if (
            not isinstance(profile_id, str)
            or not _SPECIALIST_PROFILE_RE.fullmatch(profile_id)
            or profile_id in assessment_ids
        ):
            _coverage_fail("MISSING_REVIEWED_ASSESSMENT")
        assessment_ids.add(profile_id)
        roles = assessment["roles"]
        exclusions = assessment["exclusions"]
        if (
            not isinstance(roles, list)
            or len(roles) > _SPECIALIST_MAX_ARRAY_ITEMS
            or not isinstance(exclusions, list)
            or len(exclusions) > _SPECIALIST_MAX_ARRAY_ITEMS
        ):
            _coverage_fail("MISSING_REVIEWED_ASSESSMENT", profile_id)
        try:
            _text_list(exclusions, "assessment.exclusions")
        except ContractError as exc:
            _coverage_fail("MISSING_REVIEWED_ASSESSMENT", profile_id)
        if not roles and not exclusions:
            _coverage_fail("MISSING_REVIEWED_ASSESSMENT", profile_id)
        role_ids: set[str] = set()
        for role in roles:
            if not isinstance(role, Mapping) or set(role) != role_fields:
                _coverage_fail("MISSING_REVIEWED_ASSESSMENT", profile_id)
            role_id = role["semantic_role_id"]
            if not isinstance(role_id, str) or not _SPECIALIST_ROLE_RE.fullmatch(
                role_id
            ):
                _coverage_fail("MISSING_REVIEWED_ASSESSMENT", profile_id)
            if role_id in role_ids:
                _coverage_fail("MISSING_REVIEWED_ASSESSMENT", profile_id)
            role_ids.add(role_id)
            try:
                _enum(
                    role["descriptor_status"],
                    {"missing", "unreadable", "malformed", "valid", "unknown"},
                    "descriptor_status",
                )
                _ref(role["descriptor_ref"], "descriptor_ref", nullable=True)
                _enum(
                    role["binding_status"],
                    {"unbound", "bound", "mismatch", "unknown"},
                    "binding_status",
                )
                _ref(role["binding_ref"], "binding_ref", nullable=True)
                _enum(
                    role["index_status"],
                    {"indexed", "unindexed", "unknown"},
                    "index_status",
                )
                _enum(
                    role["enabled_state"],
                    {"enabled", "disabled", "unknown"},
                    "enabled_state",
                )
                _enum(
                    role["availability"],
                    {
                        "available",
                        "disabled_by_owner",
                        "temporarily_unavailable",
                        "unknown",
                    },
                    "availability",
                )
                _enum(
                    role["semantic_coverage"],
                    {"covered", "not_covered", "unknown"},
                    "semantic_coverage",
                )
            except ContractError as exc:
                _coverage_fail("MISSING_REVIEWED_ASSESSMENT", profile_id)
            if (role["descriptor_status"] == "valid") != (
                role["descriptor_ref"] is not None
            ):
                _coverage_fail("MISSING_REVIEWED_ASSESSMENT", profile_id)
            if (role["binding_status"] in {"bound", "mismatch"}) != (
                role["binding_ref"] is not None
            ):
                _coverage_fail("MISSING_REVIEWED_ASSESSMENT", profile_id)

    # Validate the full matrix for every explicitly reviewed profile before
    # capture performs any reads. The compiler repeats this against the actual
    # profile union discovered in evidence to catch unlisted source identities.
    expected_assessment_pairs = {
        (profile_id, source_ref)
        for profile_id in assessment_ids
        for source_ref in source_refs
    }
    if set(applicability_by_pair) != expected_assessment_pairs:
        _coverage_fail("INVALID_APPLICABILITY_MATRIX")

    return scope


def _coverage_file_map(evidence: Any) -> dict[str, Any]:
    files = getattr(evidence, "files", None)
    if not isinstance(files, tuple):
        _coverage_fail("INVALID_SCOPE_MANIFEST", "evidence")
    result: dict[str, Any] = {}
    for item in files:
        file_ref = getattr(item, "file_ref", None)
        if not isinstance(file_ref, str) or file_ref in result:
            _coverage_fail("INVALID_SCOPE_MANIFEST", "evidence")
        result[file_ref] = item
    return result


def _coverage_state(
    record: Any, *, file_ref: str
) -> tuple[str, str | None, int | None]:
    status = getattr(record, "status", None)
    if status not in _FILE_STATUSES:
        _coverage_fail("INVALID_SCOPE_MANIFEST", file_ref)
    byte_digest = getattr(record, "byte_digest", None)
    byte_length = getattr(record, "byte_length", None)
    if status in {"missing", "unreadable"}:
        if byte_digest is not None or byte_length is not None:
            _coverage_fail("INVALID_SCOPE_MANIFEST", file_ref)
        return status, None, None
    if not isinstance(byte_digest, str) or not _SPECIALIST_SHA_RE.fullmatch(
        byte_digest
    ):
        _coverage_fail("INVALID_SCOPE_MANIFEST", file_ref)
    if (
        isinstance(byte_length, bool)
        or not isinstance(byte_length, int)
        or not 0 <= byte_length <= _SPECIALIST_MAX_SOURCE_BYTES
    ):
        _coverage_fail("INVALID_SCOPE_MANIFEST", file_ref)
    if status == "valid_populated" and byte_length == 0:
        _coverage_fail("INVALID_SCOPE_MANIFEST", file_ref)
    return status, byte_digest, byte_length


def _coverage_profile_ids(
    source: Mapping[str, Any], record: Any
) -> tuple[str, list[str]]:
    status, _byte_digest, _byte_length = _coverage_state(
        record, file_ref=source["file_ref"]
    )
    if status not in {"valid_empty", "valid_populated"}:
        return status, []
    if source["profile_ids_field"] is None:
        return status, []
    parsed = getattr(record, "parsed", None)
    field = source["profile_ids_field"]
    if not isinstance(parsed, Mapping) or field not in parsed:
        return "malformed", []
    value = parsed[field]
    if not isinstance(value, list) or len(value) > _SPECIALIST_MAX_ARRAY_ITEMS:
        return "malformed", []
    if any(
        not isinstance(profile_id, str)
        or not _SPECIALIST_PROFILE_RE.fullmatch(profile_id)
        for profile_id in value
    ):
        return "malformed", []
    if len(value) != len(set(value)):
        return "malformed", []
    return status, sorted(value)


def compile_coverage(
    scope_manifest: Mapping[str, object],
    evidence: ProfileEvidenceRead,
    *,
    capture_gaps: Sequence[str] = (),
) -> SpecialistCoverageSnapshotV1:
    """Purely compile caller-supplied scope and parsed evidence into P01 data."""
    scope = _coverage_manifest(scope_manifest)
    if (
        not isinstance(capture_gaps, (list, tuple))
        or len(capture_gaps) > _SPECIALIST_MAX_ARRAY_ITEMS
    ):
        _coverage_fail("INVALID_SCOPE_MANIFEST", "capture_gaps")
    gaps: set[str] = set()
    for gap in capture_gaps:
        try:
            _text(gap, "capture_gap")
        except ContractError as exc:
            _coverage_fail("INVALID_SCOPE_MANIFEST", exc.field)
        gaps.add(gap)
    evidence_files = _coverage_file_map(evidence)
    source_rows = {row["source_ref"]: row for row in scope["sources"]}
    expected_file_refs = {row["file_ref"] for row in scope["sources"]} | {
        row["file_ref"] for row in scope["profile_files"]
    }
    if set(evidence_files) != expected_file_refs:
        _coverage_fail("INVALID_SCOPE_MANIFEST", "evidence.files")
    expected_paths = {
        row["file_ref"]: row["relative_path"]
        for row in scope["sources"] + scope["profile_files"]
    }
    for file_ref, record in evidence_files.items():
        if getattr(record, "relative_path", None) != expected_paths[file_ref]:
            _coverage_fail("INVALID_SCOPE_MANIFEST", "evidence.relative_path")

    sources_out: list[dict[str, Any]] = []
    source_ids: dict[str, list[str]] = {}
    source_statuses: dict[str, str] = {}
    for row in sorted(scope["sources"], key=lambda item: item["source_ref"]):
        record = evidence_files[row["file_ref"]]
        status, profile_ids = _coverage_profile_ids(row, record)
        if status != getattr(record, "status", None):
            status = "malformed"
        _status, byte_digest, byte_length = _coverage_state(
            record, file_ref=row["file_ref"]
        )
        source_ids[row["source_ref"]] = profile_ids
        source_statuses[row["source_ref"]] = status
        if status not in {"valid_empty", "valid_populated"}:
            gaps.add(f"source_incomplete:{row['source_ref']}:{status}")
        sources_out.append({
            "source_ref": row["source_ref"],
            "source_kind": row["source_kind"],
            "revision": row["revision"],
            "cutoff": row["cutoff"],
            "status": status,
            "byte_digest": byte_digest,
            "byte_length": byte_length,
            "profile_ids": profile_ids,
        })

    roster_ids = sorted({
        profile_id for ids in source_ids.values() for profile_id in ids
    })
    assessment_rows: dict[str, Mapping[str, Any]] = {}
    for row in scope["profile_assessments"]:
        if not isinstance(row, Mapping) or set(row) != {
            "profile_id",
            "roles",
            "exclusions",
        }:
            _coverage_fail("MISSING_REVIEWED_ASSESSMENT")
        profile_id = row["profile_id"]
        if not isinstance(profile_id, str) or profile_id in assessment_rows:
            _coverage_fail("MISSING_REVIEWED_ASSESSMENT")
        assessment_rows[profile_id] = row
    if set(assessment_rows) != set(roster_ids):
        _coverage_fail("MISSING_REVIEWED_ASSESSMENT")

    applicability_rows: list[dict[str, Any]] = []
    applicability_by_pair: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in scope["source_profile_applicability"]:
        if not isinstance(row, Mapping) or set(row) != {
            "profile_id",
            "source_ref",
            "applicability",
            "file_ref",
            "basis",
            "basis_refs",
        }:
            _coverage_fail("INVALID_APPLICABILITY_MATRIX")
        key = (row["profile_id"], row["source_ref"])
        if key in applicability_by_pair:
            _coverage_fail("INVALID_APPLICABILITY_MATRIX")
        applicability_by_pair[key] = row
        applicability_rows.append(dict(row))
    expected_pairs = {
        (profile_id, source_ref)
        for profile_id in roster_ids
        for source_ref in source_rows
    }
    if set(applicability_by_pair) != expected_pairs:
        _coverage_fail("INVALID_APPLICABILITY_MATRIX")

    profile_file_by_pair: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in scope["profile_files"]:
        pair = (row["profile_id"], row["source_ref"])
        if pair in profile_file_by_pair:
            _coverage_fail("INVALID_APPLICABILITY_MATRIX")
        profile_file_by_pair[pair] = row
    for pair, row in applicability_by_pair.items():
        if row["applicability"] == "required":
            file_row = profile_file_by_pair.get(pair)
            if file_row is None or file_row["file_ref"] != row["file_ref"]:
                _coverage_fail("INVALID_APPLICABILITY_MATRIX")
        elif row["applicability"] in {"not_applicable", "unresolved"}:
            if row["file_ref"] is not None or pair in profile_file_by_pair:
                _coverage_fail("INVALID_APPLICABILITY_MATRIX")
            if row["applicability"] == "unresolved":
                gaps.add(f"applicability_unresolved:{pair[0]}:{pair[1]}")
        else:
            _coverage_fail("INVALID_APPLICABILITY_MATRIX")

    roster_read_ids = tuple(getattr(evidence, "roster_ids", ()))
    roster_identity = tuple(getattr(evidence, "roster_identity", ()))
    registry_ids = source_ids.get(scope["roster_source_ref"], [])
    if sorted(roster_read_ids) != registry_ids:
        gaps.add("profile_registry_roster_mismatch")
    recognized = set(roster_read_ids)
    if any(
        not isinstance(row, tuple) or len(row) != 3 or row[0] not in recognized
        for row in roster_identity
    ):
        gaps.add("roster_contains_unclassified_entries")
    if len(roster_identity) > _SPECIALIST_MAX_ARRAY_ITEMS:
        gaps.add("roster_identity_truncated_or_oversized")

    roster_out: list[dict[str, Any]] = []
    for profile_id in roster_ids:
        assessment = assessment_rows[profile_id]
        roles = assessment["roles"]
        exclusions = assessment["exclusions"]
        if not isinstance(roles, list) or not isinstance(exclusions, list):
            _coverage_fail("MISSING_REVIEWED_ASSESSMENT", profile_id)
        try:
            exclusions = _text_list(exclusions, "exclusions")
        except ContractError as exc:
            _coverage_fail("MISSING_REVIEWED_ASSESSMENT", profile_id)
        if not roles and not exclusions:
            _coverage_fail("MISSING_REVIEWED_ASSESSMENT", profile_id)
        profile_source_files: list[dict[str, Any]] = []
        for row in scope["profile_files"]:
            if row["profile_id"] != profile_id:
                continue
            record = evidence_files[row["file_ref"]]
            status, byte_digest, byte_length = _coverage_state(
                record, file_ref=row["file_ref"]
            )
            if status not in {"valid_empty", "valid_populated"}:
                gaps.add(
                    f"profile_source_incomplete:{profile_id}:{row['source_ref']}:{status}"
                )
            profile_source_files.append({
                "source_ref": row["source_ref"],
                "file_ref": row["file_ref"],
                "status": status,
                "byte_digest": byte_digest,
                "byte_length": byte_length,
            })
        normalized_roles: list[dict[str, Any]] = []
        for role in roles:
            if not isinstance(role, Mapping):
                _coverage_fail("MISSING_REVIEWED_ASSESSMENT", profile_id)
            normalized = dict(role)
            normalized_roles.append(normalized)
        roster_out.append({
            "profile_id": profile_id,
            "source_files": sorted(
                profile_source_files,
                key=lambda item: (item["source_ref"], item["file_ref"]),
            ),
            "roles": sorted(
                normalized_roles, key=lambda item: item.get("semantic_role_id", "")
            ),
            "exclusions": sorted(set(exclusions)),
        })

    sorted_applicability = sorted(
        applicability_rows, key=lambda item: (item["profile_id"], item["source_ref"])
    )
    sorted_gaps = sorted(gaps)
    if sorted_gaps:
        # Gaps may be discovered while visiting later profiles. Normalize all
        # role facts only after the final completeness state is known so no
        # earlier profile can retain a stale absence claim.
        for profile in roster_out:
            for role in profile["roles"]:
                if role["semantic_coverage"] == "not_covered":
                    role["semantic_coverage"] = "unknown"
    completeness = "incomplete" if sorted_gaps else "complete_within_declared_sources"
    payload: dict[str, Any] = {
        "schema_version": "1.0.0",
        "snapshot_id": f"snapshot:{scope['scope_ref']}:{scope['source_revision']}:{scope['cutoff']}",
        "source_revision": scope["source_revision"],
        "cutoff": scope["cutoff"],
        "declared_scope": {
            "scope_ref": scope["scope_ref"],
            "description": scope["description"],
            "source_refs": sorted(source_rows),
            "completeness": completeness,
            "gaps": sorted_gaps,
            "source_profile_applicability": sorted_applicability,
        },
        "sources": sources_out,
        "roster_manifest": roster_out,
        "roster_identity_digest": digest(roster_ids),
    }
    try:
        return SpecialistCoverageSnapshotV1.create(payload)
    except ContractError as exc:
        if exc.code == "SOURCE_PROFILE_MATRIX_MISMATCH":
            _coverage_fail("INVALID_APPLICABILITY_MATRIX", exc.field)
        _coverage_fail("INVALID_SCOPE_MANIFEST", exc.field)


def capture_coverage_snapshot(
    scope_manifest: Mapping[str, object],
) -> SpecialistCoverageSnapshotV1:
    """Capture one explicit safe scope twice and return inert P01 coverage."""
    scope = _coverage_manifest(scope_manifest)
    reader_manifest = {
        "roster_root": scope["roster_root"],
        "files": [
            {
                "file_ref": row["file_ref"],
                "relative_path": row["relative_path"],
                "format": "json",
            }
            for row in scope["sources"]
        ]
        + [
            {
                "file_ref": row["file_ref"],
                "relative_path": row["relative_path"],
                "format": row["format"],
            }
            for row in scope["profile_files"]
        ],
    }
    try:
        from hermes_cli import profiles

        first = profiles.read_profile_evidence(
            Path(scope["approved_root"]), reader_manifest
        )
        second = profiles.read_profile_evidence(
            Path(scope["approved_root"]), reader_manifest
        )
    except Exception as exc:
        from hermes_cli.profiles import ProfileEvidenceReadError

        if isinstance(exc, ProfileEvidenceReadError):
            raise CoverageCaptureError("INVALID_SCOPE_MANIFEST") from exc
        raise

    if (
        first.roster_ids != second.roster_ids
        or first.roster_identity != second.roster_identity
    ):
        _coverage_fail("ROSTER_DRIFT")
    first_files = {item.file_ref: item for item in first.files}
    second_files = {item.file_ref: item for item in second.files}
    if set(first_files) != set(second_files):
        _coverage_fail("SOURCE_DRIFT")
    for file_ref in first_files:
        left, right = first_files[file_ref], second_files[file_ref]
        if (
            left.status != right.status
            or left.byte_digest != right.byte_digest
            or left.byte_length != right.byte_length
            or left.identity != right.identity
        ):
            _coverage_fail("SOURCE_DRIFT", file_ref)
    return compile_coverage(scope, first)


@dataclass(frozen=True)
class SpecialistDecisionReceiptV1:
    """A stable, content-addressed non-dispatch decision projection."""

    payload: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return dict(self.payload)


def _finalize(payload: dict[str, object]) -> SpecialistDecisionReceiptV1:
    receipt = dict(payload)
    receipt["decision_digest"] = digest(receipt)
    return SpecialistDecisionReceiptV1(receipt)


def decide_specialist_nonactivation(
    *,
    request_id: str,
    request_digest: str,
    requested_capability: str | None,
    candidates: Sequence[SpecialistDescriptorV1],
    profile_binding_refs: Mapping[str, str],
    policy_version: str,
) -> SpecialistDecisionReceiptV1:
    """Return a deterministic no-dispatch decision for a frozen candidate set.

    A matching descriptor remains blocked while it is disabled. This function
    cannot return a selected profile: that transition needs separately
    authorized value evidence plus Electron-owned admission, neither of which
    is supplied here.
    """
    if not isinstance(request_id, str) or not request_id:
        raise ContractError("INVALID_REQUEST", "request_id")
    if not isinstance(request_digest, str) or not request_digest.startswith("sha256:"):
        raise ContractError("INVALID_REQUEST", "request_digest")
    if not isinstance(policy_version, str) or not policy_version:
        raise ContractError("INVALID_POLICY", "policy_version")
    ordered = sorted(
        candidates, key=lambda candidate: str(candidate.to_dict()["profile_id"])
    )
    profile_ids = [str(candidate.to_dict()["profile_id"]) for candidate in ordered]
    if len(profile_ids) != len(set(profile_ids)):
        raise ContractError("DUPLICATE_PROFILE", "candidates")

    requested = (requested_capability or "").strip()
    base: dict[str, object] = {
        "schema": DECISION_SCHEMA,
        "request_id": request_id,
        "request_digest": request_digest,
        "policy_version": policy_version,
        "candidate_profile_ids": profile_ids,
        "selected_profile_id": None,
        "dispatch_authorized": False,
    }
    if not requested:
        return _finalize({
            **base,
            "outcome": "no_specialist_needed",
            "reason_code": "NO_MATERIAL_EVIDENCE_GAP",
            "candidate_rejections": [],
        })

    rejections: list[dict[str, str]] = []
    for candidate in ordered:
        descriptor = candidate.to_dict()
        profile_id = str(descriptor["profile_id"])
        binding = profile_binding_refs.get(profile_id)
        if binding != specialist_descriptor_ref(candidate):
            reason = "PROFILE_BINDING_MISMATCH"
        elif requested not in descriptor["capability_classes"]:
            reason = "CAPABILITY_NOT_MATCHED"
        elif descriptor["enabled"] is not False:
            reason = "NONACTIVATION_POLICY_DENIED"
        else:
            reason = "DESCRIPTOR_DISABLED"
        rejections.append({"profile_id": profile_id, "reason_code": reason})

    return _finalize({
        **base,
        "outcome": "blocked",
        "reason_code": "NO_DISPATCH_AUTHORITY",
        "requested_capability": requested,
        "candidate_rejections": rejections,
    })


class SpecialistCompilationError(ValueError):
    """A typed, inert refusal from P03 need/proposal compilation."""

    def __init__(self, code: str, field: str = "") -> None:
        self.code = code
        self.field = field
        super().__init__(f"{code}{(': ' + field) if field else ''}")


_P03_NEED_INPUT_FIELDS = {
    "task_id",
    "task_ref",
    "task_digest",
    "obligation_id",
    "obligation_text",
    "affected_decision",
    "consequence",
    "evidence_refs",
    "approved_input_manifest",
    "alternatives",
    "reviewer_assessments",
}
_P03_ASSESSMENT_FIELDS = {
    "protocol_digest",
    "proposed_responsibilities",
    "proposed_methods",
    "explicit_exclusions",
    "draft_soul",
    "curated_skill_refs",
    "requested_input_classes",
    "requested_tool_classes",
    "requested_model_class",
    "output_schema_ref",
    "output_schema_digest",
    "method_evidence_refs",
    "comparator_residual",
    "comparator_evidence_refs",
    "expected_checkable_output",
    "falsifiers",
}
_P03_REVIEW_VECTOR_FIELDS = (
    "materiality",
    "direct_sufficiency",
    "semantic_coverage",
    "substantive_method_coverage",
    "independent_pass_requirement",
    "method_feasibility",
    "disposition",
)
_P03_TRIAL_BLOCKERS = [
    "ACCOUNT_OWNER_REVIEW_REQUIRED",
    "BUDGET_APPROVAL_ABSENT",
    "CONTAINMENT_APPROVAL_ABSENT",
    "TRIAL_POLICY_ABSENT",
]
_P03_AUTHORITY_PARTS = {
    "credential",
    "credentials",
    "auth",
    "oauth",
    "oauth2",
    "bearer",
    "apikey",
    "password",
    "secret",
    "secrets",
    "factory",
    "dispatch",
    "activate",
    "activation",
    "execute",
    "execution",
    "permit",
    "trial",
}


def _p03_fail(code: str, field: str = "") -> None:
    raise SpecialistCompilationError(code, field)


def _p03_exact_object(
    value: Any, expected: set[str], *, field: str, unknown_code: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _p03_fail("INVALID_OBJECT", field)
    keys = set(value)
    unknown = keys - expected
    missing = expected - keys
    if unknown:
        _p03_fail(unknown_code, sorted(unknown)[0])
    if missing:
        _p03_fail("MISSING_FIELD", f"{field}.{sorted(missing)[0]}")
    result = dict(value)
    _check_specialist_tree(result)
    return result


def _p03_need_artifact(
    value: SpecialistNeedV1 | Mapping[str, Any],
) -> SpecialistNeedV1:
    if isinstance(value, SpecialistNeedV1):
        return value
    if not isinstance(value, Mapping):
        _p03_fail("INVALID_NEED")
    return SpecialistNeedV1.parse(value)


def _p03_coverage_artifact(
    value: SpecialistCoverageSnapshotV1 | Mapping[str, Any],
) -> SpecialistCoverageSnapshotV1:
    if isinstance(value, SpecialistCoverageSnapshotV1):
        return value
    if not isinstance(value, Mapping):
        _p03_fail("INVALID_COVERAGE")
    return SpecialistCoverageSnapshotV1.parse(value)


def _p03_authority_token(value: str) -> bool:
    token = re.sub(r"[^a-z0-9]+", "_", value.strip().casefold()).strip("_")
    parts = set(token.split("_"))
    if parts & _P03_AUTHORITY_PARTS:
        return True
    if (
        ({"api", "key"} <= parts)
        or ({"access", "token"} <= parts)
        or ({"refresh", "token"} <= parts)
        or ("account" in parts and "access" in parts)
        or ("profile" in parts and parts & {"create", "creation"})
        or ("registry" in parts and "write" in parts)
    ):
        return True
    return token in {"home_scan", "broad_home_read", "broad_home_access"}


def _p03_review_state(
    need: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    rows = need["reviewer_assessments"]
    if not rows:
        return None, "REVIEWER_ASSESSMENT_ABSENT"
    vectors = {tuple(row[field] for field in _P03_REVIEW_VECTOR_FIELDS) for row in rows}
    if len(vectors) != 1:
        return None, "REVIEWER_DISAGREEMENT"
    agreed = dict(rows[0])
    agreed["uncertainty"] = [item for row in rows for item in row["uncertainty"]]
    return agreed, None


def _p03_role_facts(coverage: Mapping[str, Any]) -> list[dict[str, Any]]:
    roles = [
        role for profile in coverage["roster_manifest"] for role in profile["roles"]
    ]
    return sorted(roles, key=lambda role: role["semantic_role_id"])


def _p03_roles_ready(roles: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        role["descriptor_status"] == "valid"
        and role["binding_status"] == "bound"
        and role["index_status"] == "indexed"
        and role["enabled_state"] == "enabled"
        and role["availability"] == "available"
        for role in roles
    )


def _p03_role_known_blocked(role: Mapping[str, Any]) -> bool:
    return (
        role["descriptor_status"] in {"missing", "unreadable", "malformed"}
        or role["binding_status"] in {"unbound", "mismatch"}
        or role["index_status"] == "unindexed"
        or role["enabled_state"] == "disabled"
        or role["availability"] in {"disabled_by_owner", "temporarily_unavailable"}
    )


def _p03_family_digest(need: Mapping[str, Any], responsibilities: Sequence[str]) -> str:
    normalized = sorted({
        " ".join(value.casefold().split()) for value in responsibilities
    })
    return digest({
        "task_id": need["task_id"],
        "task_ref": need["task_ref"],
        "obligation_id": need["obligation_id"],
        "domain_boundary": normalized,
    })


def _p03_lineage_pair(
    pair: Mapping[str, Any],
) -> tuple[SpecialistNeedV1, SpecialistProposalV1]:
    if not isinstance(pair, Mapping) or set(pair) != {"need", "proposal"}:
        _p03_fail("INVALID_LINEAGE_ENTRY")
    need = _p03_need_artifact(pair["need"])
    proposal_value = pair["proposal"]
    if isinstance(proposal_value, SpecialistProposalV1):
        proposal = proposal_value
    elif isinstance(proposal_value, Mapping):
        proposal = SpecialistProposalV1.parse(proposal_value)
    else:
        _p03_fail("INVALID_LINEAGE_PROPOSAL")
    proposal_values = proposal.to_dict()
    if (
        proposal_values["need_digest"] != need.artifact_digest
        or proposal_values["coverage_snapshot_digest"]
        != need.to_dict()["coverage_snapshot_digest"]
    ):
        _p03_fail("LINEAGE_CROSS_LINK_MISMATCH")
    return need, proposal


def _p03_revision_digest(proposal: Mapping[str, Any]) -> str:
    material = dict(proposal)
    material.pop("proposal_digest", None)
    material.pop("supersedes_proposal_ref", None)
    return digest(material)


def compile_specialist_need(
    *,
    fields: Mapping[str, Any],
    coverage: SpecialistCoverageSnapshotV1 | Mapping[str, Any],
) -> SpecialistNeedV1:
    """Bind caller-supplied P01 need facts to one exact coverage artifact."""
    supplied = _p03_exact_object(
        fields,
        _P03_NEED_INPUT_FIELDS,
        field="fields",
        unknown_code="UNKNOWN_NEED_FIELD",
    )
    coverage_artifact = _p03_coverage_artifact(coverage)
    need_artifact = SpecialistNeedV1.create({
        "schema_version": "1.0.0",
        **supplied,
        "coverage_snapshot_digest": coverage_artifact.artifact_digest,
    })
    validated = need_artifact.to_dict()
    evidence_refs = set(validated["evidence_refs"])
    for row in validated["reviewer_assessments"]:
        if not set(row["evidence_refs"]) <= evidence_refs:
            _p03_fail("REVIEW_EVIDENCE_NOT_BOUND", row["reviewer_ref"])
    return need_artifact


def compile_specialist_proposal(
    *,
    need: SpecialistNeedV1 | Mapping[str, Any],
    coverage: SpecialistCoverageSnapshotV1 | Mapping[str, Any],
    assessment: Mapping[str, Any],
    prior_lineage: Sequence[Mapping[str, Any]] = (),
    presentation_title: str | None = None,
) -> SpecialistProposalV1:
    """Compile only reviewed inert facts; never discover or activate a role."""
    need_artifact = _p03_need_artifact(need)
    coverage_artifact = _p03_coverage_artifact(coverage)
    need_values = need_artifact.to_dict()
    coverage_values = coverage_artifact.to_dict()
    if need_values["coverage_snapshot_digest"] != coverage_artifact.artifact_digest:
        _p03_fail("COVERAGE_DIGEST_MISMATCH", "coverage_snapshot_digest")

    source = _p03_exact_object(
        assessment,
        _P03_ASSESSMENT_FIELDS,
        field="assessment",
        unknown_code="UNKNOWN_ASSESSMENT_FIELD",
    )
    if presentation_title is not None:
        try:
            _text(presentation_title, "presentation_title")
        except ContractError as exc:
            _p03_fail(exc.code, exc.field)
    try:
        _sha(source["protocol_digest"], "protocol_digest")
        for field in (
            "proposed_responsibilities",
            "proposed_methods",
            "explicit_exclusions",
            "requested_input_classes",
            "requested_tool_classes",
            "falsifiers",
        ):
            _text_list(source[field], field)
        _text(source["draft_soul"], "draft_soul", empty=True)
        _text(source["comparator_residual"], "comparator_residual", empty=True)
        if source["expected_checkable_output"] is not None:
            _text(
                source["expected_checkable_output"],
                "expected_checkable_output",
            )
        _text_list(
            source["method_evidence_refs"],
            "method_evidence_refs",
            refs=True,
            sorted_unique=True,
        )
        _text_list(
            source["comparator_evidence_refs"],
            "comparator_evidence_refs",
            refs=True,
            sorted_unique=True,
        )
    except ContractError as exc:
        _p03_fail(exc.code, exc.field)

    requested_classes = (
        source["requested_input_classes"] + source["requested_tool_classes"]
    )
    if any(_p03_authority_token(value) for value in requested_classes):
        _p03_fail("AUTHORITY_TOOL_CLASS_REFUSED", "requested_classes")

    approved_manifest_refs = {
        row["artifact_ref"] for row in need_values["approved_input_manifest"]
    }
    if not set(source["method_evidence_refs"]) <= approved_manifest_refs:
        _p03_fail("UNAPPROVED_EVIDENCE_REF", "method_evidence_refs")
    if not set(source["comparator_evidence_refs"]) <= approved_manifest_refs:
        _p03_fail("UNAPPROVED_EVIDENCE_REF", "comparator_evidence_refs")

    reviewers, review_error = _p03_review_state(need_values)
    all_roles = _p03_role_facts(coverage_values)
    role_refs = sorted({role["semantic_role_id"] for role in all_roles})
    disposition = "evidence_blocked"
    reasons: list[str] = []
    comparator: dict[str, Any] | None = None
    expected_output = source["expected_checkable_output"]
    falsifiers = list(source["falsifiers"])
    trial_readiness = "not_assessed"
    trial_blockers: list[str] = []

    if review_error:
        disposition = "independent_review_needed"
        reasons = [review_error]
    elif reviewers is not None:
        materiality = reviewers["materiality"]
        direct_sufficiency = reviewers["direct_sufficiency"]
        semantic_coverage = reviewers["semantic_coverage"]
        method_coverage = reviewers["substantive_method_coverage"]
        independent_pass = reviewers["independent_pass_requirement"]
        method_feasibility = reviewers["method_feasibility"]
        snapshot_complete = (
            coverage_values["declared_scope"]["completeness"]
            == "complete_within_declared_sources"
        )
        covered_roles = [
            role for role in all_roles if role["semantic_coverage"] == "covered"
        ]
        sufficient_direct_alt = any(
            row["kind"] in {"direct_evidence", "procedure", "skill"}
            and row["assessment"] == "sufficient"
            for row in need_values["alternatives"]
        )

        if materiality == "not_material":
            disposition = "no_material_gap"
            reasons = ["NO_WITNESSED_CONSEQUENTIAL_UNRESOLVED_OBLIGATION"]
        elif materiality == "unknown":
            disposition = "evidence_blocked"
            reasons = ["MATERIALITY_UNRESOLVED"]
        elif not need_values["evidence_refs"]:
            disposition = "evidence_blocked"
            reasons = ["TASK_EVIDENCE_MISSING"]
        elif direct_sufficiency == "sufficient" or sufficient_direct_alt:
            disposition = "direct_evidence_or_skill_sufficient"
            reasons = ["DIRECT_EVIDENCE_OR_SKILL_SUFFICIENT"]
        elif semantic_coverage == "unknown" or not snapshot_complete:
            disposition = "coverage_unknown"
            reasons = [
                "COVERAGE_ASSESSMENT_UNKNOWN"
                if semantic_coverage == "unknown"
                else "COVERAGE_SNAPSHOT_INCOMPLETE"
            ]
        elif any(role["semantic_coverage"] == "unknown" for role in all_roles):
            disposition = "coverage_unknown"
            reasons = ["ROLE_FACT_COVERAGE_UNKNOWN"]
        elif semantic_coverage == "covered":
            if not covered_roles:
                disposition = "independent_review_needed"
                reasons = ["ROLE_COVERAGE_REVIEW_MISMATCH"]
            elif _p03_roles_ready(covered_roles):
                disposition = "existing_role_covers_need"
                reasons = ["REVIEWED_ROLE_COVERS_NEED"]
            elif all(_p03_role_known_blocked(role) for role in covered_roles):
                disposition = "existing_role_unindexed_or_unavailable"
                reasons = ["REVIEWED_ROLE_COVERAGE_UNAVAILABLE"]
            else:
                disposition = "coverage_unknown"
                reasons = ["ROLE_AVAILABILITY_UNKNOWN"]
        elif covered_roles:
            disposition = "independent_review_needed"
            reasons = ["ROLE_COVERAGE_REVIEW_MISMATCH"]
        elif reviewers["uncertainty"]:
            disposition = "independent_review_needed"
            reasons = ["REVIEWER_UNCERTAINTY_REMAINS"]
        elif direct_sufficiency == "unknown":
            disposition = "independent_review_needed"
            reasons = ["DIRECT_SUFFICIENCY_UNRESOLVED"]
        elif independent_pass != "not_required":
            disposition = "independent_review_needed"
            reasons = [
                "INDEPENDENT_PASS_REQUIRED"
                if independent_pass == "required"
                else "INDEPENDENT_PASS_REQUIREMENT_UNKNOWN"
            ]
        elif method_coverage == "unknown":
            disposition = "coverage_unknown"
            reasons = ["SUBSTANTIVE_METHOD_COVERAGE_UNKNOWN"]
        elif method_coverage == "covered":
            disposition = "independent_review_needed"
            reasons = ["METHOD_COVERAGE_REVIEW_MISMATCH"]
        elif method_feasibility != "specified":
            disposition = "evidence_blocked"
            reasons = ["METHOD_FEASIBILITY_UNDEFINED"]
        elif not need_values["alternatives"]:
            disposition = "evidence_blocked"
            reasons = ["COMPARATOR_ABSENT"]
        else:
            strongest = need_values["alternatives"][0]
            if strongest["assessment"] != "insufficient":
                disposition = "evidence_blocked"
                reasons = ["COMPARATOR_UNRESOLVED"]
            elif strongest["kind"] == "independent_pass":
                disposition = "independent_review_needed"
                reasons = ["INDEPENDENT_PASS_IS_STRONGEST_ALTERNATIVE"]
            else:
                missing_differential: list[str] = []
                if not source["proposed_responsibilities"]:
                    missing_differential.append("RESPONSIBILITY_BOUNDARY_MISSING")
                if (
                    not source["proposed_methods"]
                    or not source["method_evidence_refs"]
                    or not set(source["method_evidence_refs"])
                    <= set(need_values["evidence_refs"])
                ):
                    missing_differential.append("METHOD_EVIDENCE_MISSING")
                if not source["explicit_exclusions"]:
                    missing_differential.append("EXPLICIT_EXCLUSIONS_MISSING")
                if not source["comparator_residual"]:
                    missing_differential.append("COMPARATOR_RESIDUAL_MISSING")
                if (
                    not source["comparator_evidence_refs"]
                    or not set(source["comparator_evidence_refs"])
                    <= set(need_values["evidence_refs"])
                    or not set(strongest["evidence_refs"])
                    <= set(source["comparator_evidence_refs"])
                ):
                    missing_differential.append("COMPARATOR_EVIDENCE_MISSING")
                if not expected_output:
                    missing_differential.append("CHECKABLE_OUTPUT_MISSING")
                if not falsifiers:
                    missing_differential.append("FALSIFIER_MISSING")
                if missing_differential:
                    disposition = "evidence_blocked"
                    reasons = ["DIFFERENTIAL_METHOD_INCOMPLETE"]
                else:
                    disposition = "candidate_proposal"
                    reasons = ["MATERIAL_UNCOVERED_DIFFERENTIAL_METHOD"]
                    comparator = {
                        "alternative_ref": strongest["alternative_ref"],
                        "residual": source["comparator_residual"],
                        "evidence_refs": source["comparator_evidence_refs"],
                    }
                    trial_readiness = "blocked"
                    trial_blockers = list(_P03_TRIAL_BLOCKERS)

    # The strict P01 ProposalV1 shape has no assessment field. Preserve the
    # method citation explicitly and bind every assessment field that may be
    # omitted from a non-candidate summary into the revision identity.
    reasons = [
        *reasons,
        "METHOD_EVIDENCE_REFS:" + ",".join(source["method_evidence_refs"]),
        "ASSESSMENT_DIGEST:" + digest(source),
    ]

    proposal_values: dict[str, Any] = {
        "schema_version": "1.0.0",
        "need_digest": need_artifact.artifact_digest,
        "coverage_snapshot_digest": coverage_artifact.artifact_digest,
        "protocol_digest": source["protocol_digest"],
        "disposition": disposition,
        "reasons": reasons,
        "role_refs_considered": role_refs,
        "proposed_responsibilities": source["proposed_responsibilities"],
        "proposed_methods": source["proposed_methods"],
        "explicit_exclusions": source["explicit_exclusions"],
        "draft_soul": source["draft_soul"],
        "curated_skill_refs": source["curated_skill_refs"],
        "requested_input_classes": source["requested_input_classes"],
        "requested_tool_classes": source["requested_tool_classes"],
        "requested_model_class": source["requested_model_class"],
        "output_schema_ref": source["output_schema_ref"],
        "output_schema_digest": source["output_schema_digest"],
        "comparator": comparator,
        "expected_checkable_output": (
            expected_output if disposition == "candidate_proposal" else None
        ),
        "falsifiers": falsifiers if disposition == "candidate_proposal" else [],
        "trial_readiness": trial_readiness,
        "trial_blockers": trial_blockers,
        "supersedes_proposal_ref": None,
    }

    # The P01 artifact type remains the source of truth for every serialized
    # ProposalV1 shape, digest, size, and enum invariant.
    proposal_values = SpecialistProposalV1.create(proposal_values).to_dict()

    if not isinstance(prior_lineage, Sequence) or isinstance(
        prior_lineage, (str, bytes)
    ):
        _p03_fail("INVALID_LINEAGE")
    parsed_lineage: list[tuple[SpecialistNeedV1, SpecialistProposalV1]] = []
    seen_digests: set[str] = set()
    current_family = _p03_family_digest(
        need_values, proposal_values["proposed_responsibilities"]
    )
    for index, pair in enumerate(prior_lineage):
        prior_need, prior_proposal = _p03_lineage_pair(pair)
        prior_values = prior_proposal.to_dict()
        if prior_proposal.artifact_digest in seen_digests:
            _p03_fail("LINEAGE_DUPLICATE")
        seen_digests.add(prior_proposal.artifact_digest)
        prior_family = _p03_family_digest(
            prior_need.to_dict(), prior_values["proposed_responsibilities"]
        )
        if prior_family != current_family:
            _p03_fail("LINEAGE_FAMILY_MISMATCH")
        expected_predecessor = (
            None
            if index == 0
            else "proposal:"
            + parsed_lineage[-1][1].artifact_digest.removeprefix("sha256:")
        )
        if prior_values["supersedes_proposal_ref"] != expected_predecessor:
            _p03_fail("LINEAGE_INCOMPLETE" if index == 0 else "LINEAGE_CONFLICT")
        parsed_lineage.append((prior_need, prior_proposal))

    if parsed_lineage:
        previous = parsed_lineage[-1][1]
        if _p03_revision_digest(previous.to_dict()) == _p03_revision_digest(
            proposal_values
        ):
            return previous
        proposal_values["supersedes_proposal_ref"] = (
            "proposal:" + previous.artifact_digest.removeprefix("sha256:")
        )
    return SpecialistProposalV1.create(proposal_values)
