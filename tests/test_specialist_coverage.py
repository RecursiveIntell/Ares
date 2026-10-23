"""Synthetic tests for inert, source-scoped specialist coverage capture."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
from pathlib import Path

import jsonschema
import pytest

from ares_runtime.collaboration import digest
from ares_runtime import specialist_routing
from hermes_cli import profiles


SOURCE_KINDS = (
    "descriptor_manifest",
    "panel_roster",
    "role_registry",
    "profile_registry",
    "binding_index",
)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _assessment(
    *,
    semantic_coverage: str = "not_covered",
    index_status: str = "unindexed",
    enabled_state: str = "disabled",
    availability: str = "unknown",
) -> dict[str, object]:
    return {
        "semantic_role_id": "role.existing-research",
        "descriptor_status": "valid",
        "descriptor_ref": "descriptor:existing-research",
        "binding_status": "unbound",
        "binding_ref": None,
        "index_status": index_status,
        "enabled_state": enabled_state,
        "availability": availability,
        "semantic_coverage": semantic_coverage,
    }


def _scope(root: Path, *, profile_id: str = "alpha") -> dict[str, object]:
    (root / "sources").mkdir(parents=True)
    roster = root / "profiles" / profile_id
    roster.mkdir(parents=True)
    sources: list[dict[str, object]] = []
    profile_files: list[dict[str, str]] = []
    applicability: list[dict[str, object]] = []

    for kind in SOURCE_KINDS:
        source_ref = f"source:{kind}"
        global_ref = f"file:{kind}"
        global_path = f"sources/{kind}.json"
        global_data: dict[str, object] = {"profiles": [profile_id], "kind": kind}
        if kind == "profile_registry":
            global_data["profiles"] = [profile_id]
        (root / global_path).write_bytes(_json_bytes(global_data))
        sources.append({
            "source_ref": source_ref,
            "source_kind": kind,
            "revision": "synthetic-r1",
            "cutoff": "synthetic-cutoff-1",
            "file_ref": global_ref,
            "relative_path": global_path,
            "profile_ids_field": "profiles",
        })

        profile_ref = f"file:{profile_id}:{kind}"
        profile_path = f"profiles/{profile_id}/{kind}.json"
        (root / profile_path).write_bytes(_json_bytes({"id": profile_id, "kind": kind}))
        profile_files.append({
            "profile_id": profile_id,
            "source_ref": source_ref,
            "file_ref": profile_ref,
            "relative_path": profile_path,
            "format": "json",
        })
        applicability.append({
            "profile_id": profile_id,
            "source_ref": source_ref,
            "applicability": "required",
            "file_ref": profile_ref,
            "basis": "Reviewed synthetic applicability for this source/profile pair.",
            "basis_refs": ["source:profile_registry"],
        })

    return {
        "schema_version": "1.0.0",
        "scope_ref": "scope:synthetic",
        "description": "Synthetic fixtures under pytest tmp_path only.",
        "source_revision": "synthetic-rev-1",
        "cutoff": "synthetic-cutoff-1",
        "approved_root": str(root),
        "roster_root": "profiles",
        "roster_source_ref": "source:profile_registry",
        "sources": sources,
        "profile_files": profile_files,
        "source_profile_applicability": applicability,
        "profile_assessments": [
            {
                "profile_id": profile_id,
                "roles": [_assessment()],
                "exclusions": ["No absence inference from names or keywords."],
            }
        ],
    }


def _two_profile_scope(root: Path) -> dict[str, object]:
    scope = _scope(root)
    (root / "profiles/beta").mkdir()
    for source in scope["sources"]:
        source_path = root / source["relative_path"]
        global_value = json.loads(source_path.read_text(encoding="utf-8"))
        global_value["profiles"] = ["alpha", "beta"]
        source_path.write_bytes(_json_bytes(global_value))

        profile_path = f"profiles/beta/{source['source_kind']}.json"
        (root / profile_path).write_bytes(
            _json_bytes({"id": "beta", "kind": source["source_kind"]})
        )
        file_ref = f"file:beta:{source['source_kind']}"
        scope["profile_files"].append({
            "profile_id": "beta",
            "source_ref": source["source_ref"],
            "file_ref": file_ref,
            "relative_path": profile_path,
            "format": "json",
        })
        scope["source_profile_applicability"].append({
            "profile_id": "beta",
            "source_ref": source["source_ref"],
            "applicability": "required",
            "file_ref": file_ref,
            "basis": "Reviewed synthetic applicability for this source/profile pair.",
            "basis_refs": ["source:profile_registry"],
        })
    scope["profile_assessments"].append({
        "profile_id": "beta",
        "roles": [_assessment()],
        "exclusions": ["No absence inference from names or keywords."],
    })
    return scope


def _manifest(scope: dict[str, object]) -> dict[str, object]:
    return {
        "roster_root": scope["roster_root"],
        "files": [
            {
                "file_ref": item["file_ref"],
                "relative_path": item["relative_path"],
                "format": "json",
            }
            for item in scope["sources"]
        ]
        + [
            {
                "file_ref": item["file_ref"],
                "relative_path": item["relative_path"],
                "format": item["format"],
            }
            for item in scope["profile_files"]
        ],
    }


def _require_api(owner: object, name: str):
    api = getattr(owner, name, None)
    assert callable(api), f"expected P02 API {owner.__name__}.{name}"
    return api


def _require_safe_read_support() -> None:
    if (
        not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or os.open not in getattr(os, "supports_dir_fd", set())
        or os.stat not in getattr(os, "supports_dir_fd", set())
        or os.stat not in getattr(os, "supports_follow_symlinks", set())
        or os.listdir not in getattr(os, "supports_fd", set())
    ):
        pytest.skip("explicit no-follow directory-relative reads are unsupported here")


def test_capture_preserves_independent_role_status_axes_and_exact_input_bytes(
    tmp_path: Path,
) -> None:
    _require_safe_read_support()
    scope = _scope(tmp_path / "approved")
    capture_api = _require_api(specialist_routing, "capture_coverage_snapshot")
    evidence_api = _require_api(profiles, "read_profile_evidence")

    snapshot = capture_api(scope).to_dict()
    role = snapshot["roster_manifest"][0]["roles"][0]
    assert role == _assessment()
    registry_file = next(
        row for row in snapshot["sources"] if row["source_kind"] == "profile_registry"
    )
    raw = (tmp_path / "approved/sources/profile_registry.json").read_bytes()
    assert registry_file["byte_digest"] == f"sha256:{hashlib.sha256(raw).hexdigest()}"
    assert registry_file["status"] == "valid_populated"
    assert (
        snapshot["declared_scope"]["completeness"] == "complete_within_declared_sources"
    )
    assert snapshot["roster_manifest"][0]["profile_id"] == "alpha"
    assert role["semantic_role_id"] == "role.existing-research"
    assert snapshot["snapshot_digest"] == digest({
        key: value for key, value in snapshot.items() if key != "snapshot_digest"
    })
    schema_path = Path(__file__).resolve().parents[1] / (
        "ares_runtime/schemas/specialist_coverage_snapshot_v1.json"
    )
    jsonschema.validate(snapshot, json.loads(schema_path.read_text(encoding="utf-8")))
    assert capture_api(scope).to_dict() == snapshot

    reader_manifest = {
        "roster_root": "profiles",
        "files": [
            {
                "file_ref": "file:empty",
                "relative_path": "sources/empty.json",
                "format": "json",
            },
            {
                "file_ref": "file:populated",
                "relative_path": "sources/populated.json",
                "format": "json",
            },
        ],
    }
    (tmp_path / "approved/sources/empty.json").write_bytes(b"{}")
    (tmp_path / "approved/sources/populated.json").write_bytes(b'{"x":1}')
    read = evidence_api(tmp_path / "approved", reader_manifest)
    assert [item.status for item in read.files] == ["valid_empty", "valid_populated"]


@pytest.mark.parametrize("state", ["missing", "malformed", "unreadable"])
def test_missing_or_malformed_required_profile_evidence_cannot_assert_absence(
    tmp_path: Path, state: str
) -> None:
    _require_safe_read_support()
    scope = _scope(tmp_path / "approved")
    target = tmp_path / "approved/profiles/alpha/role_registry.json"
    if state == "missing":
        target.unlink()
    elif state == "malformed":
        target.write_text("{broken", encoding="utf-8")
    else:
        target.unlink()
        target.mkdir()
    snapshot = _require_api(specialist_routing, "capture_coverage_snapshot")(
        scope
    ).to_dict()
    assert snapshot["declared_scope"]["completeness"] == "incomplete"
    assert snapshot["roster_manifest"][0]["roles"][0]["semantic_coverage"] == "unknown"
    assert snapshot["roster_manifest"][0]["source_files"]
    assert any(state in gap for gap in snapshot["declared_scope"]["gaps"])


def test_later_profile_gap_downgrades_absence_for_every_profile(
    tmp_path: Path,
) -> None:
    _require_safe_read_support()
    scope = _two_profile_scope(tmp_path / "approved")
    missing = next(
        row
        for row in scope["profile_files"]
        if row["profile_id"] == "beta" and row["source_ref"] == "source:role_registry"
    )
    (tmp_path / "approved" / missing["relative_path"]).unlink()

    snapshot = _require_api(specialist_routing, "capture_coverage_snapshot")(
        scope
    ).to_dict()
    assert snapshot["declared_scope"]["completeness"] == "incomplete"
    assert [row["profile_id"] for row in snapshot["roster_manifest"]] == [
        "alpha",
        "beta",
    ]
    assert all(
        profile["roles"][0]["semantic_coverage"] == "unknown"
        for profile in snapshot["roster_manifest"]
    )


def test_unresolved_applicability_is_explicit_and_never_omitted(tmp_path: Path) -> None:
    _require_safe_read_support()
    scope = _scope(tmp_path / "approved")
    row = scope["source_profile_applicability"][0]
    removed_ref = row["file_ref"]
    row.update(applicability="unresolved", file_ref=None)
    scope["profile_files"] = [
        item for item in scope["profile_files"] if item["file_ref"] != removed_ref
    ]
    snapshot = _require_api(specialist_routing, "capture_coverage_snapshot")(
        scope
    ).to_dict()
    assert snapshot["declared_scope"]["completeness"] == "incomplete"
    assert snapshot["roster_manifest"][0]["roles"][0]["semantic_coverage"] == "unknown"

    omitted = copy.deepcopy(scope)
    omitted["source_profile_applicability"] = omitted["source_profile_applicability"][
        1:
    ]
    error = specialist_routing.CoverageCaptureError
    with pytest.raises(error) as raised:
        _require_api(specialist_routing, "compile_coverage")(
            omitted,
            _require_api(profiles, "read_profile_evidence")(
                Path(scope["approved_root"]), _manifest(scope)
            ),
        )
    assert raised.value.code == "INVALID_APPLICABILITY_MATRIX"


def test_static_registry_roster_mismatch_stays_incomplete_not_absent(
    tmp_path: Path,
) -> None:
    _require_safe_read_support()
    scope = _scope(tmp_path / "approved")
    registry = tmp_path / "approved/sources/profile_registry.json"
    registry.write_bytes(_json_bytes({"profiles": [], "kind": "profile_registry"}))
    for row in scope["source_profile_applicability"]:
        row["basis_refs"] = ["source:descriptor_manifest"]
    snapshot = _require_api(specialist_routing, "capture_coverage_snapshot")(
        scope
    ).to_dict()
    assert snapshot["declared_scope"]["completeness"] == "incomplete"
    assert snapshot["roster_manifest"][0]["roles"][0]["semantic_coverage"] == "unknown"
    assert "profile_registry_roster_mismatch" in snapshot["declared_scope"]["gaps"]


def test_empty_profile_registry_missing_identity_field_is_malformed_not_absent(
    tmp_path: Path,
) -> None:
    _require_safe_read_support()
    scope = _scope(tmp_path / "approved")
    registry = tmp_path / "approved/sources/profile_registry.json"
    registry.write_bytes(b"{}")
    for row in scope["source_profile_applicability"]:
        row["basis_refs"] = ["source:descriptor_manifest"]
    snapshot = _require_api(specialist_routing, "capture_coverage_snapshot")(
        scope
    ).to_dict()
    registry_source = next(
        row for row in snapshot["sources"] if row["source_kind"] == "profile_registry"
    )
    assert registry_source["status"] == "malformed"
    assert snapshot["declared_scope"]["completeness"] == "incomplete"
    assert snapshot["roster_manifest"][0]["roles"][0]["semantic_coverage"] == "unknown"


def test_unclassified_roster_entry_is_a_gap_not_silent_exclusion(
    tmp_path: Path,
) -> None:
    _require_safe_read_support()
    scope = _scope(tmp_path / "approved")
    (tmp_path / "approved/profiles/Not-a-profile").mkdir()
    snapshot = _require_api(specialist_routing, "capture_coverage_snapshot")(
        scope
    ).to_dict()
    assert snapshot["declared_scope"]["completeness"] == "incomplete"
    assert "roster_contains_unclassified_entries" in snapshot["declared_scope"]["gaps"]
    assert snapshot["roster_manifest"][0]["roles"][0]["semantic_coverage"] == "unknown"


def test_reviewed_role_facts_are_not_inferred_and_empty_assessment_needs_exclusion(
    tmp_path: Path,
) -> None:
    _require_safe_read_support()
    scope = _scope(tmp_path / "approved")
    scope["profile_assessments"][0]["roles"] = []
    scope["profile_assessments"][0]["exclusions"] = []
    with pytest.raises(specialist_routing.CoverageCaptureError) as raised:
        _require_api(specialist_routing, "compile_coverage")(
            scope,
            _require_api(profiles, "read_profile_evidence")(
                Path(scope["approved_root"]), _manifest(scope)
            ),
        )
    assert raised.value.code == "MISSING_REVIEWED_ASSESSMENT"


def test_compile_is_pure_and_rejects_unknown_scope_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_safe_read_support()
    scope = _scope(tmp_path / "approved")
    reader = _require_api(profiles, "read_profile_evidence")
    evidence = reader(tmp_path / "approved", _manifest(scope))
    monkeypatch.setattr(
        profiles,
        "read_profile_evidence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("compiler performed I/O")
        ),
    )
    result = _require_api(specialist_routing, "compile_coverage")(scope, evidence)
    assert (
        result.to_dict()["declared_scope"]["completeness"]
        == "complete_within_declared_sources"
    )

    unknown = copy.deepcopy(scope)
    unknown["auth_health"] = "must be refused"
    with pytest.raises(specialist_routing.CoverageCaptureError) as raised:
        _require_api(specialist_routing, "compile_coverage")(unknown, evidence)
    assert raised.value.code == "INVALID_SCOPE_MANIFEST"

    with pytest.raises(specialist_routing.CoverageCaptureError) as raised:
        _require_api(specialist_routing, "capture_coverage_snapshot")(unknown)
    assert raised.value.code == "INVALID_SCOPE_MANIFEST"


def test_compile_evidence_annotation_uses_profile_owner_type() -> None:
    assert (
        specialist_routing.compile_coverage.__annotations__["evidence"]
        == "ProfileEvidenceRead"
    )


def test_same_size_source_swap_with_restored_mtime_refuses_as_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_safe_read_support()
    scope = _scope(tmp_path / "approved")
    target = tmp_path / "approved/profiles/alpha/descriptor_manifest.json"
    before = target.read_bytes()
    old_stat = target.stat()
    assert b"descriptor_manifest" in before
    original_reader = profiles.read_profile_evidence
    calls = 0

    def changing_reader(root: Path, manifest: dict[str, object]):
        nonlocal calls
        result = original_reader(root, manifest)
        calls += 1
        if calls == 1:
            changed = before.replace(b"descriptor_manifest", b"descriptor_manifesx")
            assert len(changed) == len(before)
            target.write_bytes(changed)
            os.utime(target, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        return result

    monkeypatch.setattr(profiles, "read_profile_evidence", changing_reader)
    with pytest.raises(specialist_routing.CoverageCaptureError) as raised:
        _require_api(specialist_routing, "capture_coverage_snapshot")(scope)
    assert calls == 2
    assert raised.value.code == "SOURCE_DRIFT"


def test_capture_refuses_omitted_required_pair_before_any_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = _scope(tmp_path / "approved")
    scope["source_profile_applicability"].pop()
    monkeypatch.setattr(
        profiles,
        "read_profile_evidence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid matrix reached evidence reader")
        ),
    )
    with pytest.raises(specialist_routing.CoverageCaptureError) as raised:
        _require_api(specialist_routing, "capture_coverage_snapshot")(scope)
    assert raised.value.code == "INVALID_APPLICABILITY_MATRIX"


def test_capture_refuses_omitted_nonrequired_pair_before_any_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = _scope(tmp_path / "approved")
    row = scope["source_profile_applicability"][0]
    file_ref = row["file_ref"]
    row.update(applicability="unresolved", file_ref=None)
    scope["profile_files"] = [
        item for item in scope["profile_files"] if item["file_ref"] != file_ref
    ]
    scope["source_profile_applicability"].remove(row)
    monkeypatch.setattr(
        profiles,
        "read_profile_evidence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("incomplete applicability matrix reached evidence reader")
        ),
    )
    with pytest.raises(specialist_routing.CoverageCaptureError) as raised:
        _require_api(specialist_routing, "capture_coverage_snapshot")(scope)
    assert raised.value.code == "INVALID_APPLICABILITY_MATRIX"


def test_path_shaped_file_reference_is_refused_before_output(tmp_path: Path) -> None:
    scope = _scope(tmp_path / "approved")
    scope["sources"][0]["file_ref"] = "/tmp/private-profile.json"
    with pytest.raises(specialist_routing.CoverageCaptureError) as raised:
        _require_api(specialist_routing, "capture_coverage_snapshot")(scope)
    assert raised.value.code == "INVALID_SCOPE_MANIFEST"
    assert "/tmp/private-profile.json" not in str(raised.value)


def test_compiler_refuses_evidence_for_a_different_declared_path(
    tmp_path: Path,
) -> None:
    _require_safe_read_support()
    scope = _scope(tmp_path / "approved")
    evidence = _require_api(profiles, "read_profile_evidence")(
        tmp_path / "approved", _manifest(scope)
    )
    tampered = copy.deepcopy(scope)
    tampered["sources"][0]["relative_path"] = "sources/different.json"
    with pytest.raises(specialist_routing.CoverageCaptureError) as raised:
        _require_api(specialist_routing, "compile_coverage")(tampered, evidence)
    assert raised.value.code == "INVALID_SCOPE_MANIFEST"


@pytest.mark.parametrize("change", ["create", "delete"])
def test_roster_change_between_scans_refuses_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    _require_safe_read_support()
    scope = _scope(tmp_path / "approved")
    original_reader = profiles.read_profile_evidence
    calls = 0

    def changing_reader(root: Path, manifest: dict[str, object]):
        nonlocal calls
        result = original_reader(root, manifest)
        calls += 1
        if calls == 1:
            alpha = root / "profiles/alpha"
            if change == "delete":
                shutil.rmtree(alpha)
            else:
                (root / "profiles/beta").mkdir()
        return result

    monkeypatch.setattr(profiles, "read_profile_evidence", changing_reader)
    with pytest.raises(specialist_routing.CoverageCaptureError) as raised:
        _require_api(specialist_routing, "capture_coverage_snapshot")(scope)
    assert calls == 2
    assert raised.value.code == "ROSTER_DRIFT"


def test_explicit_scope_has_no_environment_or_profile_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_safe_read_support()
    scope = _scope(tmp_path / "approved")
    before = {
        path.relative_to(tmp_path / "approved").as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in (tmp_path / "approved").rglob("*")
        if path.is_file()
    }
    home = tmp_path / "sterile-home"
    hermes_home = tmp_path / "sterile-hermes"
    home.mkdir()
    hermes_home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(
        Path, "home", lambda: (_ for _ in ()).throw(AssertionError("ambient home read"))
    )
    open_file = profiles.os.open
    opened_components: list[str] = []
    protected_names = {
        ".env",
        "auth.json",
        "sessions",
        "gateway",
        "models",
        "config.yaml",
        "config.yml",
    }

    def guarded_open(path, *args, **kwargs):
        name = os.fspath(path)
        opened_components.extend(Path(name).parts)
        assert not any(part.casefold() in protected_names for part in Path(name).parts)
        return open_file(path, *args, **kwargs)

    monkeypatch.setattr(profiles.os, "open", guarded_open)
    monkeypatch.setattr(
        profiles.os,
        "supports_dir_fd",
        set(profiles.os.supports_dir_fd) | {guarded_open},
    )

    snapshot = _require_api(specialist_routing, "capture_coverage_snapshot")(scope)
    after = {
        path.relative_to(tmp_path / "approved").as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in (tmp_path / "approved").rglob("*")
        if path.is_file()
    }
    assert before == after
    assert snapshot.to_dict()["snapshot_digest"].startswith("sha256:")
    assert list(home.iterdir()) == []
    assert list(hermes_home.iterdir()) == []
    assert opened_components
