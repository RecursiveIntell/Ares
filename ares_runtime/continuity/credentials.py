"""Protected controller credential I/O; SessionDB owns all public identity.

This key signs context bindings, never Desktop effect approvals. File metadata
guards catch copies and replacement; they do not isolate code running as the
same operating-system user. No database file is opened by this module.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


KEY_DIRECTORY = "context-controller-keys"
_KEY_NAME = re.compile(r"[0-9a-f]{64}\.key")


class ControllerCredentialError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _identity(node):
    return [node.st_dev, node.st_ino]


def _private(node, *, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    mode = 0o700 if directory else 0o600
    if (not kind(node.st_mode) or node.st_uid != os.geteuid()
            or stat.S_IMODE(node.st_mode) != mode
            or not directory and node.st_nlink != 1):
        raise ControllerCredentialError("CONTEXT_CONTROLLER_CREDENTIAL_INSECURE")


@contextmanager
def _directory(database_path, *, create=False):
    if not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "geteuid")):
        raise ControllerCredentialError("CONTEXT_CONTROLLER_PLATFORM_UNSUPPORTED")
    path = Path(database_path).absolute().parent
    root_fd = directory_fd = None
    try:
        root_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        root = os.fstat(root_fd)
        if root.st_uid != os.geteuid() or root.st_mode & 0o022:
            raise ControllerCredentialError("CONTEXT_CONTROLLER_DIRECTORY_INSECURE")
        if create:
            try:
                os.mkdir(KEY_DIRECTORY, 0o700, dir_fd=root_fd)
                os.fsync(root_fd)
            except FileExistsError:
                pass
        directory_fd = os.open(KEY_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=root_fd)
        directory = os.fstat(directory_fd)
        _private(directory, directory=True)
        if _identity(os.stat(path, follow_symlinks=False)) != _identity(root):
            raise ControllerCredentialError("CONTEXT_CONTROLLER_DIRECTORY_REPLACED")
        yield directory_fd, _identity(directory)
        if (_identity(os.stat(KEY_DIRECTORY, dir_fd=root_fd, follow_symlinks=False)) != _identity(directory)
                or _identity(os.stat(path, follow_symlinks=False)) != _identity(root)):
            raise ControllerCredentialError("CONTEXT_CONTROLLER_DIRECTORY_REPLACED")
    except OSError:
        raise ControllerCredentialError("CONTEXT_CONTROLLER_CREDENTIAL_UNAVAILABLE") from None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
        if root_fd is not None:
            os.close(root_fd)


def _read_key(directory_fd, key_ref):
    if type(key_ref) is not str or _KEY_NAME.fullmatch(key_ref) is None:
        raise ControllerCredentialError("CONTEXT_CONTROLLER_KEY_REF_INVALID")
    fd = os.open(key_ref, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    try:
        node = os.fstat(fd)
        _private(node)
        raw = os.read(fd, 33)
        if len(raw) != 32 or node.st_size != 32:
            raise ControllerCredentialError("CONTEXT_CONTROLLER_KEY_INVALID")
        if _identity(os.stat(key_ref, dir_fd=directory_fd, follow_symlinks=False)) != _identity(node):
            raise ControllerCredentialError("CONTEXT_CONTROLLER_KEY_REPLACED")
        # An interrupted or concurrent creator may have written all bytes but
        # not flushed them yet. READY must never outrun credential durability.
        os.fsync(fd)
        return Ed25519PrivateKey.from_private_bytes(raw), _identity(node)
    finally:
        os.close(fd)


def create_controller_credential(database_path, key_ref):
    """Create or reconcile the key named by an already durable bootstrap intent.

    A partial file is refused, never overwritten. An existing complete private
    file is the exact orphan left by an interrupted bootstrap. Only public
    facts are returned; the caller commits them to its existing intent.
    """
    if type(key_ref) is not str or _KEY_NAME.fullmatch(key_ref) is None:
        raise ControllerCredentialError("CONTEXT_CONTROLLER_KEY_REF_INVALID")
    with _directory(database_path, create=True) as (directory_fd, directory_identity):
        try:
            fd = os.open(key_ref, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
        except FileExistsError:
            fd = None
        if fd is not None:
            try:
                raw = Ed25519PrivateKey.generate().private_bytes_raw()
                written = os.write(fd, raw)
                if written != len(raw):
                    raise ControllerCredentialError("CONTEXT_CONTROLLER_KEY_WRITE_INCOMPLETE")
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(directory_fd)
        key, file_identity = _read_key(directory_fd, key_ref)
        os.fsync(directory_fd)
        return {"public_key": list(key.public_key().public_bytes_raw()),
                "key_file_identity": file_identity, "directory_identity": directory_identity}


@contextmanager
def controller_credential(database_path, identity):
    """Private owner-only read, valid only while the pinned directory is held."""
    with _directory(database_path) as (directory_fd, directory_identity):
        key, file_identity = _read_key(directory_fd, identity["key_ref"])
        if (directory_identity != identity.get("directory_identity")
                or file_identity != identity.get("key_file_identity")
                or list(key.public_key().public_bytes_raw()) != identity.get("public_key")):
            raise ControllerCredentialError("CONTEXT_CONTROLLER_KEY_REPLACED")
        yield key
