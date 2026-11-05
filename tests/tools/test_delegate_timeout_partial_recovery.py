"""Timeout salvage on a real temporary orchestration admission owner."""
import threading
from types import SimpleNamespace

import pytest

from agent.orchestration_runtime import admit_worker
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_state import SessionDB
from tests.tools.test_delegate_subagent_timeout_diagnostic import _StubChild
from tools import delegate_tool


@pytest.fixture
def admitted_child(tmp_path, monkeypatch):
    home = tmp_path / 'profile'
    home.mkdir()
    (home / 'config.yaml').write_text('delegation:\n  provider: ""\n  model: ""\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    token = set_hermes_home_override(home)
    db = SessionDB(home / 'state.db')
    db.create_session('timeout-parent', 'cli', model='root')
    parent = SimpleNamespace(session_id='timeout-parent', _session_db=db,
                             _current_task_id=None, _touch_activity=lambda *_: None)
    child = _StubChild(api_call_count=1, hang_seconds=10)
    child.valid_tool_names.add('write_file')
    child._orchestration_admission = admit_worker(parent)
    child.steers = []
    child.steer = lambda text: child.steers.append(text) or True
    try:
        yield child, parent
    finally:
        child.interrupt()
        db.close()
        reset_hermes_home_override(token)


@pytest.mark.parametrize('deferred', [False, True])
def test_run_owned_result_file_reaches_timeout_summary(admitted_child, monkeypatch, deferred):
    import json
    import re
    from tools.file_tools import write_file_tool

    child, parent = admitted_child
    child.session_id = 'fixture-child-session'
    child._delegate_result_file_recovery = True
    if deferred:
        from agent import tool_executor
        child.valid_tool_names = {'tool_search', 'tool_call'}
        monkeypatch.setattr(tool_executor, '_tool_search_scoped_names', lambda _child: frozenset({'write_file'}))
    monkeypatch.setenv('TERMINAL_ENV', 'local')
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 2.0)
    wrote = threading.Event()
    release = threading.Event()
    paths = []

    def run(user_message, task_id=None, stream_callback=None):
        match = re.search(r'RESULT_FILE: ([^\n]+)', user_message)
        assert match is not None, 'no run-owned result file supplied'
        paths.append(match.group(1))
        result = json.loads(write_file_tool(match.group(1), 'SYNTHETIC_RESULT_MARKER\n',
                                            task_id=task_id, session_id=child.session_id))
        assert result.get('verified') is True and not result.get('error'), result
        wrote.set()
        release.wait(8)
        return {'final_response': 'late', 'completed': True, 'api_calls': 1}

    child.run_conversation = run
    try:
        result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
        assert wrote.is_set(), 'worker did not write the reserved result file'
        assert result['status'] == 'timeout'
        assert result['partial_file_recovery'] == 'available'
        assert 'SYNTHETIC_RESULT_MARKER' in (result.get('summary') or '')
        assert result['summary_is_partial'] is True
        from pathlib import Path
        assert Path(paths[0]).exists(), 'result removed before the worker exited'
    finally:
        release.set()
    import time
    deadline = time.monotonic() + 5
    while Path(paths[0]).exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(paths[0]).exists(), 'result retained after the worker exited'


def test_timed_out_worker_oversize_write_is_blocked_until_worker_exit(admitted_child, monkeypatch):
    import json
    import re
    import time
    from pathlib import Path
    from unittest.mock import Mock
    from tools.file_tools import _get_file_ops, write_file_tool

    child, parent = admitted_child
    child.session_id = 'fixture-child-session'
    child._delegate_result_file_recovery = True
    monkeypatch.setenv('TERMINAL_ENV', 'local')
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 0.5)
    ops = _get_file_ops(child._subagent_id)
    writer = Mock(wraps=ops.write_file)
    monkeypatch.setattr(ops, 'write_file', writer)
    started, attempt, written, exit_worker = (threading.Event() for _ in range(4))
    paths, outcomes = [], []

    def run(user_message, task_id=None, **kwargs):
        match = re.search(r'RESULT_FILE: ([^\n]+)', user_message)
        assert match is not None
        paths.append(Path(match.group(1)))
        started.set()
        assert attempt.wait(5)
        outcomes.append(json.loads(write_file_tool(str(paths[0]), 'x' * 65537,
                                                   task_id=task_id, session_id=child.session_id)))
        written.set()
        assert exit_worker.wait(5)
        return {'final_response': 'late', 'completed': True, 'api_calls': 1}

    child.run_conversation = run
    try:
        result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
        assert started.is_set() and result['status'] == 'timeout'
        assert paths[0].exists()
        attempt.set()
        assert written.wait(5)
        assert outcomes[0].get('error'), outcomes
        writer.assert_not_called()
        assert paths[0].exists() and paths[0].stat().st_size == 0
    finally:
        attempt.set()
        exit_worker.set()
    deadline = time.monotonic() + 5
    while paths[0].exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not paths[0].exists()


def test_delegate_task_opt_in_recovers_redacted_file(admitted_child, monkeypatch):
    import json
    import re
    from tools.file_tools import write_file_tool

    child, parent = admitted_child
    child.session_id = 'fixture-child-session'
    monkeypatch.setenv('TERMINAL_ENV', 'local')
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 3.0)
    monkeypatch.setattr(delegate_tool, '_build_child_preserving_parent_tools', lambda **kwargs: child)
    monkeypatch.setattr(delegate_tool, '_resolve_delegation_credentials', lambda *args, **kwargs: {
        'model': 'fixture', 'provider': None, 'base_url': None,
        'api_key': None, 'api_mode': None, 'command': None, 'args': None,
    })
    release = threading.Event()
    written = threading.Event()
    canary = 'sk-' + 'A' * 40

    def run(user_message, task_id=None, stream_callback=None):
        match = re.search(r'RESULT_FILE: ([^\n]+)', user_message)
        assert match is not None
        result = json.loads(write_file_tool(match.group(1), f'result marker\nOPENAI_API_KEY={canary}\n',
                                            task_id=task_id, session_id=child.session_id))
        assert result.get('verified') is True and not result.get('error'), result
        written.set()
        release.wait(8)
        return {'final_response': 'late', 'completed': True, 'api_calls': 1}

    child.run_conversation = run
    try:
        result = json.loads(delegate_tool.delegate_task(
            tasks=[{'goal': 'bounded task', 'recover_partial_file': True}],
            background=False, parent_agent=parent,
        ))
        assert written.is_set(), result
        worker = result['results'][0]
        assert worker['status'] == 'timeout'
        assert 'result marker' in worker['summary']
        assert canary not in json.dumps(result)
        assert worker['summary_is_partial'] is True
    finally:
        release.set()


