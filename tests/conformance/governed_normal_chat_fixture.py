"""Ares-owned fixture material for cross-repo governed normal-chat conformance.

This module validates Ares semantics and emits only explicit, nonauthorizing
refs/digests for an external recursive-agent test. It does not grant provider
authority, resolve credentials, or execute a model call.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ares_runtime.collaboration import canonical_json
from ares_runtime.managed_calls import ManagedCallContextV1

FIXTURE_SCHEMA = "ares.governed-normal-chat-conformance/v1"
CONVERSATION_REF = "conversation:ares-cross-repo-conformance-v1"


def context_values(ares_revision: str) -> dict[str, Any]:
    if len(ares_revision) != 40 or any(ch not in "0123456789abcdef" for ch in ares_revision):
        raise ValueError("ares_revision must be one lowercase 40-hex commit")
    return {
        "schema_version": "ares.managed-call-context/v1",
        "call_ref": "call:cross-repo-conformance-v1",
        "parent_call_ref": None,
        "session_ref": "session:cross-repo-conformance-v1",
        "branch_ref": "branch:main",
        "turn_ref": "turn:1",
        "transcript_generation": 1,
        "surface": "normal_chat",
        "purpose": "conversation",
        "inbound_event_ref": "event:cross-repo-conformance-v1",
        "inbound_event_digest": "sha256:" + "1" * 64,
        "current_instruction_ref": "instruction:cross-repo-conformance-v1",
        "current_instruction_digest": "sha256:" + "2" * 64,
        "transcript_snapshot_ref": "transcript:cross-repo-conformance-v1",
        "transcript_snapshot_digest": "sha256:" + "3" * 64,
        "compaction_state": "not_compacted",
        "compaction_receipt_ref": None,
        "compaction_receipt_digest": None,
        "policy_basis_ref": "policy:cross-repo-conformance-v1",
        "policy_basis_digest": "blake3:" + "4" * 64,
        "graph_obligation_ref": "graph-obligation:cross-repo-conformance-v1",
        "graph_obligation_digest": "blake3:" + "5" * 64,
        "memory_requirement": "not_required",
        "authorized_memory_namespaces": [],
        "route_resolution_ref": "route:cross-repo-conformance-v1",
        "route_resolution_digest": "sha256:" + "6" * 64,
        "source_revision": f"git:{ares_revision}",
        "authority_origin": "host:test-conformance-owner",
        "derived_authority_ref": None,
        "derived_authority_digest": None,
    }


def build_fixture(ares_revision: str) -> tuple[bytes, dict[str, Any]]:
    context = ManagedCallContextV1.create(context_values(ares_revision))
    material = context.to_dict()
    # Reparse through the Ares owner before anything crosses the repository boundary.
    if ManagedCallContextV1.parse(material).to_dict() != material:
        raise RuntimeError("managed-call context failed Ares owner round-trip")
    context_bytes = canonical_json(material).encode("utf-8")
    manifest = {
        "schema_version": FIXTURE_SCHEMA,
        "ares_revision": ares_revision,
        "conversation_ref": CONVERSATION_REF,
        "context_ref": material["call_ref"],
        "context_digest": material["context_digest"],
        "context_artifact_sha256": hashlib.sha256(context_bytes).hexdigest(),
        "policy_basis_ref": material["policy_basis_ref"],
        "policy_basis_digest": material["policy_basis_digest"],
        "source_revision": material["source_revision"],
        "route_resolution_ref": material["route_resolution_ref"],
        "route_resolution_digest": material["route_resolution_digest"],
    }
    return context_bytes, manifest


def write_fixture(ares_revision: str, context_path: Path, manifest_path: Path) -> None:
    context_bytes, manifest = build_fixture(ares_revision)
    for path in (context_path, manifest_path):
        if path.exists():
            raise FileExistsError(f"refusing to overwrite conformance artifact: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_bytes(context_bytes)
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
