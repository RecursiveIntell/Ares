"""Strict delegation admission shares config ownership, not lenient fallbacks."""
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from hermes_cli import config
from hermes_cli import managed_scope
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import delegate_tool as delegate


@pytest.fixture(autouse=True)
def neutral_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('HERMES_MANAGED_DIR', raising=False)
    monkeypatch.delenv('HERMES_IGNORE_USER_CONFIG', raising=False)
    monkeypatch.delenv('DELEGATION_CHILD_TIMEOUT_SECONDS', raising=False)
    token = set_hermes_home_override(None)
    config._LOAD_CONFIG_CACHE.clear()
    config._RAW_CONFIG_CACHE.clear()
    config._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    config._POLICY_CONFIG_OBSERVED.clear()
    config._POLICY_MANAGED_PATHS.clear()
    managed_scope.invalidate_managed_cache()
    yield tmp_path
    reset_hermes_home_override(token)


def test_absent_never_observed_policy_preserves_legacy_default(neutral_home):
    policy = config.read_delegation_config_policy()
    assert policy.status is config.ConfigPolicyStatus.ABSENT
    policy.validate()
    assert delegate._get_child_timeout() is None


@pytest.mark.parametrize('warm', [False, True])
@pytest.mark.parametrize('bad', ['delegation: [unterminated', 'null', '', '[one, two]', 'false'])
def test_cold_or_cached_bad_config_cannot_admit_defaults(neutral_home, warm, bad):
    path = neutral_home / 'config.yaml'
    if warm:
        path.write_text('delegation:\n  child_timeout_seconds: 600\n')
        assert config.load_config_readonly()['delegation']['child_timeout_seconds'] == 600
    path.write_text(bad)
    # Deliberately populate the lenient cache first. The admission owner may
    # not promote defaults or stale LKG from that cache into fresh authority.
    config.load_config_readonly()
    with pytest.raises(config.ConfigPolicyError):
        config.read_delegation_config_policy()
    with pytest.raises(delegate.DelegationConfigError):
        delegate._get_child_timeout()


def test_strict_reader_leaves_legacy_lkg_behavior_unchanged(neutral_home):
    path = neutral_home / 'config.yaml'
    path.write_text('delegation:\n  child_timeout_seconds: 600\n')
    assert config.load_config_readonly()['delegation']['child_timeout_seconds'] == 600
    path.write_text('delegation: [unterminated')
    assert config.load_config_readonly()['delegation']['child_timeout_seconds'] == 600
    with pytest.raises(config.ConfigPolicyError):
        config.read_delegation_config_policy()


@pytest.mark.parametrize('value', ['null', '[]', 'true', 'invalid'])
def test_nonmapping_delegation_section_refuses(neutral_home, value):
    (neutral_home / 'config.yaml').write_text('delegation: ' + value)
    with pytest.raises(config.ConfigPolicyError) as caught:
        config.read_delegation_config_policy()
    assert caught.value.status is config.ConfigPolicyStatus.NONMAPPING


def test_managed_leaf_precedence_and_environment_expansion(neutral_home, monkeypatch):
    (neutral_home / 'config.yaml').write_text(
        'delegation:\n  child_timeout_seconds: 600\n  max_iterations: 7\n')
    managed = neutral_home / 'managed'
    managed.mkdir()
    (managed / 'config.yaml').write_text('delegation:\n  child_timeout_seconds: ${NEUTRAL_SECONDS}\n')
    monkeypatch.setenv('HERMES_MANAGED_DIR', str(managed))
    monkeypatch.setenv('NEUTRAL_SECONDS', '700')
    policy = config.read_delegation_config_policy()
    assert policy.values['child_timeout_seconds'] == '700'
    assert policy.values['max_iterations'] == 7
    assert delegate._get_child_timeout() == 700.0
    policy.validate()


def test_malformed_managed_policy_is_not_optional_absence(neutral_home, monkeypatch):
    (neutral_home / 'config.yaml').write_text('delegation:\n  child_timeout_seconds: 600\n')
    managed = neutral_home / 'managed'
    managed.mkdir()
    (managed / 'config.yaml').write_text('delegation: [unterminated')
    monkeypatch.setenv('HERMES_MANAGED_DIR', str(managed))
    with pytest.raises(config.ConfigPolicyError):
        config.read_delegation_config_policy()
    with pytest.raises(delegate.DelegationConfigError):
        delegate._get_child_timeout()


def test_explicit_unavailable_managed_owner_refuses(neutral_home, monkeypatch):
    monkeypatch.setenv('HERMES_MANAGED_DIR', str(neutral_home / 'absent-managed'))
    with pytest.raises(config.ConfigPolicyError) as caught:
        config.read_delegation_config_policy()
    assert caught.value.status is config.ConfigPolicyStatus.UNAVAILABLE


def test_snapshot_is_immutable_and_returned_values_do_not_rewrite_it(neutral_home):
    (neutral_home / 'config.yaml').write_text(
        'delegation:\n  child_timeout_seconds: 600\n  api_key: neutral-sensitive-value\n')
    policy = config.read_delegation_config_policy()
    values = policy.values
    values['child_timeout_seconds'] = 0
    assert policy.values['child_timeout_seconds'] == 600
    assert 'neutral-sensitive-value' not in repr(policy)
    with pytest.raises(FrozenInstanceError):
        policy.status = config.ConfigPolicyStatus.ABSENT


def test_known_authority_removal_refuses(neutral_home):
    path = neutral_home / 'config.yaml'
    path.write_text('delegation:\n  child_timeout_seconds: 600\n')
    config.read_delegation_config_policy()
    path.unlink()
    with pytest.raises(config.ConfigPolicyError) as caught:
        config.read_delegation_config_policy()
    assert caught.value.status is config.ConfigPolicyStatus.REMOVED


def test_snapshot_rejects_profile_rehome_and_file_change(neutral_home):
    path = neutral_home / 'config.yaml'
    path.write_text('delegation:\n  child_timeout_seconds: 600\n')
    policy = config.read_delegation_config_policy()
    token = set_hermes_home_override(str(neutral_home / 'other'))
    try:
        with pytest.raises(config.ConfigPolicyError) as caught:
            policy.validate()
        assert caught.value.status is config.ConfigPolicyStatus.CHANGED
    finally:
        reset_hermes_home_override(token)
    path.write_text('delegation:\n  child_timeout_seconds: 500\n')
    with pytest.raises(config.ConfigPolicyError) as caught:
        policy.validate()
    assert caught.value.status is config.ConfigPolicyStatus.CHANGED


def test_permission_failure_refuses_before_reservation(neutral_home, monkeypatch):
    path = neutral_home / 'config.yaml'
    path.write_text('delegation:\n  child_timeout_seconds: 600\n')
    real_open = open
    def refuse(file, *args, **kwargs):
        if Path(file) == path:
            raise PermissionError('neutral reader refusal')
        return real_open(file, *args, **kwargs)
    monkeypatch.setattr('builtins.open', refuse)
    invoked = []
    monkeypatch.setattr(delegate, '_build_child_agent', lambda **kw: invoked.append(kw))
    before = delegate.list_active_subagents()
    with pytest.raises(delegate.DelegationConfigError):
        delegate._build_child_preserving_parent_tools(goal='offline', parent_agent=None)
    assert invoked == []
    assert delegate.list_active_subagents() == before