@pytest.mark.parametrize('first', ['controller', 'worker'])
def test_result_lease_held_until_both_lifecycle_events(admitted_child, monkeypatch, first):
    import os
    import time
    from tools import delegation_result_lease as leases
    from tools import delegation_run_result as owner

    monkeypatch.setenv('TERMINAL_ENV', 'local')
    child, _ = admitted_child
    child.session_id = 'fixture-child-session'
    result = owner.RunResult(child, time.monotonic() + 30, child._subagent_id)
    directory_fd = os.open(result._directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert leases.try_acquire_lease(directory_fd) is None
        (result.controller_done if first == 'controller' else result.worker_done)()
        assert leases.try_acquire_lease(directory_fd) is None
        (result.worker_done if first == 'controller' else result.controller_done)()
        assert not result._directory.exists()
    finally:
        result.controller_done()
        result.worker_done()
        os.close(directory_fd)


def test_replaced_result_lease_is_not_removed_and_slot_stays_consumed(admitted_child, monkeypatch):
    import os
    import time
    from tools import delegation_run_result as owner

    monkeypatch.setenv('TERMINAL_ENV', 'local')
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(owner, '_result_slots', slots)
    child, _ = admitted_child
    child.session_id = 'fixture-child-session'
    result = owner.RunResult(child, time.monotonic() + 30, child._subagent_id)
    lease = result._directory / '.lease'
    old = result._directory / 'old-lease'
    os.rename(lease, old)
    lease.write_text('replacement')
    try:
        result.controller_done()
        result.worker_done()
        assert lease.read_text() == 'replacement'
        assert not slots.acquire(blocking=False)
    finally:
        if result._lease_fd is not None:
            os.close(result._lease_fd)
            result._lease_fd = None
        result._artifact.close()
        lease.unlink(missing_ok=True)
        old.unlink(missing_ok=True)
        (result._directory / 'result.txt').unlink(missing_ok=True)
        if result._directory.exists():
            result._directory.rmdir()


@pytest.mark.parametrize('first', ['controller', 'worker'])
def test_result_capacity_is_held_until_both_lifecycle_events(admitted_child, monkeypatch, first):
    import time
    from tools import delegation_run_result as owner

    monkeypatch.setenv('TERMINAL_ENV', 'local')
    monkeypatch.setattr(owner, '_result_slots', threading.BoundedSemaphore(1))
    child, _ = admitted_child
    child.session_id = 'fixture-child-session'
    def create():
        return owner.RunResult(child, time.monotonic() + 30, child._subagent_id)

    result = create()
    try:
        if first == 'controller':
            result.controller_done()
        else:
            result.worker_done()
        with pytest.raises(ValueError, match='capacity'):
            create()
    finally:
        result.controller_done()
        result.worker_done()
    replacement = create()
    replacement.controller_done()
    replacement.worker_done()


def test_result_capacity_enforced_across_concurrent_admissions(admitted_child, monkeypatch):
    import concurrent.futures
    import time
    from tools import delegation_run_result as owner

    monkeypatch.setenv('TERMINAL_ENV', 'local')
    monkeypatch.setattr(owner, '_result_slots', threading.BoundedSemaphore(2))
    child, _ = admitted_child
    child.session_id = 'fixture-child-session'
    barrier = threading.Barrier(6)
    release = threading.Event()
    acquired = []
    errors = []
    lock = threading.Lock()

    def attempt():
        barrier.wait(timeout=10)
        try:
            result = owner.RunResult(child, time.monotonic() + 30, child._subagent_id)
        except ValueError as exc:
            with lock:
                errors.append(str(exc))
            return
        with lock:
            acquired.append(result)
        try:
            assert release.wait(10)
        finally:
            result.controller_done()
            result.worker_done()

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(attempt) for _ in range(6)]
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with lock:
                    if len(acquired) + len(errors) == 6:
                        break
                time.sleep(0.01)
            with lock:
                assert len(acquired) == 2
                assert len(errors) == 4
                assert all('capacity' in error for error in errors)
        finally:
            release.set()
        for future in futures:
            future.result(timeout=10)
    replacement = owner.RunResult(child, time.monotonic() + 30, child._subagent_id)
    replacement.controller_done()
    replacement.worker_done()


def test_exhausted_result_capacity_does_not_create_file_backend(admitted_child, monkeypatch):
    import time
    from tools import delegation_run_result as owner, file_tools

    monkeypatch.setenv('TERMINAL_ENV', 'local')
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    monkeypatch.setattr(owner, '_result_slots', slots)
    calls = []
    def forbidden_backend(*args, **kwargs):
        calls.append(True)
        raise AssertionError('backend must not be created')
    monkeypatch.setattr(file_tools, '_get_file_ops', forbidden_backend)
    child, _ = admitted_child
    child.session_id = 'fixture-child-session'
    try:
        with pytest.raises(ValueError, match='capacity'):
            owner.RunResult(child, time.monotonic() + 30, child._subagent_id)
        assert not calls
    finally:
        slots.release()


def test_result_capacity_released_on_backend_failure(admitted_child, monkeypatch):
    import time
    from tools import delegation_run_result as owner, file_tools

    monkeypatch.setenv('TERMINAL_ENV', 'local')
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(owner, '_result_slots', slots)
    def fail_backend(*args, **kwargs):
        raise OSError('synthetic backend failure')
    monkeypatch.setattr(file_tools, '_get_file_ops', fail_backend)
    child, _ = admitted_child
    child.session_id = 'fixture-child-session'
    with pytest.raises(OSError, match='synthetic backend failure'):
        owner.RunResult(child, time.monotonic() + 30, child._subagent_id)
    assert slots.acquire(blocking=False)
    slots.release()


def test_result_capacity_released_on_creation_error(admitted_child, monkeypatch):
    import time
    from tools import delegation_run_result as owner

    monkeypatch.setenv('TERMINAL_ENV', 'local')
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(owner, '_result_slots', slots)
    child, _ = admitted_child
    child.session_id = 'fixture-child-session'
    def fail(*args, **kwargs):
        raise OSError('synthetic creation failure')
    monkeypatch.setattr(owner.tempfile, 'mkdtemp', fail)
    with pytest.raises(OSError, match='synthetic creation'):
        owner.RunResult(child, time.monotonic() + 30, child._subagent_id)
    assert slots.acquire(blocking=False)
    slots.release()


def test_slot_released_if_grant_registration_fails(admitted_child, monkeypatch):
    import time
    from tools import delegation_run_result as owner

    monkeypatch.setenv('TERMINAL_ENV', 'local')
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(owner, '_result_slots', slots)
    def fail(*args, **kwargs):
        raise ValueError('synthetic grant failure')
    monkeypatch.setattr(owner, 'register_result_write', fail)
    child, _ = admitted_child
    child.session_id = 'fixture-child-session'
    with pytest.raises(ValueError, match='synthetic grant failure'):
        owner.RunResult(child, time.monotonic() + 30, child._subagent_id)
    assert slots.acquire(blocking=False)
    slots.release()


