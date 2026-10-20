"""C04 offline policy admission; inactivity is deliberately NOT qualified yet."""
from concurrent.futures import Future
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import threading

import pytest

from hermes_cli import config
from tools import delegate_tool as delegate


@pytest.fixture(autouse=True)
def empty_config_home(monkeypatch, tmp_path):
    home = tmp_path / 'empty-home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('HERMES_HOME', str(home / '.hermes'))
    monkeypatch.setenv('ARES_HOME', str(home / '.ares'))
    monkeypatch.delenv('DELEGATION_CHILD_TIMEOUT_SECONDS', raising=False)
    monkeypatch.delenv('HERMES_IGNORE_USER_CONFIG', raising=False)
    monkeypatch.delenv('HERMES_MANAGED_DIR', raising=False)
    config._LOAD_CONFIG_CACHE.clear()
    config._LAST_EXPANDED_CONFIG_BY_PATH.clear()


def test_absent_timeout_policy_preserves_legacy_no_cap(monkeypatch):
    monkeypatch.setattr(delegate, '_load_config', lambda: {})
    assert delegate._get_child_timeout() is None


def test_real_canonical_loader_absence_preserves_total_zero_no_cap():
    loaded = config.load_config_readonly()
    assert loaded['delegation']['child_timeout_mode'] == 'total'
    assert loaded['delegation']['child_timeout_seconds'] == 0
    assert delegate._get_child_timeout() is None


def test_real_canonical_loader_and_delegate_keep_explicit_total_600():
    config.set_config_value('delegation.child_timeout_seconds', '600')
    config.set_config_value('delegation.child_timeout_mode', 'total')
    loaded = config.load_config()
    assert loaded['delegation']['child_timeout_seconds'] == 600
    assert loaded['delegation']['child_timeout_mode'] == 'total'
    assert delegate._get_child_timeout() == 600.0
    assert config._validate_config_key('delegation.child_timeout_mode')[0]


@pytest.mark.parametrize('value, expected', [(0, None), (-1, None), ('-2', None),
    (None, None), (1, 30.0), ('60.5', 60.5), (600, 600.0)])
def test_explicit_total_keeps_legacy_budget_and_floor(monkeypatch, value, expected):
    monkeypatch.setattr(delegate, '_load_config', lambda: {
        'child_timeout_mode': 'total', 'child_timeout_seconds': value})
    assert delegate._get_child_timeout() == expected


@pytest.mark.parametrize('mode', [None, True, False, 0, 1, [], {}, '',
    'TOTAL', ' total', 'inactivity ', 'wall', 'heartbeat'])
def test_invalid_mode_never_relaxes_cap(monkeypatch, mode):
    monkeypatch.setattr(delegate, '_load_config', lambda: {
        'child_timeout_mode': mode, 'child_timeout_seconds': 0})
    with pytest.raises(delegate.DelegationTimeoutConfigError,
                       match='DELEGATION_INVALID_TIMEOUT_MODE'):
        delegate._get_child_timeout()


@pytest.mark.parametrize('value', [True, False, [], {}, '', 'oops', float('nan'),
    float('inf'), '-inf', '1e100', threading.TIMEOUT_MAX * 2, 10 ** 1000])
def test_invalid_total_budget_is_typed(monkeypatch, value):
    monkeypatch.setattr(delegate, '_load_config', lambda: {
        'child_timeout_mode': 'total', 'child_timeout_seconds': value})
    with pytest.raises(delegate.DelegationTimeoutConfigError,
                       match='DELEGATION_INVALID_TIMEOUT'):
        delegate._get_child_timeout()


