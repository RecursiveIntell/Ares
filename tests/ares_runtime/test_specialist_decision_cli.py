from __future__ import annotations

import json
import socket
import stat
from pathlib import Path

import pytest

from ares_runtime import specialist_dispatch
from ares_runtime.collaboration import SpecialistDescriptorV1, specialist_descriptor_ref
from ares_runtime.specialist_dispatch import client_main


ROLE_ARTIFACTS = {"role.explorer": ["explorer_dissent"]}


def descriptor(*, enabled: bool = False) -> SpecialistDescriptorV1:
    return SpecialistDescriptorV1.create(
        {
            "profile_id": "explorer",
            "semantic_role_id": "role.explorer",
            "enabled": enabled,
            "narrow_purpose": "Competing hypotheses only.",
            "capability_classes": ["competing_design"],
            "tool_classes": ["artifact_read"],
            "required_artifact_ids": ["explorer_dissent"],
            "input_evidence_classes": ["pinned_source"],
            "required_outputs": ["falsifier"],
            "explicit_exclusions": ["runtime_authority"],
            "mandatory_deferrals": ["statistician"],
            "handoff_rules": ["preserve_evidence"],
            "failure_and_abstention_behavior": {
                "on_insufficient_evidence": "blocked_or_unknown",
                "on_unavailable_or_disabled_contract": "blocked_or_unknown",
                "generic_fallback_label": "forbidden",
            },
            "activation_evidence_refs": ["evidence:activation"],
            "provenance": {
                "source_refs": ["evidence:source"],
                "semantic_registry_ref": "docs:role-contracts/role-contracts.json",
                "semantic_registry_digest": "sha256:" + "a" * 64,
            },
        },
        profile_exists=lambda candidate: candidate in {"explorer", "statistician"},
        semantic_role_artifacts=ROLE_ARTIFACTS,
    )


def wire_inputs(
    monkeypatch: pytest.MonkeyPatch,
    candidate: SpecialistDescriptorV1,
    *,
    binding_ref: str | None = None,
) -> list[tuple[str, tuple[str, ...]]]:
    calls: list[tuple[str, tuple[str, ...]]] = []

    def load_candidates(_source_root: Path, profile_ids: tuple[str, ...] | list[str]):
        calls.append(("load", tuple(profile_ids)))
        return {"explorer": candidate}

    def profile_bindings(profile_ids: tuple[str, ...] | list[str]):
        calls.append(("bindings", tuple(profile_ids)))
        return {
            "explorer": binding_ref
            if binding_ref is not None
            else specialist_descriptor_ref(candidate)
        }

    monkeypatch.setattr(specialist_dispatch, "load_active_candidates", load_candidates)
    monkeypatch.setattr(specialist_dispatch, "_profile_bindings", profile_bindings)
    return calls


def hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "hermes-home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


def invoke_decide(capsys: pytest.CaptureFixture[str], *args: str) -> dict[str, object]:
    assert client_main(["decide", "--profile", "explorer", *args]) == 0
    return json.loads(capsys.readouterr().out)


def test_decide_without_capability_emits_no_specialist_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    candidate = descriptor()
    calls = wire_inputs(monkeypatch, candidate)
    root = hermes_home(tmp_path, monkeypatch)

    receipt = invoke_decide(capsys)

    assert receipt["outcome"] == "no_specialist_needed"
    assert receipt["selected_profile_id"] is None
    assert receipt["dispatch_authorized"] is False
    assert calls == [("load", ("explorer",)), ("bindings", ("explorer",))]
    saved = root / "specialist-decision-receipts" / f"{receipt['decision_digest']}.json"
    assert json.loads(saved.read_text(encoding="utf-8")) == receipt


def test_decide_matching_disabled_descriptor_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    candidate = descriptor(enabled=False)
    wire_inputs(monkeypatch, candidate)
    hermes_home(tmp_path, monkeypatch)

    receipt = invoke_decide(capsys, "--capability", "explorer:competing_design")

    assert receipt["outcome"] == "blocked"
    assert receipt["selected_profile_id"] is None
    assert receipt["candidate_rejections"] == [
        {"profile_id": "explorer", "reason_code": "DESCRIPTOR_DISABLED"}
    ]