def test_interrupted_grant_registration_removes_directory_and_releases_slot(admitted_child, monkeypatch):
    import time
    from tools import delegation_run_result as owner

    monkeypatch.setenv('TERMINAL_ENV', 'local')
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(owner, '_result_slots', slots)
    created = []
    real_mkdtemp = owner.tempfile.mkdtemp
    def remember_directory(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(owner.Path(path))
        return path
    monkeypatch.setattr(owner.tempfile, 'mkdtemp', remember_directory)
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt('synthetic interruption')
    monkeypatch.setattr(owner, 'register_result_write', interrupt)
    child, _ = admitted_child
    child.session_id = 'fixture-child-session'
    try:
        with pytest.raises(KeyboardInterrupt, match='synthetic interruption'):
            owner.RunResult(child, time.monotonic() + 30, child._subagent_id)
        assert len(created) == 1 and not created[0].exists()
        assert slots.acquire(blocking=False)
        slots.release()
    finally:
        # Quarantine-only fixture cleanup, including on the pre-fix failure.
        for directory in created:
            if directory.exists():
                (directory / 'result.txt').unlink(missing_ok=True)
                directory.rmdir()


def test_interrupted_after_grant_registration_does_not_leak_registry(admitted_child, monkeypatch):
    import time
    from tools import delegation_result_write as registry
    from tools import delegation_run_result as owner

    monkeypatch.setenv('TERMINAL_ENV', 'local')
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(owner, '_result_slots', slots)
    original = owner.register_result_write
    recorded = []
    def register_then_interrupt(*args, **kwargs):
        ticket = original(*args, **kwargs)
        recorded.append(ticket)
        raise KeyboardInterrupt('synthetic post-registration interruption')
    monkeypatch.setattr(owner, 'register_result_write', register_then_interrupt)
    child, _ = admitted_child
    child.session_id = 'fixture-child-session'
    try:
        with pytest.raises(KeyboardInterrupt, match='post-registration'):
            owner.RunResult(child, time.monotonic() + 30, child._subagent_id)
        assert len(recorded) == 1
        ticket = recorded[0]
        assert registry._active.get(ticket._path) is not ticket
        assert ticket._closed
        assert slots.acquire(blocking=False)
        slots.release()
    finally:
        for ticket in recorded:
            ticket.close()


def test_cleanup_uses_fresh_directory_cursor(admitted_child, monkeypatch):
    import os
    import time
    from tools import delegation_run_result as owner

    monkeypatch.setenv('TERMINAL_ENV', 'local')
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(owner, '_result_slots', slots)
    child, _ = admitted_child
    child.session_id = 'fixture-child-session'
    result = owner.RunResult(child, time.monotonic() + 30, child._subagent_id)
    orphan = result._directory / '.hermes-tmp.ABC123'
    orphan.write_text('fixture')
    original_fd = result._artifact._dir_fd
    real_listdir = os.listdir
    def stale_cursor(path):
        return [] if path == original_fd else real_listdir(path)
    try:
        with monkeypatch.context() as context:
            context.setattr(owner.os, 'listdir', stale_cursor)
            result.controller_done()
            result.worker_done()
        assert not result._directory.exists()
        assert slots.acquire(blocking=False)
        slots.release()
    finally:
        if result._directory.exists():
            orphan.unlink(missing_ok=True)
            (result._directory / 'result.txt').unlink(missing_ok=True)
            (result._directory / '.lease').unlink(missing_ok=True)
            result._directory.rmdir()


def test_owned_atomic_temp_removed_only_after_both_finish(admitted_child, monkeypatch):
    import time
    from tools import delegation_run_result as owner

    monkeypatch.setenv('TERMINAL_ENV', 'local')
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(owner, '_result_slots', slots)
    child, _ = admitted_child
    child.session_id = 'fixture-child-session'
    result = owner.RunResult(child, time.monotonic() + 30, child._subagent_id)
    orphan = result._directory / '.hermes-tmp.ABC123'
    orphan.write_text('fixture')
    try:
        result.controller_done()
        assert orphan.exists()
        assert not slots.acquire(blocking=False)
        result.worker_done()
        assert not result._directory.exists()
        assert slots.acquire(blocking=False)
        slots.release()
    finally:
        orphan.unlink(missing_ok=True)
        (result._directory / 'result.txt').unlink(missing_ok=True)
        if result._directory.exists():
            result._directory.rmdir()


def test_atomic_temp_symlink_is_not_followed_or_removed(admitted_child, monkeypatch, tmp_path):
    import time
    from tools import delegation_run_result as owner

    monkeypatch.setenv('TERMINAL_ENV', 'local')
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(owner, '_result_slots', slots)
    child, _ = admitted_child
    child.session_id = 'fixture-child-session'
    result = owner.RunResult(child, time.monotonic() + 30, child._subagent_id)
    target = tmp_path / 'outside'
    target.write_text('keep')
    link = result._directory / '.hermes-tmp.ABC123'
    link.symlink_to(target)
    try:
        result.controller_done()
        result.worker_done()
        assert link.is_symlink()
        assert target.read_text() == 'keep'
        assert not slots.acquire(blocking=False)
    finally:
        link.unlink(missing_ok=True)
        (result._directory / '.lease').unlink(missing_ok=True)
        (result._directory / 'result.txt').unlink(missing_ok=True)
        if result._directory.exists():
            result._directory.rmdir()


@pytest.mark.parametrize('failure', ['unlink', 'listdir'])
def test_cleanup_io_error_releases_lease_but_preserves_residue(admitted_child, monkeypatch, failure):
    import os
    import time
    from tools import delegation_result_lease as leases
    from tools import delegation_run_result as owner

    monkeypatch.setenv('TERMINAL_ENV', 'local')
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(owner, '_result_slots', slots)
    child, _ = admitted_child
    child.session_id = 'fixture-child-session'
    result = owner.RunResult(child, time.monotonic() + 30, child._subagent_id)
    directory_fd = os.open(result._directory, os.O_RDONLY | os.O_DIRECTORY)
    real_unlink = owner.os.unlink
    real_listdir = owner.os.listdir
    def fail_owned_result(path, *args, **kwargs):
        if path == 'result.txt' and kwargs.get('dir_fd') == result._artifact._dir_fd:
            raise OSError('synthetic cleanup failure')
        return real_unlink(path, *args, **kwargs)
    def fail_owned_listdir(path):
        if (isinstance(path, int)
                and (os.fstat(path).st_dev, os.fstat(path).st_ino)
                == (os.fstat(directory_fd).st_dev, os.fstat(directory_fd).st_ino)):
            raise OSError('synthetic directory listing failure')
        return real_listdir(path)
    try:
        with monkeypatch.context() as context:
            if failure == 'unlink':
                context.setattr(owner.os, 'unlink', fail_owned_result)
            else:
                context.setattr(owner.os, 'listdir', fail_owned_listdir)
            result.controller_done()
            result.worker_done()
        if failure == 'unlink':
            assert (result._directory / 'result.txt').exists()
        assert (result._directory / '.lease').exists()
        assert not slots.acquire(blocking=False)
        acquired = leases.try_acquire_lease(directory_fd)
        assert acquired is not None
        os.close(acquired)
    finally:
        if result._lease_fd is not None:
            os.close(result._lease_fd)
            result._lease_fd = None
        result._artifact.close()
        os.close(directory_fd)
        (result._directory / 'result.txt').unlink(missing_ok=True)
        (result._directory / '.lease').unlink(missing_ok=True)
        if result._directory.exists():
            result._directory.rmdir()


def test_residue_retains_unlocked_lease_marker(admitted_child, monkeypatch):
    import os
    import time
    from tools import delegation_result_lease as leases
    from tools import delegation_run_result as owner

    monkeypatch.setenv('TERMINAL_ENV', 'local')
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(owner, '_result_slots', slots)
    child, _ = admitted_child
    child.session_id = 'fixture-child-session'
    result = owner.RunResult(child, time.monotonic() + 30, child._subagent_id)
    residue = result._directory / 'unrecognized'
    residue.write_text('keep')
    directory_fd = os.open(result._directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        result.controller_done()
        assert leases.try_acquire_lease(directory_fd) is None
        result.worker_done()
        assert residue.read_text() == 'keep'
        assert not slots.acquire(blocking=False)
        acquired = leases.try_acquire_lease(directory_fd)
        assert acquired is not None
        os.close(acquired)
    finally:
        result.controller_done()
        result.worker_done()
        os.close(directory_fd)
        residue.unlink(missing_ok=True)
        (result._directory / '.lease').unlink(missing_ok=True)
        (result._directory / 'result.txt').unlink(missing_ok=True)
        if result._directory.exists():
            result._directory.rmdir()


def test_residue_keeps_capacity_consumed_after_worker_exit(admitted_child, monkeypatch):
    import time
    from tools import delegation_run_result as owner

    monkeypatch.setenv('TERMINAL_ENV', 'local')
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(owner, '_result_slots', slots)
    child, _ = admitted_child
    child.session_id = 'fixture-child-session'
    result = owner.RunResult(child, time.monotonic() + 30, child._subagent_id)
    residue = result._directory / 'synthetic-residue'
    residue.write_text('fixture')
    try:
        result.controller_done()
        result.worker_done()
        assert residue.exists()
        assert not slots.acquire(blocking=False)
    finally:
        residue.unlink(missing_ok=True)
        (result._directory / '.lease').unlink(missing_ok=True)
        result._directory.rmdir()


def test_readonly_result_parent_preserves_timeout_and_streamed_partial(admitted_child, monkeypatch):
    import errno
    from tools import delegation_run_result as owner

    monkeypatch.setenv('TERMINAL_ENV', 'local')
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 0.3)
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(owner, '_result_slots', slots)
    def readonly(*args, **kwargs):
        raise OSError(errno.EROFS, 'synthetic read-only result parent')
    monkeypatch.setattr(owner.tempfile, 'mkdtemp', readonly)
    child, parent = admitted_child
    child.session_id = 'fixture-child-session'
    child._delegate_result_file_recovery = True
    child.valid_tool_names = {'write_file'}
    release = threading.Event()
    messages = []
    def run(user_message, stream_callback, **kwargs):
        messages.append(user_message)
        stream_callback('partial finding before timeout\n')
        release.wait(5)
        return {'final_response': 'late', 'completed': True, 'api_calls': 1}
    child.run_conversation = run
    try:
        result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
        assert result['status'] == 'timeout'
        assert result['partial_file_recovery'] == 'unavailable'
        assert 'partial finding before timeout' in result['summary']
        assert messages and 'RESULT_FILE:' not in messages[0]
        assert slots.acquire(blocking=False)
        slots.release()
    finally:
        release.set()


def test_capacity_exhaustion_reports_unavailable_without_file_instruction(admitted_child, monkeypatch):
    from tools import delegation_run_result as owner

    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    monkeypatch.setattr(owner, '_result_slots', slots)
    monkeypatch.setenv('TERMINAL_ENV', 'local')
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 0.2)
    child, parent = admitted_child
    child.session_id = 'fixture-child-session'
    child._delegate_result_file_recovery = True
    release = threading.Event()
    messages = []
    def run(user_message, **kwargs):
        messages.append(user_message)
        release.wait(5)
        return {'final_response': 'late', 'completed': True, 'api_calls': 1}
    child.run_conversation = run
    try:
        result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
        assert result['status'] == 'timeout'
        assert result['partial_file_recovery'] == 'unavailable'
        assert messages and 'RESULT_FILE:' not in messages[0]
    finally:
        release.set()
        slots.release()


