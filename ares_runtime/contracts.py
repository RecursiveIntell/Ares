"""Versioned, canonical CandidateStore authorization contracts for Ares."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import AresRuntimeError

ACTIVATION_GRANT_SCHEMA = "AresActivationGrantV1"


def canonical_json(value: Any) -> bytes:
    """Encode a contract as newline-terminated canonical JSON."""

    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise AresRuntimeError("CONTRACT_NOT_CANONICAL", str(exc)) from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise AresRuntimeError("INVALID_IDENTITY", field)
    if set(value) - set("0123456789abcdef"):
        raise AresRuntimeError("INVALID_IDENTITY", field)
    return value


def require_absolute_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise AresRuntimeError("INVALID_PATH", field)
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise AresRuntimeError("INVALID_PATH", field)
    return str(path)


def _require_nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AresRuntimeError("INVALID_CONTRACT", field)
    return value


def _strict_object(raw: bytes) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise AresRuntimeError("DUPLICATE_JSON_KEY", key)
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AresRuntimeError("INVALID_JSON", str(exc)) from exc
    if not isinstance(value, dict):
        raise AresRuntimeError("INVALID_CONTRACT", "root must be an object")
    if canonical_json(value) != raw:
        raise AresRuntimeError("CONTRACT_NOT_CANONICAL")
    return value


@dataclass(frozen=True)
class ActivationGrant:
    """CandidateStore-issued authorization for one exact installed release."""

    candidate_id: str
    certification_set_id: str
    sealed_candidate_id: str
    audit_subject_id: str
    audit_subject_sha256: str
    audit_result_sha256: str
    archive_sha256: str
    candidate_core_sha256: str
    sealed_manifest_sha256: str
    release_manifest_sha256: str
    runtime_tree_sha256: str
    custody_event_sequence: int
    target_platform: str
    target_release_root: str
    materializer_contract: str
    activator_contract: str
    resolver_contract: str
    grant_id: str | None = None

    def __post_init__(self) -> None:
        for field in (
            "candidate_id",
            "certification_set_id",
            "sealed_candidate_id",
            "audit_subject_id",
            "audit_subject_sha256",
            "audit_result_sha256",
            "archive_sha256",
            "candidate_core_sha256",
            "sealed_manifest_sha256",
            "release_manifest_sha256",
            "runtime_tree_sha256",
        ):
            require_sha256(getattr(self, field), field)
        if (
            not isinstance(self.custody_event_sequence, int)
            or isinstance(self.custody_event_sequence, bool)
            or self.custody_event_sequence < 1
        ):
            raise AresRuntimeError("INVALID_GRANT", "custody_event_sequence")
        _require_nonempty_string(self.target_platform, "target_platform")
        require_absolute_path(self.target_release_root, "target_release_root")
        for field in (
            "materializer_contract",
            "activator_contract",
            "resolver_contract",
        ):
            _require_nonempty_string(getattr(self, field), field)
        if self.grant_id is not None:
            require_sha256(self.grant_id, "grant_id")
            if self.grant_id != self.computed_grant_id():
                raise AresRuntimeError("INVALID_GRANT", "grant_id")

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema": ACTIVATION_GRANT_SCHEMA,
            "candidate_id": self.candidate_id,
            "certification_set_id": self.certification_set_id,
            "sealed_candidate_id": self.sealed_candidate_id,
            "audit_subject_id": self.audit_subject_id,
            "audit_subject_sha256": self.audit_subject_sha256,
            "audit_result": "PASS",
            "audit_result_sha256": self.audit_result_sha256,
            "archive_sha256": self.archive_sha256,
            "candidate_core_sha256": self.candidate_core_sha256,
            "sealed_manifest_sha256": self.sealed_manifest_sha256,
            "release_manifest_sha256": self.release_manifest_sha256,
            "runtime_tree_sha256": self.runtime_tree_sha256,
            "custody_event_sequence": self.custody_event_sequence,
            "target_platform": self.target_platform,
            "target_release_root": self.target_release_root,
            "materializer_contract": self.materializer_contract,
            "activator_contract": self.activator_contract,
            "resolver_contract": self.resolver_contract,
        }

    def computed_grant_id(self) -> str:
        return sha256_bytes(canonical_json(self.unsigned_dict()))

    def with_grant_id(self) -> "ActivationGrant":
        values = dict(self.__dict__)
        values["grant_id"] = self.computed_grant_id()
        return ActivationGrant(**values)

    def to_dict(self) -> dict[str, Any]:
        if self.grant_id is None:
            raise AresRuntimeError("INVALID_GRANT", "grant_id missing")
        return {**self.unsigned_dict(), "grant_id": self.grant_id}

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_dict())

    @classmethod
    def parse(cls, raw: bytes) -> "ActivationGrant":
        value = _strict_object(raw)
        expected = {
            "schema",
            "candidate_id",
            "certification_set_id",
            "sealed_candidate_id",
            "audit_subject_id",
            "audit_subject_sha256",
            "audit_result",
            "audit_result_sha256",
            "archive_sha256",
            "candidate_core_sha256",
            "sealed_manifest_sha256",
            "release_manifest_sha256",
            "runtime_tree_sha256",
            "custody_event_sequence",
            "target_platform",
            "target_release_root",
            "materializer_contract",
            "activator_contract",
            "resolver_contract",
            "grant_id",
        }
        if set(value) != expected or value.get("schema") != ACTIVATION_GRANT_SCHEMA:
            raise AresRuntimeError("INVALID_GRANT", "schema or fields")
        if value.get("audit_result") != "PASS":
            raise AresRuntimeError("INVALID_GRANT", "audit_result")
        value = dict(value)
        value.pop("schema")
        value.pop("audit_result")
        return cls(**value)
