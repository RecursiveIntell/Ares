"""Custody boundary witnesses using neutral bytes and real native stores."""
import io
import os
from pathlib import Path
import subprocess
import sys

from PIL import Image
import pytest

from hermes_state import SessionDB
from hermes_state_continuity import ContextContinuationError


def png(color='red'):
    output = io.BytesIO()
    Image.new('RGB', (8, 8), color).save(output, format='PNG')
    return output.getvalue()


@pytest.fixture
def db(tmp_path):
    value = SessionDB(tmp_path / 'state.db')
    value.create_session('owned', source='desktop')
    yield value
    value.close()


def accept(db, data=None, event='image-input'):
    return db.accept_context_media_input('owned', source='desktop', event_id=event,
        text='Neutral image', media=[{'data': png() if data is None else data,
            'mime_type': 'image/png', 'display_name': 'original.png'}],
        desktop_origin={'schema': 'SessionDBDesktopInputOriginV1', 'connection_id': None,
            'runtime_session_id': 'runtime-owned', 'stored_session_id': 'owned',
            'profile_name': 'default', 'owner_epoch': 'owner-epoch',
            'transport_epoch': 'transport-epoch', 'stop_revision': 0})


def test_same_id_conflict_never_publishes_unreferenced_bytes(db):
    receipt = accept(db)
    directory = db._context_media_directory(receipt.conversation_root, receipt.profile_name)
    before = set(directory.iterdir())
    with pytest.raises(ContextContinuationError, match='ID_COLLISION'):
        accept(db, data=png('blue'))
    assert set(directory.iterdir()) == before


def test_full_inbox_refuses_before_publishing_media(db):
    for index in range(128):
        db.accept_context_input('owned', source='desktop', event_id=f'text-{index}', content='pending')
    with pytest.raises(ContextContinuationError, match='PENDING_LIMIT'):
        accept(db)
    assert not (db.db_path.parent / 'attachments').exists()


@pytest.mark.linux_only
def test_fifo_replacement_refuses_without_blocking_the_reader(db):
    receipt = accept(db)
    ref = receipt.content['media'][0]
    path = db._context_media_directory(receipt.conversation_root, receipt.profile_name) / ref['sha256']
    path.unlink()
    os.mkfifo(path)
    probe = """
import sys
from pathlib import Path
import hermes_state
from hermes_state import SessionDB
from hermes_state_continuity import ContextContinuationError
assert Path(hermes_state.__file__).resolve() == Path(sys.argv[2]) / 'hermes_state.py'
value = SessionDB(Path(sys.argv[1]))
try:
    receipt = value.read_context_input('owned', source='desktop', event_id='image-input')
    try:
        value.read_context_input_media('owned', receipt)
    except ContextContinuationError as exc:
        assert exc.code.startswith('CONTEXT_INPUT_MEDIA_')
    else:
        raise AssertionError('FIFO admitted as immutable media')
finally:
    value.close()
"""
    source = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, '-P', '-c', probe, str(db.db_path), str(source)],
                            env={**os.environ, 'PYTHONPATH': str(source)},
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


@pytest.mark.linux_only
def test_scope_directory_replacement_does_not_follow_a_new_symlink(db, tmp_path, monkeypatch):
    receipt = accept(db)
    ref = receipt.content['media'][0]
    directory = db._context_media_directory(receipt.conversation_root, receipt.profile_name)
    external = tmp_path / 'foreign-storage'
    external.mkdir()
    (external / ref['sha256']).write_bytes(png())
    original_open = os.open
    switched, foreign_opens = [], []

    def guarded_open(path, flags, *args, **kwargs):
        if str(path).endswith(ref['sha256']) and not switched:
            directory.rename(directory.with_name(directory.name + '-old'))
            directory.symlink_to(external, target_is_directory=True)
            switched.append(True)
        value = original_open(path, flags, *args, **kwargs)
        if switched and str(path).endswith(ref['sha256']):
            target = os.readlink(f'/proc/self/fd/{value}')
            if str(external) in target:
                foreign_opens.append(target)
        return value

    with monkeypatch.context() as m:
        m.setattr(os, 'open', guarded_open)
        # A pinned reader can finish with the original bytes or refuse a
        # changed namespace. It must never follow the replacement path.
        try:
            assert db.read_context_input_media('owned', receipt) == (png(),)
        except ContextContinuationError as exc:
            assert exc.code.startswith('CONTEXT_INPUT_MEDIA_')
        assert switched
        assert foreign_opens == []