def test_run_result_refuses_remote_before_environment_creation(admitted_child, monkeypatch):
    import time
    from tools import file_tools, terminal_tool
    from tools.delegation_run_result import RunResult

    child, _parent = admitted_child
    child.session_id = 'fixture-child-session'
    monkeypatch.setattr(terminal_tool, '_get_env_config', lambda: {'env_type': 'ssh'})
    monkeypatch.setattr(file_tools, '_get_file_ops', lambda *_: pytest.fail('remote must not be created'))
    with pytest.raises(ValueError, match='local POSIX backend'):
        RunResult(child, time.monotonic() + 10, child._subagent_id)


@pytest.mark.parametrize('tool_names, scoped_names', [
    ({'terminal'}, frozenset()),
    ({'tool_search', 'tool_call'}, frozenset({'read_file'})),
])
def test_opt_in_without_write_file_reports_unavailable(admitted_child, monkeypatch, tool_names, scoped_names):
    from agent import tool_executor
    monkeypatch.setattr(tool_executor, '_tool_search_scoped_names', lambda _child: scoped_names)
    child, parent = admitted_child
    child.session_id = 'fixture-child-session'
    child._delegate_result_file_recovery = True
    child.valid_tool_names = tool_names
    monkeypatch.setenv('TERMINAL_ENV', 'local')
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 0.3)
    release = threading.Event()
    seen = []
    def run(user_message, **kwargs):
        seen.append(user_message)
        release.wait(5)
        return {'final_response': 'late', 'completed': True, 'api_calls': 1}
    child.run_conversation = run
    try:
        result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
        assert result['status'] == 'timeout'
        assert result['partial_file_recovery'] == 'unavailable'
        assert seen and 'RESULT_FILE:' not in seen[0]
    finally:
        release.set()


def test_remote_timeout_reports_file_recovery_unavailable(admitted_child, monkeypatch):
    from tools import terminal_tool

    child, parent = admitted_child
    child.session_id = 'fixture-child-session'
    child._delegate_result_file_recovery = True
    monkeypatch.setattr(terminal_tool, '_get_env_config', lambda: {'env_type': 'ssh'})
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 0.2)
    release = threading.Event()
    def run(**kwargs):
        release.wait(5)
        return {'final_response': 'late', 'completed': True, 'api_calls': 1}
    child.run_conversation = run
    try:
        result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
        assert result['status'] == 'timeout'
        assert result['partial_file_recovery'] == 'unavailable'
    finally:
        release.set()


def test_run_result_respects_safe_write_root(admitted_child, monkeypatch, tmp_path):
    import time
    from tools.delegation_partial_file import ArtifactBoundaryError
    from tools.delegation_run_result import RunResult

    child, _parent = admitted_child
    child.session_id = 'fixture-child-session'
    monkeypatch.setenv('TERMINAL_ENV', 'local')
    monkeypatch.setenv('HERMES_WRITE_SAFE_ROOT', str(tmp_path / 'only-allowed'))
    with pytest.raises(ArtifactBoundaryError, match='denied by file policy'):
        RunResult(child, time.monotonic() + 10, child._subagent_id)