def test_decide_profile_binding_mismatch_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    candidate = descriptor()
    wire_inputs(
        monkeypatch,
        candidate,
        binding_ref="specialist-descriptor:" + "0" * 64,
    )
    hermes_home(tmp_path, monkeypatch)

    receipt = invoke_decide(capsys, "--capability", "explorer:competing_design")

    assert receipt["outcome"] == "blocked"
    assert receipt["selected_profile_id"] is None
    assert receipt["candidate_rejections"] == [
        {"profile_id": "explorer", "reason_code": "PROFILE_BINDING_MISMATCH"}
    ]


def test_decide_receipt_rewrite_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    candidate = descriptor()
    wire_inputs(monkeypatch, candidate)
    root = hermes_home(tmp_path, monkeypatch)
    args = ["--capability", "explorer:competing_design"]

    first = invoke_decide(capsys, *args)
    receipt_path = root / "specialist-decision-receipts" / f"{first['decision_digest']}.json"
    first_bytes = receipt_path.read_bytes()
    second = invoke_decide(capsys, *args)

    assert second == first
    assert receipt_path.read_bytes() == first_bytes


def test_decide_refuses_conflicting_write_once_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    candidate = descriptor()
    wire_inputs(monkeypatch, candidate)
    root = hermes_home(tmp_path, monkeypatch)
    args = ["--capability", "explorer:competing_design"]

    receipt = invoke_decide(capsys, *args)
    receipt_path = root / "specialist-decision-receipts" / f"{receipt['decision_digest']}.json"
    receipt_path.write_text('{"conflict":true}\n', encoding="utf-8")

    result = client_main(["decide", "--profile", "explorer", *args])
    error_output = capsys.readouterr().err

    assert result != 0
    assert "DECISION_RECEIPT_CONFLICT" in error_output
    assert receipt_path.read_text(encoding="utf-8") == '{"conflict":true}\n'


def test_decide_receipt_directory_and_file_have_restricted_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    candidate = descriptor()
    wire_inputs(monkeypatch, candidate)
    root = hermes_home(tmp_path, monkeypatch)

    receipt = invoke_decide(capsys)

    receipt_dir = root / "specialist-decision-receipts"
    receipt_path = receipt_dir / f"{receipt['decision_digest']}.json"
    assert stat.S_IMODE(receipt_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600


def test_decide_never_authorizes_dispatch_or_opens_desktop_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    candidate = descriptor(enabled=True)
    wire_inputs(monkeypatch, candidate)
    root = hermes_home(tmp_path, monkeypatch)
    (root / "specialist-dispatch.json").write_text(
        json.dumps(
            {
                "schema": "AresDesktopSpecialistDispatchEndpointV1",
                "host": "127.0.0.1",
                "port": 1,
                "token": "x" * 32,
            }
        ),
        encoding="utf-8",
    )
    socket_attempts: list[tuple[object, ...]] = []

    def forbidden_socket(*args: object, **kwargs: object) -> None:
        socket_attempts.append(args)
        raise AssertionError("decision-only CLI must never open a Desktop socket")

    monkeypatch.setattr(socket, "create_connection", forbidden_socket)

    receipt = invoke_decide(capsys, "--capability", "explorer:competing_design")

    assert socket_attempts == []
    assert receipt["selected_profile_id"] is None
    assert receipt["dispatch_authorized"] is False


def test_decide_cli_loads_repository_descriptors_without_desktop_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source_root = Path(specialist_dispatch.__file__).resolve().parents[1]
    candidate = specialist_dispatch.load_active_candidates(source_root, ["explorer"])["explorer"]
    monkeypatch.setattr(
        specialist_dispatch,
        "_profile_bindings",
        lambda profile_ids: {"explorer": specialist_descriptor_ref(candidate)},
    )
    hermes_home(tmp_path, monkeypatch)

    receipt = invoke_decide(capsys, "--capability", "explorer:competing_design_generation")

    assert receipt["outcome"] == "blocked"
    assert receipt["candidate_profile_ids"] == ["explorer"]
    assert receipt["candidate_rejections"] == [
        {"profile_id": "explorer", "reason_code": "NONACTIVATION_POLICY_DENIED"}
    ]
    assert receipt["selected_profile_id"] is None
    assert receipt["dispatch_authorized"] is False
