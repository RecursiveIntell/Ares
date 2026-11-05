"""Strict execution-admission config must not promote display fallback."""
import dataclasses

import pytest

from hermes_cli import config, managed_scope


def admit():
    loader = getattr(config, "load_config_for_admission", None)
    assert callable(loader), "config owner needs a strict admission snapshot API"
    return loader()


def test_admission_reads_user_and_managed_precedence(tmp_path, monkeypatch):
    user = config.get_config_path()
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_text("delegation:\n  model: user-worker\n  provider: user-provider\n")
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("delegation:\n  model: managed-worker\n")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    snapshot = admit()
    assert snapshot.config["delegation"]["model"] == "managed-worker"
    assert snapshot.config["delegation"]["provider"] == "user-provider"
    snapshot.config["delegation"]["model"] = "cannot-mutate-owner"
    assert snapshot.config["delegation"]["model"] == "managed-worker"


@pytest.mark.parametrize("bad", ["delegation: [", "- not\n- a\n- mapping", "false", "42"])
def test_bad_current_source_is_not_an_admitted_default(bad):
    path = config.get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(bad)
    with pytest.raises(ValueError):
        admit()


def test_warmed_last_known_good_is_not_execution_admission():
    path = config.get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("delegation:\n  model: original-worker\n")
    assert config.load_config()["delegation"]["model"] == "original-worker"
    path.write_text("delegation: [unterminated")
    # Normal display/runtime lookup deliberately remains available.
    assert config.load_config()["delegation"]["model"] == "original-worker"
    with pytest.raises(ValueError):
        admit()


def test_admission_accepts_absence_without_creating_user_files():
    path = config.get_config_path()
    path.unlink(missing_ok=True)
    snapshot = admit()
    assert isinstance(snapshot.config, dict)
    assert not path.exists()


def test_explicit_missing_managed_scope_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "missing-managed"))
    with pytest.raises(ValueError):
        admit()


def test_malformed_managed_scope_does_not_disappear(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("delegation: [")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    # Existing lenient managed display lookup is intentionally preserved.
    assert managed_scope.load_managed_config() == {}
    with pytest.raises(ValueError):
        admit()


def test_valid_correction_gets_a_new_source_fingerprint():
    path = config.get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("delegation:\n  model: one\n")
    first = admit()
    path.write_text("delegation:\n  model: two\n")
    second = admit()
    assert first.fingerprint != second.fingerprint
    assert first.config["delegation"]["model"] == "one"
    assert second.config["delegation"]["model"] == "two"


def test_non_file_config_source_is_a_typed_refusal():
    path = config.get_config_path()
    path.unlink(missing_ok=True)
    path.mkdir(parents=True)
    with pytest.raises(ValueError):
        admit()


def test_profile_rehome_is_not_a_cache_hit(tmp_path, monkeypatch):
    first_home = tmp_path / "one"
    second_home = tmp_path / "two"
    first_home.mkdir()
    second_home.mkdir()
    (first_home / "config.yaml").write_text("delegation:\n  model: first\n")
    (second_home / "config.yaml").write_text("delegation:\n  model: second\n")
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(first_home)
    try:
        first = admit()
    finally:
        reset_hermes_home_override(token)
    token = set_hermes_home_override(second_home)
    try:
        second = admit()
    finally:
        reset_hermes_home_override(token)
    assert first.profile_home != second.profile_home
    assert first.config["delegation"]["model"] == "first"
    assert second.config["delegation"]["model"] == "second"