def test_background_opt_in_file_reaches_receipt(admitted_child, monkeypatch):
    import json
    import re
    import sqlite3
    import time
    from gateway import session_context
    from tools import async_delegation
    from tools.file_tools import write_file_tool
    from tools.process_registry import format_process_notification, process_registry

    child, parent = admitted_child
    child.session_id = 'fixture-child-session'
    monkeypatch.setenv('TERMINAL_ENV', 'local')
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 3.0)
    monkeypatch.setattr(session_context, 'async_delivery_supported', lambda: True)
    monkeypatch.setattr(delegate_tool, '_build_child_preserving_parent_tools', lambda **kwargs: child)
    monkeypatch.setattr(delegate_tool, '_resolve_delegation_credentials', lambda *args, **kwargs: {
        'model': 'fixture', 'provider': None, 'base_url': None,
        'api_key': None, 'api_mode': None, 'command': None, 'args': None,
    })
    release = threading.Event()
    canary = 'sk-' + 'B' * 40

    def run(user_message, task_id=None, stream_callback=None):
        match = re.search(r'RESULT_FILE: ([^\n]+)', user_message)
        assert match is not None
        result = json.loads(write_file_tool(match.group(1), f'background file marker\nOPENAI_API_KEY={canary}\n',
                                            task_id=task_id, session_id=child.session_id))
        assert result.get('verified') is True and not result.get('error'), result
        release.wait(8)
        return {'final_response': 'late', 'completed': True, 'api_calls': 1}

    child.run_conversation = run
    async_delegation._reset_for_tests()
    try:
        dispatched = json.loads(delegate_tool.delegate_task(
            tasks=[{'goal': 'bounded task', 'recover_partial_file': True}],
            background=True, parent_agent=parent,
        ))
        assert dispatched['status'] == 'dispatched', dispatched
        identifier = dispatched['delegation_id']
        deadline = time.monotonic() + 12
        event = None
        while time.monotonic() < deadline:
            candidate = process_registry.completion_queue.get(timeout=max(0.1, deadline - time.monotonic()))
            if candidate.get('delegation_id') == identifier:
                event = candidate
                break
        assert event is not None
        assert event['results'][0]['status'] == 'timeout'
        assert 'background file marker' in event['results'][0]['summary']
        assert canary not in json.dumps(event)
        notification = format_process_notification(event)
        assert canary not in notification
        with sqlite3.connect(parent._session_db.db_path) as db:
            state, event_json, result_json = db.execute(
                'SELECT state, event_json, result_json FROM async_delegations WHERE delegation_id=?',
                (identifier,),
            ).fetchone()
        assert state == 'error'
        for saved in (json.loads(event_json), json.loads(result_json)):
            assert saved['results'][0]['status'] == 'timeout'
            assert 'background file marker' in saved['results'][0]['summary']
            assert canary not in json.dumps(saved)
    finally:
        release.set()
        child.interrupt()
        async_delegation._reset_for_tests()


@pytest.mark.parametrize('prefix', ['safe\n' * 1636, 's' * 8180])
def test_truncated_file_does_not_leak_partial_credential(admitted_child, monkeypatch, prefix):
    import json
    import re
    from tools.file_tools import write_file_tool

    child, parent = admitted_child
    child.session_id = 'fixture-child-session'
    child._delegate_result_file_recovery = True
    monkeypatch.setenv('TERMINAL_ENV', 'local')
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 3.0)
    release = threading.Event()
    wrote = threading.Event()
    canary = 'sk-' + 'Z' * 40
    cut = (prefix + canary)[:8192][len(prefix):]
    assert cut.startswith('sk-') and len(cut) < 13

    def run(user_message, task_id=None, stream_callback=None):
        match = re.search(r'RESULT_FILE: ([^\n]+)', user_message)
        assert match is not None
        result = json.loads(write_file_tool(match.group(1), prefix + canary + '\n',
                                            task_id=task_id, session_id=child.session_id))
        assert result.get('verified') is True and not result.get('error'), result
        wrote.set()
        release.wait(8)
        return {'final_response': 'late', 'completed': True, 'api_calls': 1}

    child.run_conversation = run
    try:
        result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
        assert wrote.is_set(), result
        assert result['status'] == 'timeout'
        if '\n' in prefix:
            assert result['partial_file_recovery'] == 'available_truncated'
            assert 'safe' in (result.get('summary') or '')
        else:
            assert result['partial_file_recovery'] == 'withheld'
            assert result.get('summary') is None
        assert cut not in (result.get('summary') or '')
    finally:
        release.set()


def test_result_file_withheld_when_redactor_raises(admitted_child, monkeypatch):
    import json
    import re
    from agent import redact
    from tools.file_tools import write_file_tool

    child, parent = admitted_child
    child.session_id = 'fixture-child-session'
    child._delegate_result_file_recovery = True
    monkeypatch.setenv('TERMINAL_ENV', 'local')
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 3.0)
    release, wrote = threading.Event(), threading.Event()
    canary = 'synthetic-private-file-value'

    def redaction_failure(*_args, **_kwargs):
        raise RuntimeError('synthetic redactor failure')

    def run(user_message, task_id=None, stream_callback=None):
        match = re.search(r'RESULT_FILE: ([^\n]+)', user_message)
        assert match is not None
        result = json.loads(write_file_tool(match.group(1), canary + '\n',
                                            task_id=task_id, session_id=child.session_id))
        assert result.get('verified') is True and not result.get('error'), result
        wrote.set()
        release.wait(8)
        return {'final_response': 'late', 'completed': True, 'api_calls': 1}

    child.run_conversation = run
    monkeypatch.setattr(redact, 'redact_sensitive_text', redaction_failure)
    try:
        result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
        assert wrote.is_set(), result
        assert result['status'] == 'timeout'
        assert result['partial_file_recovery'] == 'withheld'
        assert result.get('summary') is None
        assert canary not in json.dumps(result)
    finally:
        release.set()


def test_timing_from_abandoned_child_is_not_written_after_timeout(admitted_child, monkeypatch, tmp_path):
    from tools.delegation_live_log import LiveTranscriptWriter, wrap_progress_callback

    child, parent = admitted_child
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 0.25)
    writer = LiveTranscriptWriter('deleg_fixture', 0, 'fixture', root=tmp_path / 'logs')
    child.tool_progress_callback = wrap_progress_callback(None, writer)
    release = threading.Event()
    emitted = threading.Event()

    def run(**kwargs):
        try:
            assert release.wait(5), 'test did not release synthetic child'
            child.tool_progress_callback('subagent.api_timing', duration_seconds=123.0)
        finally:
            emitted.set()
        return {'final_response': 'late result', 'completed': True, 'api_calls': 1}

    child.run_conversation = run
    try:
        result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
        assert result['status'] == 'timeout'
        release.set()
        assert emitted.wait(5), 'synthetic child did not send the late timing'
        text = writer.path.read_text(encoding='utf-8')
        assert 'status=timeout' in text
        assert 'api-window 123.000s' not in text
    finally:
        release.set()


@pytest.mark.parametrize('late_outcome', ['success', 'transport_timeout', 'error'])
def test_child_finishing_after_deadline_is_not_reported_completed(admitted_child, monkeypatch, late_outcome):
    from concurrent.futures import TimeoutError as FutureTimeoutError
    from tools import daemon_pool

    child, parent = admitted_child
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 0.25)
    real_executor = daemon_pool.DaemonThreadPoolExecutor
    release = threading.Event()

    class RacingFuture:
        def __init__(self, future):
            self.future = future
            self.calls = 0

        def result(self, timeout=None):
            self.calls += 1
            try:
                return self.future.result(timeout=timeout)
            except FutureTimeoutError:
                if self.calls == 2:
                    # Force completion after the wait's deadline but before
                    # the controller executes future.done().
                    release.set()
                    try:
                        self.future.result(timeout=5)
                    except Exception:
                        pass  # the controller must classify the late outcome
                raise

        def done(self):
            return self.future.done()

    class RacingExecutor:
        def __init__(self, *args, **kwargs):
            self.executor = real_executor(*args, **kwargs)

        def submit(self, *args, **kwargs):
            return RacingFuture(self.executor.submit(*args, **kwargs))

        def shutdown(self, *args, **kwargs):
            return self.executor.shutdown(*args, **kwargs)

    monkeypatch.setattr(daemon_pool, 'DaemonThreadPoolExecutor', RacingExecutor)

    def run(**kwargs):
        assert release.wait(5)
        if late_outcome == 'transport_timeout':
            raise TimeoutError('late transport timeout')
        if late_outcome == 'error':
            raise ValueError('late failure')
        return {'final_response': 'too late', 'completed': True,
                'api_calls': 1, 'messages': [], 'iterations': 1}

    child.run_conversation = run
    try:
        result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
        assert result['status'] == 'timeout'
        assert result.get('summary') != 'too late'
    finally:
        release.set()


