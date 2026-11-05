"""Bounded timeout artifact recovery from an explicitly admitted workspace."""
import hashlib
import os
from pathlib import Path

import pytest

from tools.delegation_partial_file import ArtifactBoundaryError, reserve_result_file


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / 'workspace'
    path.mkdir()
    return path


@pytest.mark.parametrize('limit', [0, -1, True, 65537, 1.5])
def test_refuses_invalid_read_limit(workspace, limit):
    with pytest.raises(ArtifactBoundaryError):
        reserve_result_file(workspace, 'result.md', max_bytes=limit)


def test_refuses_relative_and_root_workspace():
    for workspace in (Path('.'), Path('/')):
        with pytest.raises(ArtifactBoundaryError):
            reserve_result_file(workspace, 'result.md')


def test_unsupported_descriptor_platform_fails_closed(workspace, monkeypatch):
    monkeypatch.setattr(os, 'supports_dir_fd', frozenset())
    with pytest.raises(ArtifactBoundaryError, match='unsupported'):
        reserve_result_file(workspace, 'result.md')
    assert not (workspace / 'result.md').exists()


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def test_recovery_requires_trusted_digest_even_for_valid_file(workspace):
    with reserve_result_file(workspace, 'result.md') as artifact:
        artifact.path.write_bytes(b'approved')
        missing = artifact.recover()
        assert missing.status == 'unverified'
        assert missing.text is None
        approved = artifact.recover(expected_sha256=digest(b'approved'))
        assert approved.status == 'available'
        assert approved.text == 'approved'


@pytest.mark.parametrize('bad_digest', ['', 'g' * 64, 'A' * 64, '0' * 63, 123, b'0' * 64])
def test_malformed_digest_never_releases_content(workspace, bad_digest):
    with reserve_result_file(workspace, 'result.md') as artifact:
        artifact.path.write_bytes(b'private fixture')
        result = artifact.recover(expected_sha256=bad_digest)
        assert result.status == 'unverified'
        assert result.text is None


def test_recovery_rejects_substitution_even_with_atomic_write(workspace):
    with reserve_result_file(workspace, 'result.md') as artifact:
        artifact.path.write_bytes(b'approved content')
        replacement = workspace / 'other'
        replacement.write_bytes(b'synthetic protected content')
        os.replace(replacement, artifact.path)
        result = artifact.recover(expected_sha256=digest(b'approved content'))
        assert result.status == 'unverified'
        assert result.text is None


def test_digest_checks_full_file_not_just_truncated_prefix(workspace):
    with reserve_result_file(workspace, 'result.md', max_bytes=3) as artifact:
        artifact.path.write_bytes(b'prefix-one')
        approved = digest(b'prefix-one')
        assert artifact.recover(expected_sha256=approved).text == 'pre'
        artifact.path.write_bytes(b'prefix-two')
        assert artifact.recover(expected_sha256=approved).status == 'unverified'


def test_recovery_refuses_files_over_absolute_hash_bound(workspace):
    data = b'x' * 65537
    with reserve_result_file(workspace, 'result.md') as artifact:
        artifact.path.write_bytes(data)
        result = artifact.recover(expected_sha256=digest(data))
        assert result.status == 'unverified'
        assert result.text is None


def test_recovery_verifies_file_at_absolute_hash_bound(workspace):
    data = b'x' * 65536
    with reserve_result_file(workspace, 'result.md', max_bytes=16) as artifact:
        artifact.path.write_bytes(data)
        result = artifact.recover(expected_sha256=digest(data))
        assert result.status == 'available'
        assert result.text == 'x' * 16
        assert result.truncated is True


def test_reserves_new_file_and_recovers_atomic_child_write(workspace):
    with reserve_result_file(workspace, 'result.md') as artifact:
        assert artifact.path == workspace / 'result.md'
        assert artifact.recover().status == 'empty'
        temporary = workspace / 'child-tmp'
        temporary.write_text('synthetic partial result')
        os.replace(temporary, artifact.path)
        result = artifact.recover(expected_sha256=digest(b'synthetic partial result'))
        assert result.status == 'available'
        assert result.text == 'synthetic partial result'
        assert result.truncated is False


