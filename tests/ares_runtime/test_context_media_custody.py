"""Planned native media custody behavior through the existing SessionDB owner.

Neutral generated images only. These new-method witnesses are feature RED,
not evidence that a historical user image or screenshot was lost.
"""
import io
import json
from dataclasses import asdict

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
    value.create_session('foreign', source='desktop', profile_name='other')
    yield value
    value.close()


def origin():
    return {'schema': 'SessionDBDesktopInputOriginV1', 'connection_id': None,
            'runtime_session_id': 'runtime-owned', 'stored_session_id': 'owned',
            'profile_name': 'default', 'owner_epoch': 'owner-epoch',
            'transport_epoch': 'transport-epoch', 'stop_revision': 0}


def accept(db, *, event='image-input', data=None, **kwargs):
    return db.accept_context_media_input(
        'owned', source='desktop', event_id=event, text='Inspect this image',
        media=[{'data': png() if data is None else data,
                'mime_type': 'image/png', 'display_name': 'original.png'}],
        desktop_origin=origin(), **kwargs,
    )


def test_exact_media_receipt_roundtrip_and_custody_are_native(db):
    receipt = accept(db)
    assert receipt.schema == 'SessionDBContextInputV2'
    assert receipt.content['text'] == 'Inspect this image'
    assert receipt.content['media'][0]['display_name'] == 'original.png'
    assert db.read_context_input('owned', source='desktop', event_id='image-input') == receipt
    assert db.read_context_input_origin('owned', receipt) == origin()
    assert db.read_context_input_media('owned', receipt) == (png(),)
    assert accept(db) == receipt


def test_same_id_changed_media_refuses_without_replacing_original(db):
    receipt = accept(db)
    with pytest.raises(ContextContinuationError, match='ID_COLLISION'):
        accept(db, data=png('blue'))
    assert db.read_context_input_media('owned', receipt) == (png(),)
    assert db.read_context_input('owned', source='desktop', event_id='image-input') == receipt


def test_media_read_refuses_foreign_profile_or_root(db):
    receipt = accept(db)
    with pytest.raises(ContextContinuationError, match='SCOPE_MISMATCH'):
        db.read_context_input_media('foreign', receipt)


def test_origin_is_frozen_and_not_backfilled(db):
    receipt = accept(db)
    changed = origin()
    changed['transport_epoch'] = 'replacement'
    with pytest.raises(ContextContinuationError, match='ORIGIN_COLLISION'):
        db.accept_context_media_input(
            'owned', source='desktop', event_id='image-input', text='Inspect this image',
            media=[{'data': png(), 'mime_type': 'image/png', 'display_name': 'original.png'}],
            desktop_origin=changed,
        )
    assert db.read_context_input_origin('owned', receipt) == origin()


def test_unknown_media_version_does_not_reuse_legacy_text_schema(db):
    receipt = accept(db)
    from hermes_state_inbox import ContextInputReceipt
    raw = asdict(receipt)
    raw['schema'] = 'SessionDBContextInputV999'
    with pytest.raises(ContextContinuationError, match='RECORD_INVALID'):
        ContextInputReceipt.from_raw(json.dumps(raw))


@pytest.mark.parametrize('data,mime', [(b'not an image', 'image/png'), (png(), 'image/jpeg')])
def test_invalid_or_mismatched_image_bytes_refuse_before_acceptance(db, data, mime):
    with pytest.raises(ContextContinuationError, match='MEDIA_'):
        db.accept_context_media_input(
            'owned', source='desktop', event_id='invalid', text='Inspect this image',
            media=[{'data': data, 'mime_type': mime, 'display_name': 'original.png'}],
            desktop_origin=origin(),
        )
    assert db.read_context_input('owned', source='desktop', event_id='invalid') is None


def test_media_payload_stays_bounded_metadata_not_inline_base64(db):
    receipt = accept(db)
    import json
    raw = json.dumps(asdict(receipt))
    assert 'data' not in receipt.content['media'][0]
    assert len(raw.encode()) < 900_000
    assert db.read_context_input_media('owned', receipt) == (png(),)


def test_failed_input_commit_never_acknowledges_an_occurrence(db, monkeypatch):
    original = db._execute_write
    with monkeypatch.context() as m:
        m.setattr(db, '_execute_write', lambda *a, **k: (_ for _ in ()).throw(OSError('controlled write refusal')))
        with pytest.raises(OSError, match='controlled write refusal'):
            accept(db)
    assert db.read_context_input('owned', source='desktop', event_id='image-input') is None
    assert accept(db).sequence == 1
    assert db._execute_write == original