def test_child_finished_in_budget_remains_completed_if_observed_late(admitted_child, monkeypatch):
    import time
    from tools import daemon_pool

    child, parent = admitted_child
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 0.3)
    real_executor = daemon_pool.DaemonThreadPoolExecutor

    class DelayedFuture:
        def __init__(self, future):
            self.future = future

        def result(self, timeout=None):
            value = self.future.result(timeout=timeout)
            time.sleep(0.4)  # observer delay, after the child actually finished
            return value

        def done(self):
            return self.future.done()

    class DelayedExecutor:
        def __init__(self, *args, **kwargs):
            self.executor = real_executor(*args, **kwargs)

        def submit(self, *args, **kwargs):
            return DelayedFuture(self.executor.submit(*args, **kwargs))

        def shutdown(self, *args, **kwargs):
            return self.executor.shutdown(*args, **kwargs)

    monkeypatch.setattr(daemon_pool, 'DaemonThreadPoolExecutor', DelayedExecutor)
    child.run_conversation = lambda **kwargs: {
        'final_response': 'finished on time', 'completed': True,
        'api_calls': 1, 'messages': [], 'iterations': 1,
    }
    result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
    assert result['status'] == 'completed'
    assert result['summary'] == 'finished on time'


def test_timeout_salvages_accepted_nonstream_commentary(admitted_child, monkeypatch):
    child, parent = admitted_child
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 0.25)
    previous = lambda text: None
    child._delegation_accepted_text_callback = previous
    captured_callbacks = []

    def run(**kwargs):
        # The real conversation loop invokes this only after the assistant
        # tool-call message has been accepted and persisted.
        captured_callbacks.append(child._delegation_accepted_text_callback)
        captured_callbacks[0]('accepted finding API_KEY=syntheticSecret987654321')
        child._hang.wait(5)
        return {'final_response': '', 'completed': False, 'api_calls': 1}

    child.run_conversation = run
    result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
    assert result['status'] == 'timeout'
    assert result['summary_is_partial'] is True
    assert result['summary'] == 'accepted finding API_KEY=***'
    assert child._delegation_accepted_text_callback is previous
    captured_callbacks[0]('late accepted text')
    assert result['summary'] == 'accepted finding API_KEY=***'


def test_timeout_retains_streamed_text_and_sends_warning(admitted_child, monkeypatch):
    child, parent = admitted_child
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 0.5)
    def run(user_message, task_id=None, stream_callback=None):
        stream_callback('Partial ')
        stream_callback('useful findings')
        child._hang.wait(5)
        return {'final_response': '', 'completed': False, 'api_calls': 1}
    child.run_conversation = run
    result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
    assert result['status'] == 'timeout'
    assert result['summary'] == 'Partial useful findings'
    assert result['summary_is_partial'] is True
    assert len(child.steers) == 1
    assert 'write' in child.steers[0].lower()


@pytest.mark.parametrize('configured_timeout', [None, 1.0])
def test_child_raised_timeout_error_is_not_a_deadline_timeout(admitted_child, monkeypatch, configured_timeout):
    child, parent = admitted_child
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: configured_timeout)

    def run(**kwargs):
        raise TimeoutError('synthetic transport timeout')

    child.run_conversation = run
    result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
    assert result['status'] == 'error'
    assert result['exit_reason'] == 'error'
    assert result['timeout_seconds'] is None
    assert 'synthetic transport timeout' in result['error']


def test_child_transport_timeout_after_warning_is_not_deadline_expiry(admitted_child, monkeypatch):
    child, parent = admitted_child
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 0.8)
    warned = threading.Event()
    child.steer = lambda text: warned.set() or True

    def run(**kwargs):
        if not warned.wait(2):
            raise AssertionError('controller did not send warning')
        raise TimeoutError('synthetic timeout after warning')

    child.run_conversation = run
    result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
    assert warned.is_set()
    assert result['status'] == 'error'
    assert result['exit_reason'] == 'error'
    assert 'synthetic timeout after warning' in result['error']


def test_schema_retry_transport_timeout_does_not_claim_deadline_expired(admitted_child, monkeypatch):
    child, parent = admitted_child
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 1.0)
    child._delegate_output_schema = {'type': 'object', 'required': ['city']}
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {'final_response': 'invalid json', 'completed': True,
                    'api_calls': 1, 'messages': [], 'iterations': 1}
        raise TimeoutError('synthetic retry transport timeout')

    child.run_conversation = run
    result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
    assert len(calls) == 2
    assert result['status'] == 'completed'
    assert result['schema_valid'] is False
    assert result['summary'] == 'invalid json'


def test_timeout_summary_redacts_split_synthetic_secret_even_when_disabled(admitted_child, monkeypatch):
    from agent import redact
    child, parent = admitted_child
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 0.25)
    monkeypatch.setattr(redact, '_REDACT_ENABLED', False)
    secret = 'syntheticSecret987654321'

    def run(user_message, task_id=None, stream_callback=None):
        stream_callback('Finding API_')
        stream_callback('KEY=' + secret)
        child._hang.wait(5)
        return {'final_response': '', 'completed': False, 'api_calls': 1}

    child.run_conversation = run
    result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
    assert result['status'] == 'timeout'
    assert result['summary_is_partial'] is True
    assert 'Finding' in result['summary']
    assert secret not in result['summary']


@pytest.mark.parametrize('source', ['stream', 'schema_fallback', 'accepted'])
def test_timeout_partial_withholds_truncated_first_line_before_redaction(admitted_child, monkeypatch, source):
    child, parent = admitted_child
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 0.25)
    secret = 'syntheticSecret987654321'
    # The marker falls just outside the retained 8192-character tail, while
    # the value remains inside it. The next complete line must remain useful.
    unsafe = 'API_KEY=' + secret + '\n' + 'safe finding ' + 'x' * 8154
    assert len(unsafe) > 8192 and 'API_KEY=' not in unsafe[-8192:]
    if source == 'schema_fallback':
        child._delegate_output_schema = {'type': 'object', 'required': ['city']}
    calls = []

    def run(user_message, task_id=None, stream_callback=None):
        calls.append(1)
        if source == 'schema_fallback' and len(calls) == 1:
            return {'final_response': unsafe, 'completed': True, 'api_calls': 1, 'messages': []}
        if source == 'stream':
            stream_callback(unsafe[:5])
            stream_callback(unsafe[5:])
        elif source == 'accepted':
            child._delegation_accepted_text_callback(unsafe)
        child._hang.wait(5)
        return {'final_response': '', 'completed': False, 'api_calls': 1}

    child.run_conversation = run
    result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
    assert result['status'] == 'timeout'
    assert secret not in (result['summary'] or '')
    assert 'safe finding' in (result['summary'] or '')