def test_actual_local_write_file_atomically_replaces_reserved_result(workspace, monkeypatch):
    """Pin the intended standard-writer contract, not only os.replace."""
    import json
    from tools.file_tools import write_file_tool

    monkeypatch.setenv('TERMINAL_ENV', 'local')
    task_id = 'artifact-standard-writer-fixture'
    with reserve_result_file(workspace, 'result.md') as artifact:
        before = artifact.path.stat()
        result = json.loads(write_file_tool(str(artifact.path), 'synthetic partial', task_id=task_id))
        assert not result.get('error'), result
        after = artifact.path.stat()
        assert (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        observation = artifact.recover(expected_sha256=digest(b'synthetic partial'))
        assert observation.status == 'available'
        assert observation.text == 'synthetic partial'


def test_protected_file_replacement_is_rejected_when_digest_is_bound(workspace):
    """A path check alone is insufficient; an approved digest must match."""
    from agent.file_safety import get_read_block_error

    protected = workspace / '.env.local'
    protected.write_text('synthetic protected content')
    assert get_read_block_error(str(protected)) is not None
    with reserve_result_file(workspace, 'result.md') as artifact:
        os.replace(protected, artifact.path)
        result = artifact.recover(expected_sha256=digest(b'approved child output'))
        assert result.status == 'unverified'
        assert result.text is None


@pytest.mark.parametrize('name', ['', '.', '..', '../escape', '/absolute', 'a/b', 'a\\b', 'a\x00b', '\ud800'])
def test_refuses_non_basename(workspace, name):
    with pytest.raises(ArtifactBoundaryError):
        reserve_result_file(workspace, name)


def test_refuses_preexisting_file_without_modification(workspace):
    target = workspace / 'result.md'
    target.write_text('existing canary')
    with pytest.raises(ArtifactBoundaryError):
        reserve_result_file(workspace, 'result.md')
    assert target.read_text() == 'existing canary'


def test_reservation_obeys_write_policy(workspace, tmp_path, monkeypatch):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    monkeypatch.setenv('HERMES_WRITE_SAFE_ROOT', str(tmp_path / 'other-root'))
    with pytest.raises(ArtifactBoundaryError):
        reserve_result_file(workspace, 'result.md')
    monkeypatch.delenv('HERMES_WRITE_SAFE_ROOT')
    token = set_hermes_home_override(workspace)
    try:
        with pytest.raises(ArtifactBoundaryError):
            reserve_result_file(workspace, '.env')
    finally:
        reset_hermes_home_override(token)
    assert not (workspace / '.env').exists()
    assert not (workspace / 'result.md').exists()


def test_reservation_obeys_canonical_read_guard(workspace):
    from agent.file_safety import get_read_block_error, get_write_denied_error

    target = workspace / '.env.local'
    assert get_write_denied_error(str(target)) is None  # write-only control
    assert get_read_block_error(str(target)) is not None
    with pytest.raises(ArtifactBoundaryError):
        reserve_result_file(workspace, target.name)
    assert not target.exists()


def test_read_guard_revocation_prevents_read(workspace, monkeypatch):
    from agent import file_safety

    with reserve_result_file(workspace, 'result.md') as artifact:
        artifact.path.write_text('synthetic private content')
        monkeypatch.setattr(file_safety, 'get_read_block_error', lambda path: 'denied')
        calls = []
        original = os.read
        def counted(fd, size):
            calls.append(1)
            return original(fd, size)
        monkeypatch.setattr(os, 'read', counted)
        observation = artifact.recover()
        assert observation.status == 'policy_denied'
        assert observation.text is None
        assert calls == []


def test_unavailable_read_guard_fails_closed(workspace, monkeypatch):
    from agent import file_safety

    with reserve_result_file(workspace, 'result.md') as artifact:
        artifact.path.write_text('synthetic private content')
        def fail(path):
            raise RuntimeError('synthetic policy failure')
        monkeypatch.setattr(file_safety, 'get_read_block_error', fail)
        assert artifact.recover().status == 'policy_denied'
        with pytest.raises(ArtifactBoundaryError):
            reserve_result_file(workspace, 'second.md')


def test_reservation_refuses_cross_profile_skill_workspace(tmp_path, monkeypatch):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from agent.file_safety import get_cross_profile_warning

    root = tmp_path / 'ares'
    active = root / 'profiles' / 'active'
    other = root / 'profiles' / 'other' / 'skills'
    active.mkdir(parents=True)
    other.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(active))
    token = set_hermes_home_override(active)
    try:
        assert get_cross_profile_warning(str(other / 'result.md')) is not None
        with pytest.raises(ArtifactBoundaryError):
            reserve_result_file(other, 'result.md')
    finally:
        reset_hermes_home_override(token)
    assert not (other / 'result.md').exists()


def test_reservation_refuses_sandbox_mirror_workspace(tmp_path):
    from agent.file_safety import get_sandbox_mirror_warning

    mirror = tmp_path / 'sandboxes' / 'docker' / 'task' / 'home' / '.hermes' / 'skills'
    mirror.mkdir(parents=True)
    assert get_sandbox_mirror_warning(str(mirror / 'result.md')) is not None
    with pytest.raises(ArtifactBoundaryError):
        reserve_result_file(mirror, 'result.md')
    assert not (mirror / 'result.md').exists()


