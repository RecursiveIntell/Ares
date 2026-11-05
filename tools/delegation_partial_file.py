"""Bounded, opt-in partial-result files inside a caller-admitted workspace.

This module does not grant a workspace or dispatch a worker. The caller must
provide an authoritative workspace and explicitly opt in before running the
child. A reserved filename must not already exist. Recovery is an untrusted,
possibly incomplete observation, never evidence that a child completed.

Only POSIX systems with descriptor-relative no-follow opens are supported;
other systems fail closed. A same-UID actor can still write to the workspace,
and metadata checks cannot prove who wrote bytes or prevent all concurrent
writes. Consumers must apply their own redaction and output budget before
placing recovered text in messages or receipts. Nonempty content is withheld
unless its complete bounded bytes match a caller-supplied trusted SHA-256.
This module does not authenticate the supplied digest, identify its source,
or bind it to a child, run, backend, session, or authorization decision.
"""
from __future__ import annotations

import codecs
from dataclasses import dataclass
import errno
import hashlib
import hmac
import os
from pathlib import Path
import stat
from typing import Literal

_MAX_ALLOWED_BYTES = 65536


class ArtifactBoundaryError(ValueError):
    """An artifact reservation cannot safely be made or used."""


@dataclass(frozen=True)
class ArtifactObservation:
    status: Literal['available', 'empty', 'missing', 'unsafe', 'changed', 'unverified',
                    'invalid_text', 'workspace_changed', 'policy_denied', 'unavailable']
    text: str | None = None
    truncated: bool = False


def _open_workspace(path: Path) -> int:
    """Open every component without following links; caller owns returned fd."""
    if (os.name != 'posix' or not hasattr(os, 'O_DIRECTORY')
            or not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_NONBLOCK')
            or not hasattr(os, 'geteuid') or os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd
            or os.stat not in os.supports_follow_symlinks):
        raise ArtifactBoundaryError('descriptor-safe result files unsupported')
    if not path.is_absolute() or path == Path('/') or '..' in path.parts:
        raise ArtifactBoundaryError('workspace must be an absolute non-root path')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = None
    try:
        fd = os.open('/', flags)
        for part in path.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        raise ArtifactBoundaryError('workspace unavailable or contains a symlink') from exc


def _check_artifact_policy(path: Path) -> None:
    try:
        from agent.file_safety import (
            get_cross_profile_warning,
            get_read_block_error,
            get_sandbox_mirror_warning,
            get_write_denied_error,
            is_write_approval_required,
        )
        target = str(path)
        if (get_write_denied_error(target) or is_write_approval_required(target)
                or get_cross_profile_warning(target) or get_sandbox_mirror_warning(target)
                or get_read_block_error(target)):
            raise ArtifactBoundaryError('result path is denied by file policy')
    except ArtifactBoundaryError:
        raise
    except Exception as exc:
        raise ArtifactBoundaryError('result file policy unavailable') from exc


