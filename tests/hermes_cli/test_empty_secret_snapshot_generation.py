"""No-source rereads cannot mint a new epoch; actual refreshes still revoke."""
import pytest

from agent import secret_scope
from hermes_cli import env_loader


@pytest.fixture
def scope_home(tmp_path, monkeypatch):
    home = tmp_path / "scope-owner"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    env_loader.reset_secret_source_cache()
    yield home
    env_loader.reset_secret_source_cache()


@pytest.mark.parametrize("status", ["absent", "empty"])
def test_identical_empty_snapshot_publication_keeps_generation(scope_home, status):
    first = env_loader._record_external_secret_snapshot(scope_home, data={}, status=status)
    second = env_loader._record_external_secret_snapshot(scope_home, data={}, status=status)
    assert second.generation == first.generation
    assert second.source_identity == first.source_identity


@pytest.mark.parametrize("config", [None, "secrets: {}\n", "secrets:\n  fixture:\n    enabled: false\n"])
def test_redundant_cron_startup_load_preserves_scope(scope_home, config):
    if config is not None:
        (scope_home / "config.yaml").write_text(config)
    admitted = secret_scope.build_profile_secret_scope(scope_home)
    token = secret_scope.set_secret_scope(admitted)
    try:
        env_loader.load_hermes_dotenv(hermes_home=scope_home)
        assert str(scope_home.resolve()) not in env_loader._APPLIED_HOMES
        boundary = secret_scope.build_profile_env_boundary(source_home=scope_home, target_home=scope_home)
        assert boundary.target_generation == admitted.generation
        assert boundary.target_values == admitted.data
    finally:
        secret_scope.reset_secret_scope(token)


def test_explicit_cache_reset_revokes_equal_empty_scope(scope_home):
    admitted = secret_scope.build_profile_secret_scope(scope_home)
    token = secret_scope.set_secret_scope(admitted)
    try:
        env_loader.reset_secret_source_cache()
        fresh = secret_scope.build_profile_secret_scope(scope_home)
        assert fresh.generation != admitted.generation
        with pytest.raises(RuntimeError, match="stale for target profile generation"):
            secret_scope.build_profile_env_boundary(source_home=scope_home, target_home=scope_home)
    finally:
        secret_scope.reset_secret_scope(token)


@pytest.mark.parametrize("filename,text", [
    (".env", "FIXTURE_TOKEN=inert-token\n"),
    (".op.env", "FIXTURE_BOOTSTRAP=inert-bootstrap\n"),
    ("config.yaml", "secrets:\n  fixture:\n    enabled: false\n"),
])
def test_source_mutation_revokes_captured_scope(scope_home, filename, text):
    admitted = secret_scope.build_profile_secret_scope(scope_home)
    token = secret_scope.set_secret_scope(admitted)
    try:
        (scope_home / filename).write_text(text)
        fresh = secret_scope.build_profile_secret_scope(scope_home)
        assert fresh.generation != admitted.generation
        with pytest.raises(RuntimeError, match="stale for target profile generation"):
            secret_scope.build_profile_env_boundary(source_home=scope_home, target_home=scope_home)
    finally:
        secret_scope.reset_secret_scope(token)


@pytest.mark.parametrize("status,data", [
    ("ready", {"FIXTURE_TOKEN": "inert-token"}),
    ("failed", {}),
    ("degraded", {"FIXTURE_TOKEN": "inert-token"}),
])
def test_substantive_or_failed_publication_keeps_existing_epoch_semantics(scope_home, status, data):
    first = env_loader._record_external_secret_snapshot(scope_home, data=data, status=status)
    second = env_loader._record_external_secret_snapshot(scope_home, data=data, status=status)
    assert second.generation > first.generation


def test_absent_to_empty_is_a_new_snapshot(scope_home):
    first = env_loader._record_external_secret_snapshot(scope_home, data={}, status="absent")
    second = env_loader._record_external_secret_snapshot(scope_home, data={}, status="empty")
    assert second.generation > first.generation


def test_changed_error_kind_is_not_deduplicated(scope_home):
    first = env_loader._record_external_secret_snapshot(scope_home, data={}, status="empty")
    second = env_loader._record_external_secret_snapshot(
        scope_home, data={}, status="empty", error_kind="controlled-difference",
    )
    assert second.generation > first.generation


def test_nonempty_data_cannot_borrow_empty_publication_fast_path(scope_home):
    first = env_loader._record_external_secret_snapshot(
        scope_home, data={"FIXTURE_TOKEN": "inert-token"}, status="empty",
    )
    second = env_loader._record_external_secret_snapshot(
        scope_home, data={"FIXTURE_TOKEN": "inert-token"}, status="empty",
    )
    assert second.generation > first.generation
