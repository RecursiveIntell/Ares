"""Advisory leaf locks for future run-owned result retention.

A lease alone does not authorize deletion, prove a process is dead, or bind a
path to a run. Callers must provide an already authorized directory descriptor,
serialize creation/reaping separately, and hold the returned fd until done.
This primitive is not connected to production dispatch or a reaper.
"""
from __future__ import annotations

import errno
import os
import stat


class LeaseError(ValueError):
    """The lease cannot safely be created or opened."""


def _flags():
    if os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_NONBLOCK'):
        raise LeaseError('safe lease locking unavailable')
    return os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK


def _lock(fd: int) -> bool:
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return False
        raise LeaseError('lease lock unavailable') from exc
    except ImportError as exc:
        raise LeaseError('lease lock unavailable') from exc


def _validate(fd: int, directory_fd: int) -> None:
    try:
        current = os.stat('.lease', dir_fd=directory_fd, follow_symlinks=False)
        opened = os.fstat(fd)
    except OSError as exc:
        raise LeaseError('lease identity unavailable') from exc
    if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1 or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
        raise LeaseError('unsafe lease identity')


def create_lease(directory_fd: int) -> int:
    """Exclusively create and lock a leaf; caller must close the returned fd."""
    try:
        fd = os.open('.lease', _flags() | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
    except OSError as exc:
        raise LeaseError('lease creation failed') from exc
    try:
        _validate(fd, directory_fd)
        if not _lock(fd):
            raise LeaseError('new lease unexpectedly busy')
        _validate(fd, directory_fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def try_acquire_lease(directory_fd: int) -> int | None:
    """Return a locked fd, None when busy; malformed/missing leaf raises."""
    try:
        fd = os.open('.lease', _flags(), dir_fd=directory_fd)
    except OSError as exc:
        raise LeaseError('lease open failed') from exc
    try:
        _validate(fd, directory_fd)
        if not _lock(fd):
            os.close(fd)
            return None
        _validate(fd, directory_fd)
        return fd
    except BaseException:
        os.close(fd)
        raise
