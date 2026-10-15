"""Immutable attachment custody owned by native SessionDB input receipts.

This module grants no transport, provider or effect authority. The V2 writer
is distinct from legacy text acceptance; GUI ACK/dispatch is not enabled until
its origin, projection and final-wire route qualification are integrated.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from contextlib import contextmanager
from pathlib import Path
import re
import secrets
import stat

from hermes_state_continuity import ContextContinuationError, _canonical, _identity

MAX_MEDIA_BYTES = 25 * 1024 * 1024
MAX_INPUT_MEDIA_BYTES = 100 * 1024 * 1024
MAX_INPUT_MEDIA_COUNT = 32
MAX_MEDIA_PIXELS = 64 * 1024 * 1024
MAX_SCOPE_STORAGE_BYTES = 1024 * 1024 * 1024
_MEDIA_FIELDS = frozenset({'schema', 'conversation_root', 'profile_name', 'sha256',
    'bytes', 'mime_type', 'display_name', 'width', 'height', 'frames'})
_MIME_FORMATS = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'GIF': 'image/gif',
                 'WEBP': 'image/webp', 'BMP': 'image/bmp'}
_ORIGIN_FIELDS = frozenset({'schema', 'connection_id', 'runtime_session_id',
    'stored_session_id', 'profile_name', 'owner_epoch', 'transport_epoch', 'stop_revision'})


def desktop_input_origin(value):
    if (type(value) is not dict or set(value) != _ORIGIN_FIELDS
            or value['schema'] != 'SessionDBDesktopInputOriginV1'):
        raise ContextContinuationError('CONTEXT_INPUT_ORIGIN_INVALID')
    for field in ('runtime_session_id', 'stored_session_id', 'profile_name', 'owner_epoch', 'transport_epoch'):
        _identity(value[field], 'CONTEXT_INPUT_ORIGIN_INVALID')
    if value['connection_id'] is not None:
        _identity(value['connection_id'], 'CONTEXT_INPUT_ORIGIN_INVALID')
    if type(value['stop_revision']) is not int or not 0 <= value['stop_revision'] < 2**63:
        raise ContextContinuationError('CONTEXT_INPUT_ORIGIN_INVALID')
    return value


def validate_media_content(content, *, root=None, profile=None):
    if (type(content) is not dict or set(content) != {'schema', 'text', 'media'}
            or content['schema'] != 'SessionDBContextMediaContentV1'
            or type(content['text']) is not str or type(content['media']) is not list
            or not 1 <= len(content['media']) <= MAX_INPUT_MEDIA_COUNT):
        raise ContextContinuationError('CONTEXT_INPUT_MEDIA_INVALID')
    total = 0
    pixels = 0
    for ref in content['media']:
        if (type(ref) is not dict or set(ref) != _MEDIA_FIELDS
                or ref['schema'] != 'SessionDBContextMediaRefV1'
                or type(ref['sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}', ref['sha256']) is None
                or type(ref['bytes']) is not int or not 0 < ref['bytes'] <= MAX_MEDIA_BYTES
                or ref['mime_type'] not in _MIME_FORMATS.values()
                or type(ref['display_name']) is not str or not ref['display_name']
                or len(ref['display_name']) > 4096 or any(ord(c) < 32 for c in ref['display_name'])
                or any(type(ref[k]) is not int or not 1 <= ref[k] <= MAX_MEDIA_PIXELS for k in ('width', 'height', 'frames'))
                or ref['width'] * ref['height'] * ref['frames'] > MAX_MEDIA_PIXELS):
            raise ContextContinuationError('CONTEXT_INPUT_MEDIA_INVALID')
        _identity(ref['conversation_root'], 'CONTEXT_INPUT_MEDIA_INVALID')
        _identity(ref['profile_name'], 'CONTEXT_INPUT_MEDIA_INVALID')
        if root is not None and ref['conversation_root'] != root or profile is not None and ref['profile_name'] != profile:
            raise ContextContinuationError('CONTEXT_INPUT_SCOPE_MISMATCH')
        total += ref['bytes']
        pixels += ref['width'] * ref['height'] * ref['frames']
    if total > MAX_INPUT_MEDIA_BYTES:
        raise ContextContinuationError('CONTEXT_INPUT_MEDIA_TOO_LARGE')
    if pixels > MAX_MEDIA_PIXELS:
        raise ContextContinuationError('CONTEXT_INPUT_MEDIA_PIXEL_LIMIT')
    return content


def _verified_image(data, mime, *, decode=True):
    if type(data) is not bytes or not data or len(data) > MAX_MEDIA_BYTES:
        raise ContextContinuationError('CONTEXT_INPUT_MEDIA_TOO_LARGE')
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as image:
            actual = _MIME_FORMATS.get(image.format)
            width, height = image.size
            frames = getattr(image, 'n_frames', 1)
            if actual is None or actual != mime:
                raise ContextContinuationError('CONTEXT_INPUT_MEDIA_MIME_MISMATCH')
            if width * height * frames > MAX_MEDIA_PIXELS:
                raise ContextContinuationError('CONTEXT_INPUT_MEDIA_PIXEL_LIMIT')
            image.verify()
        if decode:
            # Header verification is not decode verification (notably JPEG).
            # The bounded pixel/frame metadata is checked before allocating.
            with Image.open(io.BytesIO(data)) as image:
                for frame in range(frames):
                    image.seek(frame)
                    if image.size != (width, height):
                        raise ContextContinuationError('CONTEXT_INPUT_MEDIA_INVALID')
                    image.load()
    except ContextContinuationError:
        raise
    except Exception:
        raise ContextContinuationError('CONTEXT_INPUT_MEDIA_INVALID') from None
    return width, height, frames


class SessionContextMediaMixin:
    def _context_media_directory(self, root, profile, *, create=False):
        _identity(root, 'CONTEXT_INPUT_MEDIA_INVALID')
        _identity(profile, 'CONTEXT_INPUT_MEDIA_INVALID')
        db_path = Path(self.db_path)
        if db_path.name == ':memory:':
            raise ContextContinuationError('CONTEXT_INPUT_MEDIA_DURABLE_STORE_REQUIRED')
        # A database-local byte namespace, not a caller-supplied path or ID pin.
        store = hashlib.sha256(db_path.name.encode()).hexdigest()
        scope = hashlib.sha256(_canonical([root, profile]).encode()).hexdigest()
        base = db_path.parent.resolve()
        path = base
        for segment in ('attachments', 'native-input', store, scope):
            path = path / segment
            if path.is_symlink():
                raise ContextContinuationError('CONTEXT_INPUT_MEDIA_STORAGE_INVALID')
            if create:
                path.mkdir(mode=0o700, exist_ok=True)
            if path.exists() and not path.is_dir():
                raise ContextContinuationError('CONTEXT_INPUT_MEDIA_STORAGE_INVALID')
        return path

    @contextmanager
    def _open_context_media_directory(self, root, profile, *, create=False):
        """Pin each directory component; never check then follow a full path."""
        if any(getattr(os, name, None) is None for name in ('O_NOFOLLOW', 'O_DIRECTORY', 'O_NONBLOCK')):
            raise ContextContinuationError('CONTEXT_INPUT_MEDIA_STORAGE_PLATFORM_UNQUALIFIED')
        directory = self._context_media_directory(root, profile)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = None
        try:
            fd = os.open(directory.anchor, flags)
            for segment in directory.parts[1:]:
                if create:
                    try:
                        os.mkdir(segment, mode=0o700, dir_fd=fd)
                        os.fsync(fd)
                    except FileExistsError:
                        pass
                child = os.open(segment, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        except (NotImplementedError, OSError):
            raise ContextContinuationError('CONTEXT_INPUT_MEDIA_STORAGE_UNAVAILABLE') from None
        finally:
            if fd is not None:
                os.close(fd)

    def _read_context_media_ref(self, ref, *, directory_fd=None):
        if directory_fd is None:
            with self._open_context_media_directory(ref['conversation_root'], ref['profile_name']) as pinned:
                return self._read_context_media_ref(ref, directory_fd=pinned)
        try:
            # O_NONBLOCK makes a substituted FIFO/device rejectable by fstat
            # rather than letting its open defeat the bounded file read.
            fd = os.open(ref['sha256'], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory_fd)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != ref['bytes']:
                    raise ContextContinuationError('CONTEXT_INPUT_MEDIA_CHANGED')
                data = stream.read(ref['bytes'] + 1)
            if len(data) != ref['bytes'] or hashlib.sha256(data).hexdigest() != ref['sha256']:
                raise ContextContinuationError('CONTEXT_INPUT_MEDIA_CHANGED')
            if _verified_image(data, ref['mime_type']) != (ref['width'], ref['height'], ref['frames']):
                raise ContextContinuationError('CONTEXT_INPUT_MEDIA_CHANGED')
            return data
        except ContextContinuationError:
            raise
        except OSError:
            raise ContextContinuationError('CONTEXT_INPUT_MEDIA_UNAVAILABLE') from None

    def _describe_context_media(self, root, profile, item):
        if type(item) is not dict or set(item) != {'data', 'mime_type', 'display_name'}:
            raise ContextContinuationError('CONTEXT_INPUT_MEDIA_INVALID')
        data = item['data']
        width, height, frames = _verified_image(data, item['mime_type'], decode=False)
        ref = {'schema': 'SessionDBContextMediaRefV1', 'conversation_root': root,
            'profile_name': profile, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
            'mime_type': item['mime_type'], 'display_name': item['display_name'],
            'width': width, 'height': height, 'frames': frames}
        validate_media_content({'schema': 'SessionDBContextMediaContentV1', 'text': '', 'media': [ref]},
                               root=root, profile=profile)
        return ref

    def _publish_context_media(self, root, profile, item):
        ref = self._describe_context_media(root, profile, item)
        data = item['data']
        with self._open_context_media_directory(root, profile, create=True) as directory_fd:
            temporary = None
            try:
                try:
                    os.stat(ref['sha256'], dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    self._read_context_media_ref(ref, directory_fd=directory_fd)
                    return ref
                # A failed commit can leave charged orphan bytes, but neither
                # a conflicting ID nor a full inbox reaches this publication.
                with os.scandir(directory_fd) as entries:
                    stored = sum(p.stat(follow_symlinks=False).st_size for p in entries)
                if stored + len(data) > MAX_SCOPE_STORAGE_BYTES:
                    raise ContextContinuationError('CONTEXT_INPUT_MEDIA_STORAGE_LIMIT')
                temporary = '.unpublished-' + secrets.token_hex(16)
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory_fd)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fchmod(stream.fileno(), 0o400)
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, ref['sha256'], src_dir_fd=directory_fd,
                            dst_dir_fd=directory_fd, follow_symlinks=False)
                except FileExistsError:
                    self._read_context_media_ref(ref, directory_fd=directory_fd)
                os.fsync(directory_fd)
                self._read_context_media_ref(ref, directory_fd=directory_fd)
                # No ACK for bytes published into a replaced/orphaned directory.
                with self._open_context_media_directory(root, profile) as current_fd:
                    pinned, current = os.fstat(directory_fd), os.fstat(current_fd)
                    if (pinned.st_dev, pinned.st_ino) != (current.st_dev, current.st_ino):
                        raise ContextContinuationError('CONTEXT_INPUT_MEDIA_STORAGE_CHANGED')
                return ref
            except ContextContinuationError:
                raise
            except OSError:
                raise ContextContinuationError('CONTEXT_INPUT_MEDIA_STORAGE_UNAVAILABLE') from None
            finally:
                if temporary is not None:
                    os.unlink(temporary, dir_fd=directory_fd)

    def accept_context_media_input(self, session_id, *, source, event_id, text, media,
                                   desktop_origin, timestamp=None, display_metadata=None):
        if source != 'desktop' or type(text) is not str or type(media) not in (list, tuple):
            raise ContextContinuationError('CONTEXT_INPUT_MEDIA_INVALID')
        if not 1 <= len(media) <= MAX_INPUT_MEDIA_COUNT:
            raise ContextContinuationError('CONTEXT_INPUT_MEDIA_COUNT_LIMIT')
        if getattr(os, 'O_NOFOLLOW', None) is None:
            raise ContextContinuationError('CONTEXT_INPUT_MEDIA_STORAGE_PLATFORM_UNQUALIFIED')
        origin = desktop_input_origin(json.loads(_canonical(desktop_origin)))
        if origin['stored_session_id'] != session_id:
            raise ContextContinuationError('CONTEXT_INPUT_ORIGIN_INVALID')
        # Freeze/validate every caller byte and aggregate before publishing any.
        frozen = []
        total = 0
        for item in media:
            if type(item) is not dict or set(item) != {'data', 'mime_type', 'display_name'}:
                raise ContextContinuationError('CONTEXT_INPUT_MEDIA_INVALID')
            snapshot = dict(item)
            _verified_image(snapshot['data'], snapshot['mime_type'], decode=False)
            total += len(snapshot['data'])
            frozen.append(snapshot)
        if total > MAX_INPUT_MEDIA_BYTES:
            raise ContextContinuationError('CONTEXT_INPUT_MEDIA_TOO_LARGE')
        with self._read_ctx() as conn:
            original_root, original_profile = self._context_input_scope_on_conn(conn, session_id)
        if origin['profile_name'] != original_profile:
            raise ContextContinuationError('CONTEXT_INPUT_SCOPE_MISMATCH')
        refs = [self._describe_context_media(original_root, original_profile, item) for item in frozen]
        content = {'schema': 'SessionDBContextMediaContentV1', 'text': text, 'media': refs}
        validate_media_content(content, root=original_root, profile=original_profile)
        # Aggregate bounds precede decode; every image decodes before any byte
        # publication. No rejected trailing image can leave earlier publishes.
        for item in frozen:
            _verified_image(item['data'], item['mime_type'])
        def materialize(root, profile):
            if (root, profile) != (original_root, original_profile):
                raise ContextContinuationError('CONTEXT_INPUT_SCOPE_MISMATCH')
            published = [self._publish_context_media(root, profile, item) for item in frozen]
            if published != refs:
                raise ContextContinuationError('CONTEXT_INPUT_MEDIA_CHANGED')
            return content
        return self._accept_context_input_owned(session_id, source=source, event_id=event_id,
            content=content, timestamp=timestamp, display_metadata=display_metadata,
            origin=origin, receipt_schema='SessionDBContextInputV2', content_factory=materialize)

    def read_context_input_media(self, session_id, receipt):
        from hermes_state_inbox import ContextInputReceipt
        if type(receipt) is not ContextInputReceipt or receipt.schema != 'SessionDBContextInputV2':
            raise ContextContinuationError('CONTEXT_INPUT_MEDIA_INVALID')
        with self._read_ctx() as conn:
            root, profile = self._context_input_scope_on_conn(conn, session_id)
            if (root, profile) != (receipt.conversation_root, receipt.profile_name):
                raise ContextContinuationError('CONTEXT_INPUT_SCOPE_MISMATCH')
            if self._read_context_input_on_conn(conn, root, receipt.sequence) != receipt:
                raise ContextContinuationError('CONTEXT_INPUT_RECORD_INVALID')
            self._context_input_origin_on_conn(conn, receipt)
            validate_media_content(receipt.content, root=root, profile=profile)
        return tuple(self._read_context_media_ref(ref) for ref in receipt.content['media'])