@pytest.mark.parametrize('cap', [600, 0, -1, None])
def test_inactivity_refuses_before_reservation_or_child_creation(monkeypatch, cap):
    monkeypatch.setattr(delegate, '_load_config', lambda: {
        'child_timeout_mode': 'inactivity', 'child_timeout_seconds': cap})
    invoked = []
    monkeypatch.setattr(delegate, '_build_child_agent', lambda **kw: invoked.append(kw))
    parent = SimpleNamespace(session_id='c04-no-progress-owner',
        get_activity_summary=lambda: {'last_activity_ts': 999999, 'api_call_count': 999,
                                     'current_tool': 'terminal'},
        _active_children=[], _active_children_lock=threading.Lock())
    before = delegate.list_active_subagents()
    with pytest.raises(delegate.DelegationProgressUnavailableError,
                       match='DELEGATION_PROGRESS_UNAVAILABLE'):
        delegate._build_child_preserving_parent_tools(goal='offline', parent_agent=parent)
    assert invoked == []
    assert delegate.list_active_subagents() == before


def test_inactivity_invalid_budget_is_not_hidden_by_missing_wiring(monkeypatch):
    monkeypatch.setattr(delegate, '_load_config', lambda: {
        'child_timeout_mode': 'inactivity', 'child_timeout_seconds': True})
    with pytest.raises(delegate.DelegationTimeoutConfigError,
                       match='DELEGATION_INVALID_TIMEOUT'):
        delegate._get_child_timeout()


@pytest.mark.parametrize('mode', [None, True, 'TOTAL', 'heartbeat'])
def test_config_owner_reports_invalid_mode(mode):
    issues = config.validate_config_structure({'delegation': {'child_timeout_mode': mode}})
    assert any('DELEGATION_INVALID_TIMEOUT_MODE' in issue.message for issue in issues)


@pytest.mark.parametrize('value', ['heartbeat', 'TOTAL', 'true', 'null', ' total'])
def test_config_set_invalid_mode_refuses_before_write(value):
    path = config.get_config_path()
    with pytest.raises(config.DelegationTimeoutConfigError,
                       match='DELEGATION_INVALID_TIMEOUT_MODE'):
        config.set_config_value('delegation.child_timeout_mode', value)
    assert not path.exists()


def test_config_set_total_roundtrip_and_reload():
    config.set_config_value('delegation.child_timeout_mode', 'total')
    config.set_config_value('delegation.child_timeout_seconds', '600')
    assert delegate._get_child_timeout() == 600.0
    config._LOAD_CONFIG_CACHE.clear()
    assert config.load_config_readonly()['delegation']['child_timeout_mode'] == 'total'
    assert delegate._get_child_timeout() == 600.0


def test_config_set_inactivity_roundtrip_but_runtime_is_blocked():
    config.set_config_value('delegation.child_timeout_mode', 'inactivity')
    assert config.load_config()['delegation']['child_timeout_mode'] == 'inactivity'
    with pytest.raises(delegate.DelegationProgressUnavailableError):
        delegate._get_child_timeout()


def test_admitted_total_policy_does_not_reread_changed_config(monkeypatch):
    state = {'child_timeout_mode': 'total', 'child_timeout_seconds': 600}
    monkeypatch.setattr(delegate, '_load_config', lambda: dict(state))
    monkeypatch.setattr(delegate, '_get_max_concurrent_children', lambda: 10)
    monkeypatch.setattr(delegate, '_get_worktree_isolation', lambda: False)
    parent = SimpleNamespace(session_id='c04-policy-parent', _current_task_id=None,
        _active_children=[], _active_children_lock=threading.Lock())
    child = SimpleNamespace(_subagent_id='c04-policy-child', session_id='c04-policy-child',
        tool_progress_callback=None, close=lambda: None,
        run_conversation=lambda **kw: {'final_response': 'offline complete', 'api_calls': 0},
        get_activity_summary=lambda: {'api_call_count': 0})
    monkeypatch.setattr(delegate, '_build_child_agent', lambda **kw: child)
    caller_done = Future()
    try:
        child = delegate._build_child_preserving_parent_tools(goal='offline', parent_agent=parent,
            _caller_initialization_done=caller_done)
    finally:
        caller_done.set_result(None)
    policy = child._delegate_lifetime_policy
    assert policy.mode == 'total' and policy.timeout_seconds == 600.0
    with pytest.raises(FrozenInstanceError):
        policy.mode = 'inactivity'
    deadline = child._delegate_deadline
    state.update(child_timeout_mode='inactivity', child_timeout_seconds=0)
    try:
        result = delegate._run_single_child(0, 'offline', child, parent)
        assert result['status'] == 'completed'
        assert child._delegate_deadline == deadline
        assert child._delegate_lifetime_policy is policy
    finally:
        delegate._unregister_subagent(child._subagent_id, agent=child)