@pytest.mark.parametrize('replacement', [[], {}, None, True])
def test_malformed_version_is_a_typed_refusal(db, replacement):
    from hermes_state_inbox import ContextInputReceipt
    raw = asdict(accept(db))
    raw['schema'] = replacement
    with pytest.raises(ContextContinuationError, match='RECORD_INVALID'):
        ContextInputReceipt.from_raw(json.dumps(raw))


def test_removed_origin_never_becomes_legacy_unscoped_media(db):
    from hermes_state_inbox import _event_key
    receipt = accept(db)
    db._execute_write(lambda conn: conn.execute(
        'DELETE FROM state_meta WHERE key=?',
        (_event_key(receipt.conversation_root, receipt.sequence) + ':origin',),
    ))
    with pytest.raises(ContextContinuationError, match='ORIGIN_MISSING'):
        db.read_context_input_media('owned', receipt)


@pytest.mark.parametrize('replace_with_link', [False, True])
def test_changed_or_symlinked_blob_cannot_satisfy_a_receipt(db, tmp_path, replace_with_link):
    receipt = accept(db)
    ref = receipt.content['media'][0]
    path = db._context_media_directory(receipt.conversation_root, receipt.profile_name) / ref['sha256']
    path.unlink()
    if replace_with_link:
        external = tmp_path / 'outside.png'
        external.write_bytes(png())
        path.symlink_to(external)
    else:
        path.write_bytes(png('blue'))
    with pytest.raises(ContextContinuationError, match='MEDIA_'):
        db.read_context_input_media('owned', receipt)


def test_mutable_caller_values_freeze_before_write_reservation(db, monkeypatch):
    image = {'data': png(), 'mime_type': 'image/png', 'display_name': 'original.png'}
    metadata = {'visible': 'original'}
    native_origin = origin()
    write = db._execute_write
    def mutate(fn, *args, **kwargs):
        image['data'] = png('blue')
        image['display_name'] = 'changed.png'
        metadata['visible'] = 'changed'
        native_origin['transport_epoch'] = 'changed'
        return write(fn, *args, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(db, '_execute_write', mutate)
        receipt = db.accept_context_media_input(
            'owned', source='desktop', event_id='frozen', text='', media=[image],
            desktop_origin=native_origin, display_metadata=metadata,
        )
    assert receipt.content['text'] == ''
    assert receipt.display_metadata == {'visible': 'original'}
    assert receipt.content['media'][0]['display_name'] == 'original.png'
    assert db.read_context_input_origin('owned', receipt) == origin()
    assert db.read_context_input_media('owned', receipt) == (png(),)


def test_oversized_metadata_refuses_without_publishing_bytes(db):
    with pytest.raises(ContextContinuationError, match='TOO_LARGE'):
        db.accept_context_media_input(
            'owned', source='desktop', event_id='huge', text='x' * 900_001,
            media=[{'data': png(), 'mime_type': 'image/png', 'display_name': 'original.png'}],
            desktop_origin=origin(),
        )
    assert not (db.db_path.parent / 'attachments').exists()
    assert db.read_context_input('owned', source='desktop', event_id='huge') is None


def test_two_native_connections_cannot_race_past_the_storage_quota(db, monkeypatch):
    import threading
    import hermes_state_input_media as media_owner
    other = SessionDB(db.db_path)
    inputs = [png(), png('blue')]
    monkeypatch.setattr(media_owner, 'MAX_SCOPE_STORAGE_BYTES', sum(map(len, inputs)) - 1)
    results = []
    gate = threading.Barrier(2)
    def send(owner, index):
        gate.wait(timeout=5)
        try:
            receipt = accept(owner, event=f'quota-{index}', data=inputs[index])
            results.append(('accepted', receipt))
        except ContextContinuationError as exc:
            results.append(('refused', exc.code))
    workers = [threading.Thread(target=send, args=(owner, index), daemon=True)
               for index, owner in enumerate((db, other))]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
        assert all(not worker.is_alive() for worker in workers)
        assert [kind for kind, _ in results].count('accepted') == 1
        assert [kind for kind, _ in results].count('refused') == 1
        assert next(value for kind, value in results if kind == 'refused') == 'CONTEXT_INPUT_MEDIA_STORAGE_LIMIT'
        assert len(db.read_pending_context_inputs('owned')) == 1
    finally:
        other.close()