def test_bounded_utf8_and_binary_fail_closed(workspace):
    with reserve_result_file(workspace, 'result.md', max_bytes=16) as artifact:
        artifact.path.write_text('a' * 30)
        result = artifact.recover(expected_sha256=digest(b'a' * 30))
        assert result.status == 'available'
        assert result.text == 'a' * 16
        assert result.truncated is True
        artifact.path.write_bytes(b'bad\xfftext')
        assert artifact.recover(expected_sha256=digest(b'bad\xfftext')).status == 'invalid_text'


def test_truncated_partial_multibyte_character_does_not_claim_text(workspace):
    with reserve_result_file(workspace, 'result.md', max_bytes=1) as artifact:
        artifact.path.write_text('🙂x')
        result = artifact.recover(expected_sha256=digest('🙂x'.encode()))
        assert result.status == 'empty'
        assert result.text is None
        assert result.truncated is True


def test_policy_revocation_refuses_recovery(workspace, tmp_path, monkeypatch):
    with reserve_result_file(workspace, 'result.md') as artifact:
        artifact.path.write_text('synthetic result')
        monkeypatch.setenv('HERMES_WRITE_SAFE_ROOT', str(tmp_path / 'other-root'))
        assert artifact.recover().status == 'policy_denied'


@pytest.mark.parametrize('mutation', ['append', 'replace'])
def test_change_while_reading_refuses_partial_data(workspace, monkeypatch, mutation):
    with reserve_result_file(workspace, 'result.md') as artifact:
        artifact.path.write_text('original')
        original_read = os.read
        modified = []

        def race(fd, size):
            data = original_read(fd, size)
            if data and not modified:
                if mutation == 'append':
                    with artifact.path.open('a') as output:
                        output.write('-later')
                else:
                    replacement = workspace / 'replacement'
                    replacement.write_text('different')
                    os.replace(replacement, artifact.path)
                modified.append(True)
            return data

        monkeypatch.setattr(os, 'read', race)
        result = artifact.recover(expected_sha256=digest(b'original'))
        assert modified == [True]
        assert result.status == 'changed'
        assert result.text is None


def test_missing_file_is_not_a_fabricated_result(workspace):
    with reserve_result_file(workspace, 'result.md') as artifact:
        artifact.path.unlink()
        result = artifact.recover()
        assert result.status == 'missing'
        assert result.text is None


def test_symlink_and_hardlink_are_refused(workspace, tmp_path):
    outside = tmp_path / 'outside'
    outside.write_text('synthetic secret')
    with reserve_result_file(workspace, 'result.md') as artifact:
        artifact.path.unlink()
        artifact.path.symlink_to(outside)
        assert artifact.recover().status == 'unsafe'
        artifact.path.unlink()
        os.link(outside, artifact.path)
        assert artifact.recover().status == 'unsafe'
    assert outside.read_text() == 'synthetic secret'


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='FIFO unavailable')
def test_fifo_is_refused_without_blocking(workspace):
    with reserve_result_file(workspace, 'result.md') as artifact:
        artifact.path.unlink()
        os.mkfifo(artifact.path)
        assert artifact.recover().status == 'unsafe'


def test_workspace_replacement_is_refused(workspace, tmp_path):
    with reserve_result_file(workspace, 'result.md') as artifact:
        artifact.path.write_text('original')
        moved = tmp_path / 'moved'
        workspace.rename(moved)
        workspace.mkdir()
        (workspace / 'result.md').write_text('replacement')
        assert artifact.recover().status == 'workspace_changed'


def test_workspace_symlink_is_refused(workspace, tmp_path):
    alias = tmp_path / 'alias'
    alias.symlink_to(workspace, target_is_directory=True)
    with pytest.raises(ArtifactBoundaryError):
        reserve_result_file(alias, 'result.md')


@pytest.mark.skipif(os.name != 'posix', reason='descriptor-safe POSIX contract')
def test_real_child_partial_can_be_recovered_while_child_is_alive(workspace):
    import select
    import subprocess
    import sys

    with reserve_result_file(workspace, 'result.md') as artifact:
        child = subprocess.Popen(
            [sys.executable, '-c',
             "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('actual partial'); "
             "print('READY', flush=True); time.sleep(60)", str(artifact.path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            readable, _, _ = select.select([child.stdout], [], [], 5)
            assert readable, 'child did not write its result in time'
            assert child.stdout.readline().strip() == 'READY'
            assert child.poll() is None
            result = artifact.recover(expected_sha256=digest(b'actual partial'))
            assert result.status == 'available'
            assert result.text == 'actual partial'
        finally:
            child.terminate()
            child.communicate(timeout=5)


def test_closed_artifact_cannot_be_reused(workspace):
    artifact = reserve_result_file(workspace, 'result.md')
    artifact.close()
    artifact.close()
    with pytest.raises(ArtifactBoundaryError):
        artifact.recover()
