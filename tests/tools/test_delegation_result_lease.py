"""Process-death and hostile-leaf witnesses for result-directory leases."""
import os
from pathlib import Path
import select
import signal
import subprocess
import sys

import pytest

from tools import delegation_result_lease as leases


@pytest.mark.skipif(os.name != 'posix', reason='POSIX advisory locks required')
def test_lease_is_busy_until_owner_process_exits(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    code = '''import os, sys
from tools.delegation_result_lease import create_lease
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
lease = create_lease(fd)
print("ready", flush=True)
sys.stdin.read(1)
os._exit(0)
'''
    child = subprocess.Popen([sys.executable, '-c', code, str(tmp_path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        ready, _, _ = select.select([child.stdout], [], [], 10)
        assert ready, 'lease child did not become ready'
        assert child.stdout.readline().strip() == 'ready'
        assert leases.try_acquire_lease(fd) is None
        child.kill()
        assert child.wait(timeout=10) == -signal.SIGKILL
        lease = leases.try_acquire_lease(fd)
        assert lease is not None
        os.close(lease)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        child.stdin.close()
        child.stdout.close()
        child.stderr.close()
        os.close(fd)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX advisory locks required')
def test_actual_run_result_lease_releases_after_controller_sigkill(tmp_path):
    """Use a real temporary profile and RunResult, not an invented lease leaf."""
    home = tmp_path / 'profile'
    home.mkdir()
    (home / 'config.yaml').write_text(
        'delegation:\n  provider: ""\n  model: ""\nterminal:\n  backend: local\n'
    )
    code = '''import os, sys, time
from pathlib import Path
from types import SimpleNamespace
from hermes_constants import set_hermes_home_override
from hermes_state import SessionDB
from agent.orchestration_runtime import admit_worker
from tools.delegation_run_result import RunResult
home = Path(sys.argv[1])
set_hermes_home_override(home)
db = SessionDB(home / 'state.db')
db.create_session('lease-parent', 'test', model='fixture')
parent = SimpleNamespace(session_id='lease-parent', _session_db=db)
child = SimpleNamespace(_subagent_id='lease-child', session_id='lease-child-session',
                        _orchestration_admission=admit_worker(parent))
result = RunResult(child, time.monotonic() + 60, 'lease-child')
print(result._directory, flush=True)
sys.stdin.read(1)
'''
    env = {'HOME': str(tmp_path), 'HERMES_HOME': str(home), 'ARES_HOME': str(home),
           'HERMES_TEST_ISOLATION': str(home), 'TERMINAL_ENV': 'local',
           'PATH': os.environ.get('PATH', ''), 'LANG': 'C.UTF-8'}
    child = subprocess.Popen([sys.executable, '-c', code, str(home)], env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    directory = None
    directory_fd = None
    writer = None
    try:
        ready, _, _ = select.select([child.stdout], [], [], 20)
        assert ready, 'result owner did not become ready'
        line = child.stdout.readline().strip()
        if not line:
            raise AssertionError(f'owner exited before readiness: {child.stderr.read()}')
        directory = Path(line)
        assert directory.parent == Path('/tmp') and directory.name.startswith('ares-delegate-result-')
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        assert leases.try_acquire_lease(directory_fd) is None
        # A separately surviving writer does not inherit/hold the lease. This
        # deliberately models an external writer, not a real terminal process.
        writer_code = '''import sys
from pathlib import Path
print('ready', flush=True)
sys.stdin.read(1)
Path(sys.argv[1]).write_text('late synthetic write')
'''
        writer = subprocess.Popen([sys.executable, '-c', writer_code, str(directory / 'result.txt')],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=env)
        ready, _, _ = select.select([writer.stdout], [], [], 10)
        assert ready and writer.stdout.readline().strip() == 'ready'
        child.kill()
        assert child.wait(timeout=10) == -signal.SIGKILL
        assert (directory / 'result.txt').is_file()
        acquired = leases.try_acquire_lease(directory_fd)
        assert acquired is not None
        try:
            assert writer.poll() is None
            writer.stdin.write('x')
            writer.stdin.flush()
            assert writer.wait(timeout=10) == 0
            assert (directory / 'result.txt').read_text() == 'late synthetic write'
        finally:
            os.close(acquired)
    finally:
        if writer is not None:
            if writer.poll() is None:
                writer.kill()
                writer.wait(timeout=10)
            writer.stdin.close()
            writer.stdout.close()
            writer.stderr.close()
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        if directory_fd is not None:
            os.close(directory_fd)
        if directory is not None and directory.parent == Path('/tmp') and directory.name.startswith('ares-delegate-result-'):
            (directory / 'result.txt').unlink(missing_ok=True)
            (directory / '.lease').unlink(missing_ok=True)
            directory.rmdir()
        child.stdin.close()
        child.stdout.close()
        child.stderr.close()


def test_existing_symlink_or_hardlink_never_authorizes_lease(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    target = tmp_path / 'outside'
    target.write_text('keep')
    leaf = tmp_path / '.lease'
    try:
        leaf.symlink_to(target)
        with pytest.raises(leases.LeaseError):
            leases.try_acquire_lease(fd)
        assert target.read_text() == 'keep'
        leaf.unlink()
        os.link(target, leaf)
        with pytest.raises(leases.LeaseError):
            leases.try_acquire_lease(fd)
        assert target.read_text() == 'keep'
    finally:
        os.close(fd)


def test_replaced_lease_after_lock_is_rejected(tmp_path, monkeypatch):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_fd = leases.create_lease(fd)
    os.close(original_fd)
    original_lock = leases._lock
    def replace_after_lock(lease_fd):
        assert original_lock(lease_fd)
        (tmp_path / '.lease').unlink()
        replacement = tmp_path / '.lease'
        replacement.write_text('replacement')
        replacement.chmod(0o600)
        return True
    monkeypatch.setattr(leases, '_lock', replace_after_lock)
    try:
        with pytest.raises(leases.LeaseError, match='identity'):
            leases.try_acquire_lease(fd)
        assert (tmp_path / '.lease').read_text() == 'replacement'
    finally:
        os.close(fd)


def test_create_is_exclusive_and_does_not_replace_existing(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    leaf = tmp_path / '.lease'
    leaf.write_text('existing')
    try:
        with pytest.raises(leases.LeaseError):
            leases.create_lease(fd)
        assert leaf.read_text() == 'existing'
    finally:
        os.close(fd)
