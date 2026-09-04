from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

ROOT = Path(__file__).parents[2]
SCHEMA_DIR = ROOT / "tui_gateway" / "protocol" / "schemas"
MANIFEST_PATH = ROOT / "apps" / "shared" / "src" / "protocol" / "generated" / "mobile-protocol-manifest.json"
TYPES_PATH = ROOT / "apps" / "shared" / "src" / "protocol" / "generated" / "mobile-protocol.ts"


def test_all_mobile_protocol_schemas_are_valid_json_schema_documents() -> None:
    schemas = sorted(SCHEMA_DIR.glob("*.json"))
    assert schemas
    for path in schemas:
        document = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(document)
        assert document["$id"].startswith("https://ares.local/protocol/")


def test_generated_manifest_is_reproducible_and_matches_source_schemas() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/generate_mobile_protocol.py", "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["schema"] == "AresMobileProtocolManifestV1"
    assert manifest["protocol_revision"] == 2
    assert manifest["schemas"]
    assert all(len(item["sha256"]) == 64 for item in manifest["schemas"])
    types = TYPES_PATH.read_text(encoding="utf-8")
    assert "MOBILE_PROTOCOL_REVISION" in types
    assert "GatewayReadyV2" in types


def test_generator_rejects_stale_manifest(tmp_path: Path) -> None:
    stale = tmp_path / "stale.json"
    stale.write_text("{}\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "scripts/generate_mobile_protocol.py",
            "--check",
            "--manifest",
            str(stale),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "stale" in (result.stdout + result.stderr).lower()


def test_session_observe_schema_requires_host_identity() -> None:
    schema = json.loads((SCHEMA_DIR / "session-observe.v1.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    with pytest.raises(ValidationError):
        validator.validate({"session_id": "session", "profile": "default"})

    validator.validate(
        {
            "session_id": "session",
            "profile": "default",
            "host_id": "host",
        }
    )


def test_controller_lease_and_mutation_outcome_schemas_model_admitted_contracts() -> None:
    control = Draft202012Validator(
        json.loads((SCHEMA_DIR / "session-control.v1.json").read_text(encoding="utf-8"))
    )
    outcome_path = SCHEMA_DIR / "mutation-outcome.v1.json"
    assert outcome_path.is_file()
    outcome = Draft202012Validator(json.loads(outcome_path.read_text(encoding="utf-8")))

    control.validate(
        {
            "host_id": "host",
            "profile": "default",
            "session_id": "session",
            "runtime_id": "runtime",
            "principal_id": "principal",
            "controller_instance_id": "phone",
            "generation": 1,
            "fencing_token": "fence",
            "expires_at": 123.0,
        }
    )
    with pytest.raises(ValidationError):
        control.validate(
            {
                "host_id": "host",
                "profile": "default",
                "session_id": "session",
                "principal_id": "principal",
                "controller_instance_id": "phone",
                "generation": 1,
            }
        )

    outcome.validate(
        {
            "scope": {
                "host_id": "host",
                "profile": "default",
                "session_key": "stored",
                "principal_id": "principal",
            },
            "method": "session.title",
            "idempotency_key": "request",
            "payload_digest": "a" * 64,
            "state": "completed",
            "result": {"title": "safe"},
            "error": None,
            "created_at": 1.0,
            "updated_at": 2.0,
        }
    )
    with pytest.raises(ValidationError):
        outcome.validate(
            {
                "scope": {"host_id": "host", "profile": "default", "session_key": "stored"},
                "method": "session.title",
                "idempotency_key": "request",
                "payload_digest": "a" * 64,
                "state": "completed",
                "created_at": 1.0,
                "updated_at": 2.0,
            }
        )