def test_failed_staging_open_retains_typed_refusal_and_no_receipt(db, monkeypatch):
    original_open = os.open
    def refuse_staging(path, *args, **kwargs):
        if str(path).startswith('.unpublished-'):
            raise PermissionError('neutral staging refusal')
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(os, 'open', refuse_staging)
    with pytest.raises(ContextContinuationError, match='MEDIA_STORAGE_UNAVAILABLE'):
        accept(db)
    assert db.read_context_input('owned', source='desktop', event_id='image-input') is None


def test_aggregate_pixel_limit_refuses_before_publication(db, monkeypatch):
    import hermes_state_input_media as media_owner

    monkeypatch.setattr(media_owner, 'MAX_MEDIA_PIXELS', 100)
    one = {'data': png(), 'mime_type': 'image/png', 'display_name': 'neutral.png'}
    with pytest.raises(ContextContinuationError, match='MEDIA_PIXEL_LIMIT'):
        db.accept_context_media_input('owned', source='desktop', event_id='pixels', text='',
            media=[one, one], desktop_origin={'schema': 'SessionDBDesktopInputOriginV1',
                'connection_id': None, 'runtime_session_id': 'runtime-owned',
                'stored_session_id': 'owned', 'profile_name': 'default',
                'owner_epoch': 'owner-epoch', 'transport_epoch': 'transport-epoch', 'stop_revision': 0})
    assert not (db.db_path.parent / 'attachments').exists()


def test_header_readable_but_truncated_jpeg_refuses_before_acceptance(db):
    output = io.BytesIO()
    Image.new('RGB', (8, 8), 'red').save(output, format='JPEG')
    malformed = output.getvalue()[:-5]
    # Establish the fixture's precise property, not a magic input snapshot.
    with Image.open(io.BytesIO(malformed)) as image:
        image.verify()
    with Image.open(io.BytesIO(malformed)) as image:
        with pytest.raises(OSError):
            image.load()
    with pytest.raises(ContextContinuationError, match='MEDIA_INVALID'):
        db.accept_context_media_input('owned', source='desktop', event_id='truncated', text='',
            media=[{'data': malformed, 'mime_type': 'image/jpeg', 'display_name': 'neutral.jpg'}],
            desktop_origin={'schema': 'SessionDBDesktopInputOriginV1', 'connection_id': None,
                'runtime_session_id': 'runtime-owned', 'stored_session_id': 'owned',
                'profile_name': 'default', 'owner_epoch': 'owner-epoch',
                'transport_epoch': 'transport-epoch', 'stop_revision': 0})
    assert db.read_context_input('owned', source='desktop', event_id='truncated') is None


@pytest.mark.parametrize('format,mime', [('PNG', 'image/png'), ('JPEG', 'image/jpeg'),
    ('GIF', 'image/gif'), ('WEBP', 'image/webp'), ('BMP', 'image/bmp')])
def test_valid_supported_formats_preserve_original_bytes(db, format, mime):
    output = io.BytesIO()
    Image.new('RGB', (8, 8), 'red').save(output, format=format)
    data = output.getvalue()
    original = accept(db)
    receipt = db.accept_context_media_input('owned', source='desktop', event_id='format-' + format,
        text='Neutral image', media=[{'data': data, 'mime_type': mime, 'display_name': 'neutral-image'}],
        desktop_origin=db.read_context_input_origin('owned', original))
    assert db.read_context_input_media('owned', receipt) == (data,)
    assert receipt.content['media'][0]['mime_type'] == mime


def test_failed_native_write_after_publication_retains_original_receipt_and_bytes(db):
    import sqlite3
    from hermes_state_inbox import _event_key

    original = accept(db)
    failed_key = _event_key(original.conversation_root, original.sequence + 1)
    # Native SQLite abort after filesystem publication, not a mock that fails
    # before entering the write callback. The key contains only native hex/IDs.
    db._execute_write(lambda conn: conn.execute(
        f"CREATE TRIGGER neutral_receipt_abort BEFORE INSERT ON state_meta "
        f"WHEN NEW.key='{failed_key}' BEGIN SELECT RAISE(ABORT, 'neutral receipt abort'); END"))
    try:
        with pytest.raises(sqlite3.IntegrityError, match='neutral receipt abort'):
            accept(db, data=png('blue'), event='neutral-abort')
        assert db.read_context_input('owned', source='desktop', event_id='neutral-abort') is None
        assert db.read_context_input_media('owned', original) == (png(),)
        assert db.read_context_input('owned', source='desktop', event_id='image-input') == original
    finally:
        db._execute_write(lambda conn: conn.execute('DROP TRIGGER neutral_receipt_abort'))
    repaired = accept(db, data=png('blue'), event='neutral-abort')
    assert repaired.sequence == original.sequence + 1
    assert db.read_context_input_media('owned', repaired) == (png('blue'),)
