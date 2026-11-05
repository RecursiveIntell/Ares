"""Strict security-policy snapshots are bound to their config owner and bytes."""
from pathlib import Path

import pytest

from hermes_cli import config
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def test_snapshot_rejects_mismatched_or_aliased_profile_owner(tmp_path):
    first, second, alias = tmp_path / 'first', tmp_path / 'second', tmp_path / 'alias'
    first.mkdir()
    second.mkdir()
    (first / 'config.yaml').write_text('{}\n')
    (second / 'config.yaml').write_text('{}\n')
    snapshot = config.capture_security_policy_config(first)
    snapshot.assert_profile_owner(first)
    with pytest.raises(config.ConfigAdmissionError, match='CONFIG_ADMISSION_OWNER_MISMATCH'):
        snapshot.assert_profile_owner(second)
    try:
        alias.symlink_to(first, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable')
    with pytest.raises(config.ConfigAdmissionError, match='CONFIG_ADMISSION_OWNER_MISMATCH'):
        snapshot.assert_profile_owner(alias)


def test_snapshot_is_detached_and_rechecks_without_reparsing(tmp_path, monkeypatch):
    path = tmp_path / 'config.yaml'
    path.write_text('secrets:\n  bitwarden:\n    access_token_env: FIRST_BOOT\n')
    snapshot = config.capture_security_policy_config(tmp_path)
    value = snapshot.config
    value['secrets']['bitwarden']['access_token_env'] = 'MUTATED'
    assert snapshot.config['secrets']['bitwarden']['access_token_env'] == 'FIRST_BOOT'

    def no_reparse(*args, **kwargs):
        raise AssertionError('snapshot recheck must not parse YAML')
    monkeypatch.setattr(config, '_read_admission_config', no_reparse)
    snapshot.assert_current()
    path.write_text('secrets:\n  bitwarden:\n    access_token_env: SECOND_BOOT\n')
    with pytest.raises(config.ConfigAdmissionError, match='CONFIG_ADMISSION_SOURCE_CHANGED'):
        snapshot.assert_current()


@pytest.mark.parametrize('invalid', ['secrets: [broken\n', '- nonmapping\n', 'x: 1\nx: 2\n'])
def test_snapshot_refuses_invalid_source(tmp_path, invalid):
    (tmp_path / 'config.yaml').write_text(invalid)
    with pytest.raises(config.ConfigAdmissionError, match='CONFIG_ADMISSION_INVALID_SOURCE'):
        config.capture_security_policy_config(tmp_path)


def test_snapshot_detects_creation_and_deletion(tmp_path):
    path = tmp_path / 'config.yaml'
    absent = config.capture_security_policy_config(tmp_path)
    assert absent.config == {}
    absent.assert_current()
    path.write_text('{}\n')
    with pytest.raises(config.ConfigAdmissionError, match='CONFIG_ADMISSION_SOURCE_CHANGED'):
        absent.assert_current()
    present = config.capture_security_policy_config(tmp_path)
    path.unlink()
    with pytest.raises(config.ConfigAdmissionError, match='CONFIG_ADMISSION_SOURCE_CHANGED'):
        present.assert_current()


def test_ambient_snapshot_refuses_context_profile_switch(tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    (first / 'config.yaml').write_text('{}\n')
    (second / 'config.yaml').write_text('{}\n')
    first_token = set_hermes_home_override(first)
    try:
        snapshot = config.capture_security_policy_config()
        second_token = set_hermes_home_override(second)
        try:
            with pytest.raises(config.ConfigAdmissionError, match='CONFIG_ADMISSION_SOURCE_CHANGED'):
                snapshot.assert_current()
        finally:
            reset_hermes_home_override(second_token)
        snapshot.assert_current()
    finally:
        reset_hermes_home_override(first_token)


def test_explicit_snapshot_remains_bound_to_explicit_profile(tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    (first / 'config.yaml').write_text('{}\n')
    (second / 'config.yaml').write_text('{}\n')
    token = set_hermes_home_override(second)
    try:
        snapshot = config.capture_security_policy_config(first)
        snapshot.assert_current()
    finally:
        reset_hermes_home_override(token)


def test_snapshot_refuses_dangling_policy_link(tmp_path):
    try:
        (tmp_path / 'config.yaml').symlink_to(tmp_path / 'missing')
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable')
    with pytest.raises(config.ConfigAdmissionError, match='CONFIG_ADMISSION_SOURCE_UNAVAILABLE'):
        config.capture_security_policy_config(tmp_path)


def test_snapshot_refuses_symlink_retarget_even_when_bytes_match(tmp_path):
    first = tmp_path / 'first.yaml'
    second = tmp_path / 'second.yaml'
    link = tmp_path / 'config.yaml'
    first.write_text('{}\n')
    second.write_text('{}\n')
    try:
        link.symlink_to(first)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable')
    snapshot = config.capture_security_policy_config(tmp_path)
    link.unlink()
    link.symlink_to(second)
    with pytest.raises(config.ConfigAdmissionError, match='CONFIG_ADMISSION_SOURCE_CHANGED'):
        snapshot.assert_current()
    link.unlink()
    link.symlink_to(tmp_path / 'missing')
    with pytest.raises(config.ConfigAdmissionError, match='CONFIG_ADMISSION_SOURCE_UNAVAILABLE'):
        snapshot.assert_current()


def test_snapshot_recheck_refuses_unreadable_source(tmp_path, monkeypatch):
    path = tmp_path / 'config.yaml'
    path.write_text('{}\n')
    snapshot = config.capture_security_policy_config(tmp_path)
    original = Path.read_bytes

    def denied(self):
        if self == path:
            raise PermissionError('synthetic denial')
        return original(self)
    monkeypatch.setattr(Path, 'read_bytes', denied)
    with pytest.raises(config.ConfigAdmissionError, match='CONFIG_ADMISSION_SOURCE_UNAVAILABLE'):
        snapshot.assert_current()