def test_timeout_summary_withheld_when_redactor_unavailable(admitted_child, monkeypatch):
    from agent import redact
    child, parent = admitted_child
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 0.25)

    def unavailable(*args, **kwargs):
        raise RuntimeError('synthetic redactor failure')

    monkeypatch.setattr(redact, 'redact_sensitive_text', unavailable)

    def run(user_message, task_id=None, stream_callback=None):
        stream_callback('sensitive synthetic partial')
        child._hang.wait(5)
        return {'final_response': '', 'completed': False, 'api_calls': 1}

    child.run_conversation = run
    result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
    assert result['status'] == 'timeout'
    assert result['summary'] is None
    assert result['summary_is_partial'] is False


def test_schema_retry_timeout_redacts_fallback_first_response(admitted_child, monkeypatch):
    from agent import redact
    child, parent = admitted_child
    monkeypatch.setattr(redact, '_REDACT_ENABLED', False)
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 0.25)
    child._delegate_output_schema = {'type': 'object', 'required': ['city']}
    calls = []

    def run(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            return {'final_response': 'API_KEY=syntheticSecret987654321',
                    'completed': True, 'api_calls': 1, 'messages': []}
        child._hang.wait(5)
        return {'final_response': '', 'completed': False, 'api_calls': 1}

    child.run_conversation = run
    result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
    assert len(calls) == 2
    assert result['status'] == 'timeout'
    assert result['schema_valid'] is False
    assert result['summary'] == 'API_KEY=***'


def test_timeout_with_no_output_does_not_fabricate_a_summary(admitted_child, monkeypatch):
    child, parent = admitted_child
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 0.25)
    result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
    assert result['status'] == 'timeout'
    assert result['summary'] is None
    assert result['summary_is_partial'] is False


@pytest.mark.parametrize('text, expected', [
    ('A' * 8192, 'A' * 8192),
    ('A' * 10000 + 'LAST', None),
    ('A' * 10000 + '\nLAST', 'LAST'),
])
def test_timeout_partial_is_bounded(admitted_child, monkeypatch, text, expected):
    child, parent = admitted_child
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 0.25)
    def run(user_message, task_id=None, stream_callback=None):
        stream_callback(text)
        child._hang.wait(5)
        return {'final_response': '', 'completed': False, 'api_calls': 1}
    child.run_conversation = run
    result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
    assert result['status'] == 'timeout'
    assert result['summary'] == expected
    assert result['summary_is_partial'] is (expected is not None)


def test_retry_timeout_emits_one_warning_and_retains_invalid_status(admitted_child, monkeypatch):
    child, parent = admitted_child
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 0.5)
    child._delegate_output_schema = {'type': 'object', 'required': ['city']}
    from tools.delegation_output_schema import validate_output
    assert validate_output('invalid json', child._delegate_output_schema)[0] is False
    calls = []

    def run(user_message, task_id=None, stream_callback=None):
        calls.append(user_message)
        if len(calls) == 1:
            return {'final_response': 'invalid json', 'completed': True,
                    'api_calls': 1, 'messages': []}
        stream_callback('retry partial')
        child._hang.wait(5)
        return {'final_response': '{"city":"late"}', 'completed': True,
                'api_calls': 1, 'messages': []}

    child.run_conversation = run
    result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
    assert len(calls) == 2
    assert result['status'] == 'timeout'
    assert result['summary'] == 'retry partial'
    assert result['summary_is_partial'] is True
    assert result['schema_valid'] is False
    assert result['schema_retries'] == 1
    assert len(child.steers) == 1


def test_timeout_drops_late_stream_progress(admitted_child, monkeypatch):
    child, parent = admitted_child
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 0.25)
    callbacks = []
    events = []
    child.tool_progress_callback = lambda event, **kw: events.append((event, kw))

    def run(user_message, task_id=None, stream_callback=None):
        callbacks.append(stream_callback)
        stream_callback('visible before timeout')
        child._hang.wait(5)
        return {'final_response': '', 'completed': False, 'api_calls': 1}

    child.run_conversation = run
    result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
    assert result['status'] == 'timeout'
    assert result['summary'] == 'visible before timeout'
    assert any(e == 'subagent.text' and d.get('preview') == 'visible before timeout' for e, d in events)
    callbacks[0]('LATE_AFTER_TIMEOUT')
    assert not any(d.get('preview') == 'LATE_AFTER_TIMEOUT' for _, d in events)


def test_finished_child_gets_no_deadline_warning(admitted_child, monkeypatch):
    child, parent = admitted_child
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 1.0)
    child.run_conversation = lambda **kwargs: {
        'final_response': 'finished', 'completed': True, 'api_calls': 1,
        'messages': [], 'iterations': 1,
    }
    result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
    assert result['status'] == 'completed'
    assert result['summary'] == 'finished'
    assert child.steers == []


@pytest.mark.parametrize(('owned_timeout', 'expected'), [(31, 31.0), (0, None)])
def test_child_timeout_comes_from_admitted_owner_not_ambient_profile(
        tmp_path, monkeypatch, owned_timeout, expected):
    owner, ambient = tmp_path / 'owner', tmp_path / 'ambient'
    owner.mkdir()
    ambient.mkdir()
    (owner / 'config.yaml').write_text(
        f'delegation:\n  provider: ""\n  model: ""\n  child_timeout_seconds: {owned_timeout}\n'
    )
    (ambient / 'config.yaml').write_text('delegation:\n  child_timeout_seconds: 60\n')
    monkeypatch.setenv('HERMES_HOME', str(ambient))
    monkeypatch.delenv('DELEGATION_CHILD_TIMEOUT_SECONDS', raising=False)
    token = set_hermes_home_override(ambient)
    db = SessionDB(owner / 'state.db')
    db.create_session('owner-session', 'cli', model='root')
    parent = SimpleNamespace(session_id='owner-session', _session_db=db,
                             _current_task_id=None, _touch_activity=lambda *_: None)
    child = _StubChild(api_call_count=1, hang_seconds=10)
    child._orchestration_admission = admit_worker(parent)
    child.run_conversation = lambda **kwargs: {
        'final_response': 'finished', 'completed': True, 'api_calls': 1,
        'messages': [], 'iterations': 1,
    }
    original = delegate_tool._get_child_timeout
    assert original() == 60.0  # positive control: ambient profile differs
    seen = []

    def capture(*args, **kwargs):
        value = original(*args, **kwargs)
        seen.append(value)
        return value

    monkeypatch.setattr(delegate_tool, '_get_child_timeout', capture)
    try:
        result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
        assert result['status'] == 'completed'
        assert seen == [expected]
    finally:
        child.interrupt()
        db.close()
        reset_hermes_home_override(token)


def test_child_timeout_config_change_after_admission_blocks_dispatch(admitted_child):
    from pathlib import Path

    child, parent = admitted_child
    (Path(parent._session_db.db_path).parent / 'config.yaml').write_text(
        'delegation:\n  child_timeout_seconds: 31\n'
    )
    calls = []
    child.run_conversation = lambda **kwargs: calls.append(kwargs)
    result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
    assert result['status'] == 'blocked'
    assert result['code'] == 'orchestration_config_changed'
    assert calls == []


