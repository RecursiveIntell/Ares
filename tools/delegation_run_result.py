"""Run-owned, local-only result-file capability for delegated timeouts.

This is not isolation from other processes running as the same OS user. The
controller owns the private directory and must keep it alive until both its
result is frozen and the worker has exited. Remote backends are not supported.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import tempfile
import threading

from agent.orchestration_runtime import assert_child_dispatch
from tools.delegation_partial_file import _check_artifact_policy, _open_workspace, reserve_result_file
from tools.delegation_result_write import discard_interrupted_registration, register_result_write
from tools.delegation_result_lease import create_lease

# Bounds live reservations in this process, not across processes or crashes.
_MAX_LIVE_RESULTS = 32
_result_slots = threading.BoundedSemaphore(_MAX_LIVE_RESULTS)


class RunResult:
    def __init__(self, child, deadline, task_id):
        from tools.environments.local import LocalEnvironment
        from tools.file_operations import ShellFileOperations
        from tools.file_tools import _get_file_ops
        from tools.terminal_tool import _get_env_config

        admission = assert_child_dispatch(child)
        if task_id != getattr(child, '_subagent_id', None):
            raise ValueError('result task identity mismatch')
        if os.name != 'posix' or _get_env_config().get('env_type') != 'local':
            raise ValueError('result recovery requires a local POSIX backend')
        self._slots = _result_slots
        if not self._slots.acquire(blocking=False):
            raise ValueError('result recovery capacity exhausted')
        try:
            ops = _get_file_ops(task_id)
            if (type(ops) is not ShellFileOperations or type(ops.env) is not LocalEnvironment
                    or hasattr(ops.env, '_hermes_backend_name')):
                raise ValueError('result recovery requires the builtin local backend')
            # The parent is fixed, not an environment-controlled cwd or TMPDIR.
            parent = Path('/tmp')
            _check_artifact_policy(parent / 'ares-delegate-result-probe' / 'result.txt')
            parent_fd = _open_workspace(parent)
            os.close(parent_fd)
            self._directory = Path(tempfile.mkdtemp(prefix='ares-delegate-result-', dir=parent))
        except BaseException:
            self._slots.release()
            raise
        self._artifact = None
        self._grant = None
        self._lease_fd = None
        self._lock = threading.Lock()
        self._controller_done = False
        self._worker_done = False
        try:
            # Recheck profile/session admission after directory creation.
            if assert_child_dispatch(child) is not admission:
                raise ValueError('admission changed')
            self._artifact = reserve_result_file(self._directory, 'result.txt')
            self._lease_fd = create_lease(self._artifact._dir_fd)
            self._grant = register_result_write(self._artifact, child=child, deadline=deadline)
        except BaseException:
            if self._artifact is not None:
                discard_interrupted_registration(self._artifact, child)
                if self._lease_fd is not None:
                    try:
                        self._remove_owned_lease()
                    except OSError:
                        pass
                self._artifact.close()
            try:
                (self._directory / 'result.txt').unlink(missing_ok=True)
                self._directory.rmdir()
            except OSError:
                # Keep capacity consumed if owned residue could not be removed.
                pass
            else:
                self._slots.release()
            raise

    @property
    def path(self):
        return self._artifact.path

    def freeze(self):
        digest = self._grant.freeze()
        return self._artifact.recover(expected_sha256=digest)

    def controller_done(self):
        with self._lock:
            self._controller_done = True
            self._cleanup_if_done()

    def worker_done(self, _future=None):
        with self._lock:
            self._worker_done = True
            self._cleanup_if_done()

    def _remove_owned_lease(self):
        """Unlink only the leaf still matching our open lease descriptor."""
        fd = self._lease_fd
        if fd is None:
            return
        try:
            opened = os.fstat(fd)
            visible = os.stat('.lease', dir_fd=self._artifact._dir_fd, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino):
                raise OSError('lease identity changed')
            os.unlink('.lease', dir_fd=self._artifact._dir_fd)
        finally:
            os.close(fd)
            self._lease_fd = None

    def _quarantine(self):
        """Release process handles, preserve residue and keep capacity consumed."""
        if self._lease_fd is not None:
            os.close(self._lease_fd)
            self._lease_fd = None
        self._artifact.close()
        self._grant = None

    def _directory_entries(self):
        # A fresh open has an independent directory cursor; duplicated file
        # descriptors can share a stale offset on some filesystems.
        fd = os.open('.', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                     dir_fd=self._artifact._dir_fd)
        try:
            return os.listdir(fd)
        finally:
            os.close(fd)

    def _cleanup_if_done(self):
        if not (self._controller_done and self._worker_done):
            return
        if self._grant is None or self._lease_fd is None:
            return
        self._grant.close()
        # Keep deletion bounded to the owned leaf; never recursively delete.
        try:
            os.unlink('result.txt', dir_fd=self._artifact._dir_fd)
        except FileNotFoundError:
            pass
        except OSError:
            self._quarantine()
            return
        # This directory belongs to this run and the worker has finished. Only
        # remove regular, single-link atomic-writer temporaries; unknown entries
        # and links remain quarantined and keep capacity consumed. No recursion.
        try:
            for name in self._directory_entries():
                if not re.fullmatch(r'\.hermes-tmp\.(?:[A-Za-z0-9]{6}|[0-9]+(?:\.[A-Za-z0-9]{6})?)', name):
                    continue
                info = os.stat(name, dir_fd=self._artifact._dir_fd, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1:
                    os.unlink(name, dir_fd=self._artifact._dir_fd)
        except OSError:
            self._quarantine()
            return
        try:
            # Retain the marker when the directory cannot be removed. An
            # unlocked marker is only a quarantine hint, never deletion authority.
            if any(name != '.lease' for name in self._directory_entries()):
                self._quarantine()
                return
            self._remove_owned_lease()
        except OSError:
            self._quarantine()
            return
        self._artifact.close()
        self._grant = None
        try:
            self._directory.rmdir()
        except OSError:
            # Leave this process's slot consumed when residue remains.
            pass
        else:
            self._slots.release()
