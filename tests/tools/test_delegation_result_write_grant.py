"""Run-scoped digest observations through the actual local file writer."""
import json
import time

import pytest

from tools.delegation_partial_file import reserve_result_file
from tools.delegation_result_write import register_result_write
from tools.file_tools import write_file_tool


@pytest.fixture
def artifact(tmp_path, monkeypatch, delegation_policy_parent):
    from types import SimpleNamespace
    from agent.orchestration_runtime import admit_worker

    monkeypatch.setenv('TERMINAL_ENV', 'local')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    with reserve_result_file(workspace, 'result.md') as value:
        value._fixture_child = SimpleNamespace(
            _subagent_id='child-1', session_id='child-session',
            _orchestration_admission=admit_worker(delegation_policy_parent),
        )
        yield value


def write(artifact, content, *, task='child-1', session='child-session'):
    return json.loads(write_file_tool(str(artifact.path), content,
                                      task_id=task, session_id=session))


def grant(artifact):
    return register_result_write(
        artifact, child=artifact._fixture_child, deadline=time.monotonic() + 60,
    )


def test_admitted_child_registration_derives_identity_and_recovers(artifact, delegation_policy_parent):
    from types import SimpleNamespace
    from agent.orchestration_runtime import admit_worker

    child = SimpleNamespace(_subagent_id='child-1', session_id='child-session',
                            _orchestration_admission=admit_worker(delegation_policy_parent))
    with register_result_write(artifact, child=child, deadline=time.monotonic() + 60) as ticket:
        assert write(artifact, 'admitted content').get('verified') is True
        assert artifact.recover(expected_sha256=ticket.freeze()).text == 'admitted content'


def test_unadmitted_child_cannot_register(artifact):
    from types import SimpleNamespace
    from agent.orchestration_policy import OrchestrationError

    child = SimpleNamespace(_subagent_id='child-1', session_id='child-session')
    with pytest.raises(OrchestrationError, match='policy_owner_required'):
        register_result_write(artifact, child=child, deadline=time.monotonic() + 60)


def test_revoked_admission_withholds_previously_observed_digest(artifact, delegation_policy_parent):
    from types import SimpleNamespace
    from agent.orchestration_policy import OrchestrationPolicy
    from agent.orchestration_runtime import admit_worker

    child = SimpleNamespace(_subagent_id='child-1', session_id='child-session',
                            _orchestration_admission=admit_worker(delegation_policy_parent))
    with register_result_write(artifact, child=child, deadline=time.monotonic() + 60) as ticket:
        assert write(artifact, 'would otherwise be accepted').get('verified') is True
        assert ticket._digest is not None  # prove the positive path actually recorded
        delegation_policy_parent._session_db.set_orchestration_policy(
            delegation_policy_parent.session_id,
            OrchestrationPolicy(allow_delegation=False).to_dict(),
            expected_revision=0, origin='operator-command',
        )
        assert ticket.freeze() is None
        assert artifact.recover().status == 'unverified'


def test_revocation_during_write_prevents_digest(artifact, delegation_policy_parent, monkeypatch):
    from agent.orchestration_policy import OrchestrationPolicy
    from tools import delegation_result_write as owner

    original = owner.record_verified_local_write
    observed = []
    def revoke_then_record(*args, **kwargs):
        delegation_policy_parent._session_db.set_orchestration_policy(
            delegation_policy_parent.session_id,
            OrchestrationPolicy(allow_delegation=False).to_dict(),
            expected_revision=0, origin='operator-command',
        )
        observed.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, 'record_verified_local_write', revoke_then_record)
    with grant(artifact) as ticket:
        assert write(artifact, 'written after revocation').get('verified') is True
        assert observed == [True]
        assert ticket._digest is None
        assert ticket.freeze() is None


def test_changed_child_identity_withholds_recorded_digest(artifact):
    with grant(artifact) as ticket:
        assert write(artifact, 'initial').get('verified') is True
        assert ticket._digest is not None
        artifact._fixture_child.session_id = 'changed-session'
        assert ticket.freeze() is None


@pytest.mark.parametrize('freeze_first', [False, True])
@pytest.mark.parametrize('payload', ['x' * 65537, 'é' * 32769], ids=['ascii', 'multibyte'])
def test_oversize_reserved_write_is_rejected_before_disk_io(artifact, freeze_first, payload):
    with grant(artifact) as ticket:
        if freeze_first:
            assert ticket.freeze() is None
        result = write(artifact, payload)
        assert result.get('error'), result
        assert artifact.path.stat().st_size == 0