@pytest.mark.parametrize('kind', ['iteration_limit', 'invalid_schema'])
def test_health_uses_real_child_completion_metadata(admitted_child, kind):
    import json
    from hermes_cli.delegation_health import summarize

    child, parent = admitted_child
    if kind == 'invalid_schema':
        child._delegate_output_schema = {'type': 'object', 'required': ['city']}
    child.run_conversation = lambda **kwargs: {
        'final_response': '{}' if kind == 'invalid_schema' else 'partial finding',
        'completed': kind != 'iteration_limit', 'api_calls': 1,
        'messages': [], 'iterations': 1,
    }
    result = delegate_tool._run_single_child(0, 'bounded task', child, parent)
    assert result['status'] == 'completed'  # the real producer's wire status
    if kind == 'iteration_limit':
        assert result['truncated'] is True
    else:
        assert result['schema_valid'] is False
    row = ('completed', json.dumps({'results': [result]}),
           json.dumps({'is_batch': True, 'goals': ['bounded task']}))
    health = summarize([row])
    assert health['completed_status_workers'] == 1
    assert health['completed_workers'] == 0
    assert health['completion_rate'] == 0
    assert health['warning_below_80_percent'] is True


def test_background_deadline_race_remains_timeout_in_receipt_and_notification(admitted_child, monkeypatch):
    import json
    import sqlite3
    import time
    from concurrent.futures import TimeoutError as FutureTimeoutError
    from gateway import session_context
    from pathlib import Path
    from tools import async_delegation, daemon_pool
    from tools.process_registry import format_process_notification, process_registry

    # Ensure the producer under test is from the same tree as this witness.
    assert Path(delegate_tool.__file__).resolve() == (
        Path(__file__).resolve().parents[2] / 'tools' / 'delegate_tool.py'
    )
    child, parent = admitted_child
    release = threading.Event()
    real_executor = daemon_pool.DaemonThreadPoolExecutor
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 0.25)
    monkeypatch.setattr(session_context, 'async_delivery_supported', lambda: True)
    monkeypatch.setattr(delegate_tool, '_build_child_preserving_parent_tools', lambda **kwargs: child)
    monkeypatch.setattr(delegate_tool, '_resolve_delegation_credentials', lambda *args, **kwargs: {
        'model': 'fixture', 'provider': None, 'base_url': None,
        'api_key': None, 'api_mode': None, 'command': None, 'args': None,
    })

    class RacingFuture:
        def __init__(self, future):
            self.future, self.calls = future, 0

        def result(self, timeout=None):
            self.calls += 1
            try:
                return self.future.result(timeout=timeout)
            except FutureTimeoutError:
                if self.calls == 2:
                    release.set()
                    self.future.result(timeout=5)
                raise

        def done(self):
            return self.future.done()

    class RacingExecutor:
        def __init__(self, *args, **kwargs):
            self.executor = real_executor(*args, **kwargs)

        def submit(self, *args, **kwargs):
            return RacingFuture(self.executor.submit(*args, **kwargs))

        def shutdown(self, *args, **kwargs):
            return self.executor.shutdown(*args, **kwargs)

    monkeypatch.setattr(daemon_pool, 'DaemonThreadPoolExecutor', RacingExecutor)

    def run(user_message, task_id=None, stream_callback=None):
        stream_callback('useful partial before deadline')
        assert release.wait(5)
        return {'final_response': 'late completion must not escape', 'completed': True,
                'api_calls': 1, 'messages': [], 'iterations': 1}

    child.run_conversation = run
    async_delegation._reset_for_tests()
    try:
        dispatched = json.loads(delegate_tool.delegate_task(
            goal='bounded task', background=True, parent_agent=parent,
        ))
        assert dispatched['status'] == 'dispatched', dispatched
        identifier = dispatched['delegation_id']
        deadline = time.monotonic() + 10
        event = None
        while time.monotonic() < deadline:
            candidate = process_registry.completion_queue.get(timeout=max(0.1, deadline - time.monotonic()))
            if candidate.get('delegation_id') == identifier:
                event = candidate
                break
        assert event is not None
        assert event['status'] == 'error'
        assert event['results'][0]['status'] == 'timeout'
        assert event['results'][0]['summary'] == 'useful partial before deadline'
        notification = format_process_notification(event)
        assert 'status=timeout' in notification
        assert 'late completion must not escape' not in notification
        with sqlite3.connect(parent._session_db.db_path) as db:
            state, event_json, result_json = db.execute(
                'SELECT state, event_json, result_json FROM async_delegations WHERE delegation_id=?',
                (identifier,),
            ).fetchone()
        assert state == 'error'
        for saved in (json.loads(event_json), json.loads(result_json)):
            assert saved['results'][0]['status'] == 'timeout'
            assert saved['results'][0]['summary'] == 'useful partial before deadline'
            assert 'late completion must not escape' not in json.dumps(saved)
    finally:
        release.set()
        child.interrupt()
        async_delegation._reset_for_tests()


@pytest.mark.parametrize(('partial', 'expected'), [
    ('synthetic partial finding', 'synthetic partial finding'),
    ('synthetic partial finding API_KEY=syntheticSecret987654321',
     'synthetic partial finding API_KEY=***'),
])
def test_background_timeout_partial_survives_receipt_and_notification(
        admitted_child, monkeypatch, partial, expected):
    """Exercise the actual async batch, durable receipt, and event formatter."""
    import json
    import sqlite3

    from agent import redact
    from tools import async_delegation
    from tools.process_registry import format_process_notification, process_registry

    child, parent = admitted_child
    monkeypatch.setattr(redact, '_REDACT_ENABLED', False)
    monkeypatch.setattr(delegate_tool, '_get_child_timeout', lambda config: 0.5)
    async_delegation._reset_for_tests()

    def run(user_message, task_id=None, stream_callback=None):
        stream_callback(partial)
        child._hang.wait(5)
        return {'final_response': '', 'completed': False, 'api_calls': 1}

    child.run_conversation = run

    def runner():
        entry = delegate_tool._run_single_child(0, 'bounded task', child, parent)
        return {'results': [entry], 'total_duration_seconds': entry['duration_seconds']}

    try:
        dispatch = async_delegation.dispatch_async_delegation_batch(
            goals=['bounded task'], context=None, toolsets=None, role='leaf',
            model='fixture-model', session_key='', runner=runner,
            parent_session_id=parent.session_id,
        )
        assert dispatch['status'] == 'dispatched'
        event = process_registry.completion_queue.get(timeout=10)
        assert event['delegation_id'] == dispatch['delegation_id']
        assert event['status'] == 'error'
        result = event['results'][0]
        assert result['status'] == 'timeout'
        assert result['summary'] == expected
        assert result['summary_is_partial'] is True
        notification = format_process_notification(event)
        assert 'status=timeout' in notification
        assert 'Partial output:' in notification
        assert expected in notification
        assert 'syntheticSecret987654321' not in notification

        with sqlite3.connect(parent._session_db.db_path) as db:
            state, event_json, result_json = db.execute(
                'SELECT state, event_json, result_json FROM async_delegations WHERE delegation_id=?',
                (dispatch['delegation_id'],),
            ).fetchone()
        assert state == 'error'
        assert 'syntheticSecret987654321' not in event_json
        assert 'syntheticSecret987654321' not in result_json
        for saved in (json.loads(event_json), json.loads(result_json)):
            assert saved['results'][0]['status'] == 'timeout'
            assert saved['results'][0]['summary_is_partial'] is True
            assert saved['results'][0]['summary'] == expected
    finally:
        child.interrupt()
        async_delegation._reset_for_tests()
