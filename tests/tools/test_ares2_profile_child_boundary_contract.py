"""PR #94878 source-to-target environment authority conformance.

Exact behavioral oracle: RecursiveIntell/Ares PR branch at
61e2dede7f92a76919ca797c314c5a2e83541bbd. These are intended
red-witness cases on unmodified stock Hermes, which does not expose the
ProfileEnvBoundary API. The suite MUST NOT be reported passing until run.

Uses disposable profile homes and synthetic values only.
"""
from pathlib import Path

import pytest


def _profile(tmp_path: Path, name: str, values: dict[str, str]) -> Path:
    home = tmp_path / name
    home.mkdir()
    (home / ".env").write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    return home


def _build(source: Path, target: Path):
    # Intentional, run-time conformance check: this contract is absent on
    # current upstream. Do not skip the test when a security API is missing.
    from agent.secret_scope import build_profile_env_boundary
    return build_profile_env_boundary(source, target)


def test_source_owned_value_is_removed_without_target_grant(tmp_path):
    source = _profile(tmp_path, "source", {"ACME_LOGIN": "alpha"})
    target = _profile(tmp_path, "target", {})
    child = _build(source, target).sanitize({
        "ACME_LOGIN": "alpha", "BENIGN": "unrelated",
    })
    assert "ACME_LOGIN" not in child
    assert child["BENIGN"] == "unrelated"


def test_target_grant_replaces_colliding_source_value(tmp_path):
    source = _profile(tmp_path, "source", {"DATABASE_URL": "alpha-db"})
    target = _profile(tmp_path, "target", {"DATABASE_URL": "beta-db"})
    child = _build(source, target).sanitize({"DATABASE_URL": "alpha-db"})
    assert child["DATABASE_URL"] == "beta-db"


def test_revocation_does_not_reclassify_stale_source_value_as_ambient(tmp_path):
    source = _profile(tmp_path, "source", {"ACME_LOGIN": "alpha"})
    target = _profile(tmp_path, "target", {})
    _build(source, target)
    (source / ".env").write_text("", encoding="utf-8")
    child = _build(source, target).sanitize({"ACME_LOGIN": "stale-alpha"})
    assert "ACME_LOGIN" not in child


def test_unreadable_source_provenance_refuses_child(tmp_path):
    source = _profile(tmp_path, "source", {})
    target = _profile(tmp_path, "target", {})
    (source / ".env").write_bytes(b"ACME_LOGIN=\\xff\\xfe\n".replace(b"\\xff", b"\xff").replace(b"\\xfe", b"\xfe"))
    with pytest.raises(RuntimeError, match="ownership unavailable"):
        _build(source, target)


def test_forwarding_carrier_cannot_reapply_launch_value(tmp_path):
    source = _profile(tmp_path, "source", {
        "APPTAINERENV_DATABASE_URL": "launch-db",
    })
    target = _profile(tmp_path, "target", {
        "DATABASE_URL": "target-db",
    })
    child = _build(source, target).sanitize({
        "APPTAINERENV_DATABASE_URL": "launch-db",
        "DATABASE_URL": "launch-db",
        "PATH": "/usr/bin",
    })
    assert "APPTAINERENV_DATABASE_URL" not in child
    assert child["DATABASE_URL"] == "target-db"
    assert child["PATH"] == "/usr/bin"