def test_absolute_reserved_path_rejects_oversize_when_resolution_fails(artifact, monkeypatch):
    from tools import file_tools

    def fail_resolution(*args, **kwargs):
        raise OSError('synthetic resolution failure')

    with grant(artifact):
        monkeypatch.setattr(file_tools, '_resolve_path_for_task', fail_resolution)
        result = write(artifact, 'x' * 65537)
        assert result.get('error'), result
        assert artifact.path.stat().st_size == 0


@pytest.mark.parametrize('freeze_first', [False, True])
def test_unresolved_owner_alias_cannot_bypass_reserved_size_limit(artifact, monkeypatch, freeze_first):
    from tools import file_tools
    def fail_resolution(*args, **kwargs):
        raise OSError('synthetic resolution failure')
    alias = str(artifact.path.parent) + '/./' + artifact.path.name
    with grant(artifact) as ticket:
        if freeze_first:
            ticket.freeze()
        monkeypatch.setattr(file_tools, '_resolve_path_for_task', fail_resolution)
        result = json.loads(write_file_tool(alias, 'x' * 65537,
                                            task_id='child-1', session_id='child-session'))
        assert result.get('error'), result
        assert artifact.path.stat().st_size == 0


def test_unresolved_ordinary_write_is_bounded_only_for_reservation_owner(artifact, monkeypatch, tmp_path):
    from tools import file_tools
    def fail_resolution(*args, **kwargs):
        raise OSError('synthetic resolution failure')
    monkeypatch.setattr(file_tools, '_resolve_path_for_task', fail_resolution)
    path = tmp_path / 'ordinary.txt'
    with grant(artifact):
        oversized = json.loads(write_file_tool(str(path), 'x' * 65537,
                                               task_id='child-1', session_id='child-session'))
        assert oversized.get('error')
        assert not path.exists()
        small = json.loads(write_file_tool(str(path), 'ok',
                                           task_id='child-1', session_id='child-session'))
        assert small.get('verified') is True
    after = json.loads(write_file_tool(str(path), 'x' * 65537,
                                       task_id='child-1', session_id='child-session'))
    assert after.get('verified') is True
    assert path.stat().st_size == 65537


@pytest.mark.parametrize('resolution_fails', [False, True])
def test_oversize_rejection_never_calls_backend_writer(artifact, monkeypatch, resolution_fails):
    from tools import file_tools
    from unittest.mock import Mock

    ops = file_tools._get_file_ops('child-1')
    writer = Mock(side_effect=AssertionError('writer must not be called'))
    monkeypatch.setattr(ops, 'write_file', writer)
    if resolution_fails:
        def fail_resolution(*args, **kwargs):
            raise OSError('synthetic resolution failure')
        monkeypatch.setattr(file_tools, '_resolve_path_for_task', fail_resolution)
    with grant(artifact):
        result = write(artifact, 'x' * 65537)
        assert result.get('error'), result
        writer.assert_not_called()
        assert artifact.path.stat().st_size == 0


def test_unregistered_file_is_not_subject_to_result_limit(artifact, tmp_path):
    path = tmp_path / 'ordinary.md'
    with grant(artifact):
        result = json.loads(write_file_tool(str(path), 'x' * 65537,
                                            task_id='child-1', session_id='child-session'))
        assert result.get('verified') is True, result
        assert path.stat().st_size == 65537


