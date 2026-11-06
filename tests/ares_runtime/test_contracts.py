from __future__ import annotations

import json

import pytest

from ares_runtime import (
    ActivationGrant,
    AresRuntimeError,
)


def _digest(char: str) -> str:
    return char * 64


def test_activation_grant_is_bound_to_all_identity_fields(tmp_path):
    grant = ActivationGrant(
        candidate_id=_digest("a"),
        certification_set_id=_digest("b"),
        sealed_candidate_id=_digest("c"),
        audit_subject_id=_digest("d"),
        audit_subject_sha256=_digest("e"),
        audit_result_sha256=_digest("f"),
        archive_sha256=_digest("1"),
        candidate_core_sha256=_digest("2"),
        sealed_manifest_sha256=_digest("3"),
        release_manifest_sha256=_digest("4"),
        runtime_tree_sha256=_digest("5"),
        custody_event_sequence=4,
        target_platform="linux-x86_64",
        target_release_root=str(tmp_path / "ares" / "releases"),
        materializer_contract="AresReleaseMaterializerV1",
        activator_contract="AresReleaseActivatorV1",
        resolver_contract="AresRuntimeResolverV1",
    ).with_grant_id()

    parsed = ActivationGrant.parse(grant.canonical_bytes())
    assert parsed == grant
    changed = json.loads(grant.canonical_bytes())
    changed["archive_sha256"] = _digest("6")
    changed_raw = (
        json.dumps(changed, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )

    with pytest.raises(AresRuntimeError, match="grant_id"):
        ActivationGrant.parse(changed_raw)