def test_total_wait_uses_monotonic_and_never_activity_ticks(monkeypatch):
    future = Future()
    clock = iter([100.0, 110.0])
    monkeypatch.setattr(delegate.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(delegate.time, 'time', lambda: -9999999.0)
    waits = []
    def wait(fs, timeout):
        waits.append(timeout)
        return set(), {future}
    monkeypatch.setattr(delegate, 'wait_futures', wait)
    assert delegate._wait_child_attempt(future, 105.0) is delegate.ChildWaitOutcome.EXPIRED
    assert waits == [5.0]


def test_no_new_model_tool_per_call_lifetime_parameters():
    properties = delegate.DELEGATE_TASK_SCHEMA['parameters']['properties']
    assert 'child_timeout_mode' not in properties
    assert 'child_timeout_seconds' not in properties
    assert 'child_timeout_mode' not in properties['tasks']['items']['properties']


@pytest.mark.parametrize('failure', [OSError, PermissionError, RuntimeError])
def test_canonical_read_failure_never_borrows_legacy_no_cap(monkeypatch, failure):
    import sys

    config.set_config_value('delegation.child_timeout_seconds', '600')
    assert delegate._get_child_timeout() == 600.0
    monkeypatch.setitem(sys.modules, 'cli', SimpleNamespace(
        CLI_CONFIG={'delegation': {'child_timeout_seconds': 0}}))
    def refuse():
        raise failure('neutral canonical reader refusal')
    monkeypatch.setattr(config, 'read_delegation_config_policy', refuse)
    invoked = []
    monkeypatch.setattr(delegate, '_build_child_agent', lambda **kw: invoked.append(kw))
    before = delegate.list_active_subagents()
    with pytest.raises(ValueError, match='DELEGATION_CONFIG_UNAVAILABLE'):
        delegate._build_child_preserving_parent_tools(goal='offline', parent_agent=None)
    assert invoked == []
    assert delegate.list_active_subagents() == before


@pytest.mark.parametrize('payload', [None, [], True, 7,
    {'delegation': None}, {'delegation': []}, {'delegation': True}, {'delegation': 'invalid'}])
def test_invalid_canonical_policy_snapshot_cannot_become_unbounded(monkeypatch, payload):
    import sys

    monkeypatch.setitem(sys.modules, 'cli', SimpleNamespace(
        CLI_CONFIG={'delegation': {'child_timeout_seconds': 0}}))
    monkeypatch.setattr(config, 'read_delegation_config_policy', lambda: payload)
    with pytest.raises(ValueError, match='DELEGATION_CONFIG_INVALID'):
        delegate._get_child_lifetime_policy()


def test_explicit_ignore_user_config_retains_legacy_owner(monkeypatch):
    import sys

    monkeypatch.setenv('HERMES_IGNORE_USER_CONFIG', '1')
    monkeypatch.setitem(sys.modules, 'cli', SimpleNamespace(
        CLI_CONFIG={'delegation': {'child_timeout_seconds': 600, 'child_timeout_mode': 'total'}}))
    def forbidden_canonical_read():
        raise AssertionError('Explicit ignore-user-config consulted suppressed owner')
    monkeypatch.setattr(config, 'read_delegation_config_policy', forbidden_canonical_read)
    assert delegate._get_child_timeout() == 600.0
