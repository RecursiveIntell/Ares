"""Behavioral witnesses: the file resolver is a cwd hint, not a grant.

These tests exercise the existing owners; they do not authorize artifact writes.
"""
from tools import file_tools, terminal_tool
from tools.delegation_live_log import LiveTranscriptWriter


def test_live_transcript_writer_refuses_existing_task_file(tmp_path):
    root = tmp_path / 'live'
    first = LiveTranscriptWriter('deleg_fixture', 0, 'first goal', root=root)
    assert first.path is not None
    first.event('assistant', 'first observation')
    original = first.path.read_text(encoding='utf-8')

    second = LiveTranscriptWriter('deleg_fixture', 0, 'second goal', root=root)
    assert second.path is None
    assert first.path.read_text(encoding='utf-8') == original


def test_live_transcript_writer_refuses_existing_task_symlink(tmp_path):
    root = tmp_path / 'live'
    run_dir = root / 'deleg_fixture'
    run_dir.mkdir(parents=True)
    other = tmp_path / 'outside-live-root'
    other.write_text('original fixture content', encoding='utf-8')
    (run_dir / 'task-0.log').symlink_to(other)

    writer = LiveTranscriptWriter('deleg_fixture', 0, 'fixture goal', root=root)
    assert writer.path is None
    assert other.read_text(encoding='utf-8') == 'original fixture content'


def test_live_transcript_writer_refuses_replaced_path_on_append(tmp_path):
    writer = LiveTranscriptWriter('deleg_fixture', 0, 'fixture goal', root=tmp_path / 'live')
    assert writer.path is not None
    other = tmp_path / 'outside-live-root'
    other.write_text('original fixture content', encoding='utf-8')
    writer.path.unlink()
    writer.path.symlink_to(other)

    writer.event('assistant', 'must not reach target')
    assert other.read_text(encoding='utf-8') == 'original fixture content'
    assert not writer._ok


def test_live_transcript_writer_refuses_regular_file_replacement(tmp_path):
    writer = LiveTranscriptWriter('deleg_fixture', 0, 'fixture goal', root=tmp_path / 'live')
    assert writer.path is not None
    writer.path.rename(writer.path.with_suffix('.saved'))
    writer.path.write_text('replacement content', encoding='utf-8')
    writer.event('assistant', 'must not reach replacement')
    assert writer.path.read_text(encoding='utf-8') == 'replacement content'
    assert not writer._ok


def test_live_transcript_writer_fails_closed_without_no_follow(tmp_path, monkeypatch):
    import os
    monkeypatch.delattr(os, 'O_NOFOLLOW', raising=False)
    writer = LiveTranscriptWriter('deleg_fixture', 0, 'fixture goal', root=tmp_path / 'live')
    assert writer.path is None
    assert not writer._ok
    assert not (tmp_path / 'live').exists()


def test_workspace_root_follows_session_cd_outside_registered_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv('TERMINAL_CWD', raising=False)
    task = 'artifact-boundary-session-cd'
    workspace = tmp_path / 'workspace'
    elsewhere = tmp_path / 'elsewhere'
    workspace.mkdir()
    elsewhere.mkdir()
    try:
        terminal_tool.register_task_env_overrides(task, {'cwd': str(workspace), 'cwd_source': 'session'})
        assert file_tools._authoritative_workspace_root(task) == str(workspace)
        # This is what the terminal's post-command cwd recording does after cd.
        terminal_tool.record_session_cwd(task, str(elsewhere))
        assert file_tools._authoritative_workspace_root(task) == str(elsewhere)
        assert terminal_tool.resolve_task_overrides(task)['cwd'] == str(workspace)
    finally:
        terminal_tool.clear_task_env_overrides(task)


def test_unregistered_session_can_resolve_ambient_terminal_cwd(tmp_path, monkeypatch):
    task = 'artifact-boundary-no-registration'
    ambient = tmp_path / 'another-session'
    ambient.mkdir()
    terminal_tool.clear_task_env_overrides(task)
    monkeypatch.setenv('TERMINAL_CWD', str(ambient))
    assert not terminal_tool.resolve_task_overrides(task)
    assert file_tools._authoritative_workspace_root(task) == str(ambient)


def test_worktree_local_predicate_does_not_prove_active_backend_is_local(monkeypatch):
    from hermes_cli import config
    from tools import subagent_worktree

    class DockerEnvironment:
        pass

    task = 'artifact-boundary-remote-backend'
    monkeypatch.setattr(config, 'load_config_readonly', lambda: {'terminal': {'backend': 'local'}})
    monkeypatch.setattr(terminal_tool, '_resolve_container_task_id', lambda task_id: task_id)
    monkeypatch.setitem(terminal_tool._active_environments, task, DockerEnvironment())

    # The worktree helper reads profile config, not the selected environment.
    assert subagent_worktree.local_backend_active() is True
    assert file_tools._terminal_env_type_for_task(task) == 'docker'


def test_file_backend_type_uses_stamped_plugin_identity_before_class_name(monkeypatch):
    from agent import terminal_env_registry
    from agent.terminal_env_provider import TerminalEnvironmentProvider

    class RemoteLocalEnvironment:
        pass

    class FixtureProvider(TerminalEnvironmentProvider):
        name = 'remote_fixture'
        is_container = True

        def is_available(self):
            return True

        def create_environment(self, **kwargs):
            return RemoteLocalEnvironment()

    task = 'artifact-boundary-misleading-class-name'
    monkeypatch.setitem(terminal_env_registry._providers, 'remote_fixture', FixtureProvider())
    env = terminal_tool._create_environment('remote_fixture', '', '/workspace', 30, task_id=task)
    monkeypatch.setattr(terminal_tool, '_resolve_container_task_id', lambda task_id: task_id)
    monkeypatch.setitem(terminal_tool._active_environments, task, env)

    # Exercise the actual plugin factory, registry and container-path resolver.
    assert file_tools._terminal_env_type_for_task(task) == 'remote_fixture'
    assert terminal_tool.get_active_env(task)._hermes_backend_name == 'remote_fixture'
    assert file_tools._uses_container_paths(task) is True


def test_worktree_local_predicate_defaults_to_local_when_config_read_fails(monkeypatch):
    from hermes_cli import config
    from tools import subagent_worktree

    def broken_config():
        raise OSError('synthetic unavailable configuration')

    monkeypatch.setattr(config, 'load_config_readonly', broken_config)
    assert subagent_worktree.local_backend_active() is True


def test_recorded_container_cwd_is_returned_without_host_translation(tmp_path, monkeypatch):
    monkeypatch.delenv('TERMINAL_CWD', raising=False)
    task = 'artifact-boundary-container-cwd'
    container_path = '/workspace/worker-only'
    try:
        terminal_tool.record_session_cwd(task, container_path)
        assert file_tools._authoritative_workspace_root(task) == container_path
    finally:
        terminal_tool.clear_task_env_overrides(task)
