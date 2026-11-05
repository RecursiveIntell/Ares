"""Bounded, local-writer observations for a candidate result reservation.

Registration is an internal building block, not a workspace grant. The caller
must already own the reserved artifact and bind its deadline to an admitted
child run. The existing orchestration owner validates child admission.
Only the real local file-tool path may record; the digest is never computed
by re-reading the result pathname. Same-process/same-UID actors are outside
this module's isolation boundary. Candidate delegation dispatch uses this
observer only for explicitly opted-in local timeout-result recovery.
"""
from __future__ import annotations

import hashlib
import math
import threading
import time

from tools.delegation_partial_file import ReservedResultFile, _MAX_ALLOWED_BYTES

_lock = threading.Lock()
_active: dict[str, ResultWriteGrant] = {}


class ResultWriteGrant:
    """One path's bounded write observation, frozen by its controller."""

    def __init__(self, artifact, child, admission, task_id: str,
                 session_id: str, deadline: float):
        self._artifact = artifact
        self._path = str(artifact.path)
        self._child = child
        self._admission = admission
        self.run_id = task_id
        self._task_id = task_id
        self._session_id = session_id
        self._deadline = deadline
        self._digest: str | None = None
        self._backend_env = None
        self._frozen = False
        self._closed = False

    def freeze(self) -> str | None:
        # Freeze before consulting other owners: an in-flight write must not
        # replace the backend or digest while those checks are in progress.
        with _lock:
            if self._closed:
                raise RuntimeError('result write grant is closed')
            self._frozen = True
            env = self._backend_env
        current = _admission_current(self)
        try:
            from tools.terminal_tool import get_active_env
            backend_current = (env is not None
                               and not hasattr(env, '_hermes_backend_name')
                               and get_active_env(self._task_id) is env)
        except Exception:
            backend_current = False
        with _lock:
            if self._closed:
                raise RuntimeError('result write grant is closed')
            if not current or not backend_current:
                self._digest = None
            return self._digest

    def close(self) -> None:
        with _lock:
            if self._closed:
                return
            if _active.get(self._path) is self:
                del _active[self._path]
            self._frozen = True
            self._closed = True
            self._digest = None
            self._backend_env = None

    def __enter__(self) -> ResultWriteGrant:
        if self._closed:
            raise RuntimeError('result write grant is closed')
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def register_result_write(artifact, *, child, deadline):
    """Register for a currently admitted child; caller owns path and lifetime."""
    from agent.orchestration_runtime import assert_child_dispatch

    admission = assert_child_dispatch(child)
    if type(artifact) is not ReservedResultFile or artifact._closed:
        raise ValueError('an open result reservation is required')
    task_id = getattr(child, '_subagent_id', None)
    session_id = getattr(child, 'session_id', None)
    if any(not isinstance(value, str) or not value.strip() or len(value) > 256
           for value in (task_id, session_id)):
        raise ValueError('explicit bounded child task and session ids required')
    if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline) or deadline <= time.monotonic()):
        raise ValueError('a future finite monotonic deadline is required')
    grant = ResultWriteGrant(artifact, child, admission, task_id, session_id, deadline)
    with _lock:
        if grant._path in _active:
            raise ValueError('result path already registered')
        _active[grant._path] = grant
    return grant


def discard_interrupted_registration(artifact, child):
    """Close only this reservation's grant after interrupted setup."""
    with _lock:
        path = str(artifact.path)
        grant = _active.get(path)
        if grant is None or grant._artifact is not artifact or grant._child is not child:
            return
        del _active[path]
        grant._frozen = True
        grant._closed = True
        grant._digest = None
        grant._backend_env = None


def _admission_current(grant) -> bool:
    """Recheck the existing admission owner; never hold our registry lock."""
    try:
        from agent.orchestration_runtime import assert_child_dispatch

        return (getattr(grant._child, '_subagent_id', None) == grant._task_id
                and getattr(grant._child, 'session_id', None) == grant._session_id
                and assert_child_dispatch(grant._child) is grant._admission)
    except Exception:
        return False


def enforce_reserved_write_limit(path, content, *, unresolved_task_id=None):
    """Bound reserved writes; fail closed for an owner's unresolved fallback.

    While a task owns a reservation, an unresolved path could alias it. Therefore
    oversized fallback writes by that task are rejected even when the original
    spelling differs. This is not a filesystem quota and other write surfaces
    can bypass it. The registry remains until controller and worker finish.
    """
    with _lock:
        reserved = path in _active or (
            isinstance(unresolved_task_id, str) and bool(unresolved_task_id)
            and any(grant._task_id == unresolved_task_id for grant in _active.values())
        )
    if not reserved:
        return
    if not isinstance(content, str) or len(content) > _MAX_ALLOWED_BYTES:
        raise ValueError('reserved result exceeds maximum byte size')
    try:
        size = len(content.encode('utf-8', 'surrogateescape'))
    except UnicodeEncodeError as exc:
        raise ValueError('reserved result cannot be encoded') from exc
    if size > _MAX_ALLOWED_BYTES:
        raise ValueError('reserved result exceeds maximum byte size')


def begin_result_write(task_id, session_id, path):
    """Capture the currently eligible grant before a write starts."""
    with _lock:
        grant = _active.get(path)
        if (grant is None or grant._closed or grant._frozen or grant._artifact._closed
                or task_id != grant._task_id or session_id != grant._session_id
                or time.monotonic() > grant._deadline):
            return None
    if not _admission_current(grant):
        return None
    with _lock:
        if (_active.get(path) is not grant or grant._closed or grant._frozen
                or grant._artifact._closed or time.monotonic() > grant._deadline):
            return None
        return grant


def record_verified_local_write(grant, task_id, session_id, path, content, result, file_ops):
    """Observe a verified canonical local file-tool write for a registered path.

    Called inside the file tool's per-path lock. A verified flag from arbitrary
    or remote file operations is insufficient. Failure to observe is not a
    write failure: recovery must withhold content without a digest.
    """
    if grant is None or not _admission_current(grant):
        return
    with _lock:
        if (grant is None or _active.get(path) is not grant
                or grant._closed or grant._frozen or grant._artifact._closed
                or task_id != grant._task_id or session_id != grant._session_id
                or time.monotonic() > grant._deadline
                or result.get('verified') is not True or result.get('error')
                or not isinstance(content, str) or len(content) > _MAX_ALLOWED_BYTES):
            return
        from tools.file_operations import ShellFileOperations
        from tools.environments.local import LocalEnvironment
        # Plugin providers are stamped by the terminal factory, even if a
        # plugin returns the built-in local class. Do not promote a stamped
        # backend into a host-file observation, regardless of its stamp value.
        if (type(file_ops) is not ShellFileOperations
                or type(file_ops.env) is not LocalEnvironment
                or hasattr(file_ops.env, '_hermes_backend_name')):
            return
        try:
            raw = content.encode('utf-8', 'surrogateescape')
        except UnicodeEncodeError:
            return
        if len(raw) <= _MAX_ALLOWED_BYTES:
            grant._digest = hashlib.sha256(raw).hexdigest()
            grant._backend_env = file_ops.env