def test_result_write_accepts_exact_byte_limit(artifact):
    from tools.delegation_partial_file import _MAX_ALLOWED_BYTES
    with grant(artifact) as ticket:
        result = write(artifact, 'é' * (_MAX_ALLOWED_BYTES // 2))
        assert result.get('verified') is True, result
        assert ticket.freeze() is not None


def test_interrupted_discard_rejects_wrong_reservation_and_child(artifact):
    from types import SimpleNamespace
    from tools.delegation_result_write import discard_interrupted_registration

    with grant(artifact) as ticket:
        discard_interrupted_registration(artifact, SimpleNamespace())
        foreign = SimpleNamespace(path=artifact.path)
        discard_interrupted_registration(foreign, artifact._fixture_child)
        assert not ticket._closed
        assert write(artifact, 'still admitted').get('verified') is True
        assert ticket.freeze() is not None


def test_real_local_verified_write_can_supply_exact_digest(artifact):
    with grant(artifact) as ticket:
        result = write(artifact, 'synthetic partial')
        assert result.get('verified') is True and not result.get('error'), result
        expected = ticket.freeze()
        assert expected is not None
        recovered = artifact.recover(expected_sha256=expected)
        assert recovered.status == 'available'
        assert recovered.text == 'synthetic partial'


def test_real_tool_dispatch_preserves_registered_child_identity(artifact):
    import model_tools

    model_tools.get_tool_definitions(enabled_toolsets=['file'])
    with grant(artifact) as ticket:
        raw = model_tools.handle_function_call(
            'write_file', {'path': str(artifact.path), 'content': 'routed finding'},
            task_id='child-1', session_id='child-session', enabled_tools=['write_file'],
        )
        result = json.loads(raw)
        assert result.get('verified') is True and not result.get('error'), result
        expected = ticket.freeze()
        assert expected is not None
        assert artifact.recover(expected_sha256=expected).text == 'routed finding'


@pytest.mark.parametrize(('task', 'session'), [
    ('foreign-child', 'child-session'), ('child-1', 'foreign-session'),
    ('child-1', None),
])
def test_other_identity_write_never_establishes_digest(artifact, task, session):
    with grant(artifact) as ticket:
        result = write(artifact, 'foreign content', task=task, session=session)
        assert result.get('verified') is True and not result.get('error'), result
        assert ticket.freeze() is None
        assert artifact.recover().status == 'unverified'


def test_digest_does_not_authorize_later_replacement(artifact):
    with grant(artifact) as ticket:
        assert write(artifact, 'approved').get('verified') is True
        expected = ticket.freeze()
        artifact.path.write_text('substituted')
        assert artifact.recover(expected_sha256=expected).status == 'unverified'


def test_latest_verified_write_is_the_only_recoverable_version(artifact):
    with grant(artifact) as ticket:
        assert write(artifact, 'first').get('verified') is True
        assert write(artifact, 'second').get('verified') is True
        expected = ticket.freeze()
        assert artifact.recover(expected_sha256=expected).text == 'second'
        artifact.path.write_text('first')
        assert artifact.recover(expected_sha256=expected).status == 'unverified'


def test_write_for_different_path_does_not_supply_digest(artifact, tmp_path):
    with grant(artifact) as ticket:
        result = json.loads(write_file_tool(str(tmp_path / 'other.md'), 'not a result',
                                            task_id='child-1', session_id='child-session'))
        assert result.get('verified') is True and not result.get('error'), result
        assert ticket.freeze() is None


def test_grant_refuses_write_after_deadline(artifact):
    with register_result_write(artifact, child=artifact._fixture_child,
                               deadline=time.monotonic() + 0.2) as ticket:
        time.sleep(0.22)
        assert write(artifact, 'past deadline').get('verified') is True
        assert ticket.freeze() is None


@pytest.mark.parametrize('verified', [None, False, 1, 'true'])
def test_unverified_file_operation_cannot_create_digest(artifact, verified):
    from tools.delegation_result_write import record_verified_local_write
    from tools.file_operations import ShellFileOperations
    from tools.environments.local import LocalEnvironment

    with grant(artifact) as ticket:
        # Construct without starting a process; the tested property is the
        # observer's strict flag check, not the backend's write behavior.
        env = object.__new__(LocalEnvironment)
        ops = ShellFileOperations(env)
        record_verified_local_write(ticket, 'child-1', 'child-session', str(artifact.path),
                                    'PRIVATE', {'verified': verified}, ops)
        assert ticket.freeze() is None


def test_remote_backend_cannot_create_digest(artifact):
    from types import SimpleNamespace
    from tools.delegation_result_write import record_verified_local_write
    from tools.file_operations import ShellFileOperations

    with grant(artifact) as ticket:
        ops = ShellFileOperations(SimpleNamespace(cwd='/workspace'))
        record_verified_local_write(ticket, 'child-1', 'child-session', str(artifact.path),
                                    'PRIVATE', {'verified': True}, ops)
        assert ticket.freeze() is None


def test_plugin_stamped_local_instance_cannot_supply_digest(artifact, monkeypatch):
    from agent import terminal_env_registry
    from agent.terminal_env_provider import TerminalEnvironmentProvider
    from tools import file_tools, terminal_tool
    from tools.delegation_result_write import begin_result_write, record_verified_local_write
    from tools.file_operations import ShellFileOperations

    local_ops = file_tools._get_file_ops('child-1')
    # The factory mutates this shared cached instance; restore its original
    # attribute state after the test so subsequent workers are unaffected.
    monkeypatch.setattr(local_ops.env, '_hermes_backend_name', None, raising=False)

    class FixtureProvider(TerminalEnvironmentProvider):
        name = 'remote_fixture'
        is_remote = True
        is_container = True

        def is_available(self):
            return True

        def create_environment(self, **kwargs):
            return local_ops.env

    monkeypatch.setitem(terminal_env_registry._providers, 'remote_fixture', FixtureProvider())
    env = terminal_tool._create_environment('remote_fixture', '', '/workspace', 30, task_id='child-1')
    assert env is local_ops.env
    assert env._hermes_backend_name == 'remote_fixture'
    ops = ShellFileOperations(env)
    with grant(artifact) as ticket:
        observation = begin_result_write('child-1', 'child-session', str(artifact.path))
        assert observation is ticket
        result = ops.write_file(str(artifact.path), 'synthetic plugin content').to_dict()
        assert result.get('verified') is True and not result.get('error'), result
        record_verified_local_write(observation, 'child-1', 'child-session', str(artifact.path),
                                    'synthetic plugin content', result, ops)
        assert ticket.freeze() is None


def test_recorded_digest_is_withheld_if_backend_identity_changes(artifact, monkeypatch):
    from tools import terminal_tool

    with grant(artifact) as ticket:
        assert write(artifact, 'initial local content').get('verified') is True
        assert ticket._digest is not None
        env = terminal_tool.get_active_env('child-1')
        assert env is not None
        monkeypatch.setattr(env, '_hermes_backend_name', 'remote_fixture', raising=False)
        assert ticket.freeze() is None


def test_recorded_digest_is_withheld_if_active_backend_is_replaced(artifact, monkeypatch):
    from types import SimpleNamespace
    from tools import terminal_tool

    with grant(artifact) as ticket:
        assert write(artifact, 'initial local content').get('verified') is True
        assert ticket._digest is not None
        key = terminal_tool._resolve_container_task_id('child-1')
        assert terminal_tool.get_active_env('child-1') is terminal_tool._active_environments[key]
        monkeypatch.setitem(terminal_tool._active_environments, key, SimpleNamespace())
        assert ticket.freeze() is None


def test_recorded_digest_is_withheld_if_backend_lookup_fails(artifact, monkeypatch):
    from tools import terminal_tool

    with grant(artifact) as ticket:
        assert write(artifact, 'initial local content').get('verified') is True
        assert ticket._digest is not None
        def unavailable(_task_id):
            raise OSError('synthetic backend lookup failure')
        monkeypatch.setattr(terminal_tool, 'get_active_env', unavailable)
        assert ticket.freeze() is None


def test_freeze_prevents_recording_while_admission_check_is_inflight(artifact, monkeypatch):
    import threading
    from tools import delegation_result_write as owner

    entered = threading.Event()
    resume = threading.Event()
    original = owner._admission_current
    results = []

    def delayed(grant):
        if threading.current_thread().name == 'freeze-test':
            entered.set()
            assert resume.wait(10)
        return original(grant)

    monkeypatch.setattr(owner, '_admission_current', delayed)
    with grant(artifact) as ticket:
        thread = threading.Thread(target=lambda: results.append(ticket.freeze()), name='freeze-test')
        thread.start()
        try:
            assert entered.wait(10)
            assert write(artifact, 'late during freeze').get('verified') is True
        finally:
            resume.set()
            thread.join(timeout=10)
        assert not thread.is_alive()
        assert results == [None]
        assert ticket.freeze() is None


def test_frozen_grant_does_not_capture_late_write(artifact):
    with grant(artifact) as ticket:
        assert ticket.freeze() is None
        assert write(artifact, 'too late').get('verified') is True
        assert ticket.freeze() is None


def test_inflight_write_from_old_grant_cannot_fill_new_grant(artifact, monkeypatch):
    import threading
    from tools import delegation_result_write as owner

    entered = threading.Event()
    resume = threading.Event()
    finished = threading.Event()
    original = owner.record_verified_local_write
    results = []

    def delayed_record(*args, **kwargs):
        entered.set()
        assert resume.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, 'record_verified_local_write', delayed_record)
    old = grant(artifact)
    def worker():
        try:
            results.append(write(artifact, 'old-run content'))
        finally:
            finished.set()
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(10)
        old.close()
        with grant(artifact) as new:
            resume.set()
            assert finished.wait(10)
            assert results[0].get('verified') is True
            assert new.freeze() is None
    finally:
        resume.set()
        thread.join(timeout=10)
        old.close()
    assert not thread.is_alive()


def test_duplicate_grant_is_refused_and_close_releases_registration(artifact):
    with grant(artifact):
        with pytest.raises(ValueError):
            grant(artifact)
    with grant(artifact) as ticket:
        assert ticket.freeze() is None