def _stable_stat(st: os.stat_result) -> tuple[int, int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


class ReservedResultFile:
    """A newly reserved basename in an already admitted workspace directory."""

    def __init__(self, workspace: Path, name: str, directory_fd: int, max_bytes: int):
        self.path = workspace / name
        self._workspace = workspace
        self._name = name
        self._dir_fd = directory_fd
        self._directory_identity = (os.fstat(directory_fd).st_dev,
                                    os.fstat(directory_fd).st_ino)
        self._max_bytes = max_bytes
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            os.close(self._dir_fd)
            self._closed = True

    def __enter__(self) -> ReservedResultFile:
        if self._closed:
            raise ArtifactBoundaryError('result file reservation closed')
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def recover(self, *, expected_sha256: str | None = None) -> ArtifactObservation:
        """Recover only bytes matching a digest from a separately trusted owner.

        A digest computed from this path at recovery time is not provenance.
        The caller must bind it to the authorized write and run. This helper
        itself does not establish that binding.
        """
        if self._closed:
            raise ArtifactBoundaryError('result file reservation closed')
        try:
            _check_artifact_policy(self.path)
        except ArtifactBoundaryError:
            return ArtifactObservation('policy_denied')
        try:
            current_dir = _open_workspace(self._workspace)
        except ArtifactBoundaryError:
            return ArtifactObservation('workspace_changed')
        try:
            st = os.fstat(current_dir)
            if (st.st_dev, st.st_ino) != self._directory_identity:
                return ArtifactObservation('workspace_changed')
        finally:
            os.close(current_dir)

        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(self._name, flags, dir_fd=self._dir_fd)
        except FileNotFoundError:
            return ArtifactObservation('missing')
        except OSError as exc:
            status = 'unsafe' if exc.errno in {errno.ELOOP, errno.ENXIO, errno.ENODEV, errno.EISDIR} else 'unavailable'
            return ArtifactObservation(status)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_uid != os.geteuid()):
                return ArtifactObservation('unsafe')
            chunks: list[bytes] = []
            # Verify the complete file, not only the output prefix: two files
            # with an identical prefix must not share an approval. Refuse a
            # file exceeding the absolute bounded-read limit.
            remaining = _MAX_ALLOWED_BYTES + 1
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(fd)
            try:
                current = os.stat(self._name, dir_fd=self._dir_fd, follow_symlinks=False)
            except OSError:
                return ArtifactObservation('changed')
            if (_stable_stat(before) != _stable_stat(after)
                    or _stable_stat(after) != _stable_stat(current)):
                return ArtifactObservation('changed')
            raw = b''.join(chunks)
            if not raw:
                return ArtifactObservation('empty')
            if (len(raw) > _MAX_ALLOWED_BYTES
                    or not isinstance(expected_sha256, str)
                    or len(expected_sha256) != 64
                    or any(c not in '0123456789abcdef' for c in expected_sha256)
                    or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256)):
                return ArtifactObservation('unverified')
            truncated = len(raw) > self._max_bytes
            try:
                decoder = codecs.getincrementaldecoder('utf-8')('strict')
                text = decoder.decode(raw[:self._max_bytes], final=not truncated)
            except UnicodeDecodeError:
                return ArtifactObservation('invalid_text')
            if not text:
                return ArtifactObservation('empty', truncated=truncated)
            return ArtifactObservation('available', text=text, truncated=truncated)
        except OSError:
            return ArtifactObservation('unavailable')
        finally:
            os.close(fd)


def reserve_result_file(workspace: str | os.PathLike[str], name: str, *,
                        max_bytes: int = 8192) -> ReservedResultFile:
    """Exclusively create a private result file under an admitted workspace.

    Only a basename is accepted. Existing entries, including dangling symlinks,
    are never overwritten. The caller owns and must close the returned handle;
    the resulting file is intentionally not removed automatically.
    """
    if (not isinstance(name, str) or not name or name in {'.', '..'}
            or '/' in name or '\\' in name or '\x00' in name):
        raise ArtifactBoundaryError('result filename must be a bounded basename')
    try:
        if len(name.encode('utf-8')) > 200:
            raise ArtifactBoundaryError('result filename must be a bounded basename')
    except UnicodeEncodeError as exc:
        raise ArtifactBoundaryError('result filename must be UTF-8') from exc
    if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
            or not 0 < max_bytes <= _MAX_ALLOWED_BYTES):
        raise ArtifactBoundaryError('invalid result byte limit')
    root = Path(workspace)
    _check_artifact_policy(root / name)
    fd = _open_workspace(root)
    try:
        created = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o600, dir_fd=fd)
        os.close(created)
        return ReservedResultFile(root, name, fd, max_bytes)
    except OSError as exc:
        os.close(fd)
        raise ArtifactBoundaryError('result file could not be reserved exclusively') from exc
