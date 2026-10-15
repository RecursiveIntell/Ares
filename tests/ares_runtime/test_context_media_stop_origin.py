"""Native media admission must preserve the observed canonical Stop epoch.

Neutral generated images/real SQLite only. Historical data is not a permit.
"""
from dataclasses import asdict
import sqlite3

import pytest

from hermes_state import SessionDB
from hermes_state_continuity import ContextContinuationError, _canonical
from tests.ares_runtime.test_context_media_custody import db, png, origin  # noqa: F401


def send(owner, *, revision=0, event='native-origin', color='red'):
    witness = origin()
    witness['stop_revision'] = revision
    return owner.accept_context_media_input('owned', source='desktop', event_id=event,
        text='Neutral frozen input',
        media=[{'data': png(color), 'mime_type': 'image/png', 'display_name': 'neutral.png'}],
        desktop_origin=witness)


def assert_no_admission(owner, event='native-origin'):
    assert owner.read_context_input('owned', source='desktop', event_id=event) is None
    assert not (owner.db_path.parent/'attachments').exists()


def test_stale_stop_origin_refuses_before_publication_or_receipt(db):
    control = db.record_context_stop('owned')
    assert control['revision'] > 0
    with pytest.raises(ContextContinuationError, match='CONTEXT_INPUT_ORIGIN_STOP_CHANGED'):
        send(db)
    assert_no_admission(db)
    assert db.read_context_stop('owned')['control'] == control


def test_future_stop_origin_cannot_mint_an_epoch(db):
    assert db.read_context_stop('owned') is None
    with pytest.raises(ContextContinuationError, match='CONTEXT_INPUT_ORIGIN_STOP_CHANGED'):
        send(db, revision=1)
    assert_no_admission(db)
    assert db.read_context_stop('owned') is None


def test_current_post_cut_origin_accepts_without_changing_the_cut(db):
    control = db.record_context_stop('owned')
    receipt = send(db, revision=control['revision'])
    assert receipt.sequence > control['input_sequence']
    assert db.read_context_input_origin('owned', receipt)['stop_revision'] == control['revision']
    assert db.read_context_input_work('owned')['receipts'] == (receipt,)
    assert db.read_context_stop('owned')['control'] == control
    assert db.read_context_input_media('owned', receipt) == (png(),)


def test_stop_between_frozen_origin_and_write_reservation_refuses(db, monkeypatch):
    other = SessionDB(db.db_path)
    write = db._execute_write
    cuts = []
    def cross_cut(fn, *args, **kwargs):
        if not cuts:
            cuts.append(other.record_context_stop('owned'))
        return write(fn, *args, **kwargs)
    try:
        with monkeypatch.context() as m:
            m.setattr(db, '_execute_write', cross_cut)
            with pytest.raises(ContextContinuationError, match='CONTEXT_INPUT_ORIGIN_STOP_CHANGED'):
                send(db)
        assert len(cuts) == 1
        assert_no_admission(db)
        assert db.read_context_stop('owned')['control'] == cuts[0]
    finally:
        other.close()


def test_historical_duplicate_retains_bytes_origin_and_stopped_eligibility(db):
    receipt = send(db)
    before = asdict(receipt)
    original_origin = db.read_context_input_origin('owned', receipt)
    directory = db._context_media_directory(receipt.conversation_root, receipt.profile_name)
    files = set(directory.iterdir())
    control = db.record_context_stop('owned')
    assert control['input_sequence'] == receipt.sequence
    # Exact duplicate is accepted-data reconciliation, never a new Send/cut.
    assert send(db) == receipt
    assert asdict(db.read_context_input('owned', source='desktop', event_id='native-origin')) == before
    assert db.read_context_input_origin('owned', receipt) == original_origin
    assert db.read_context_input_media('owned', receipt) == (png(),)
    assert set(directory.iterdir()) == files
    assert db.read_context_input_work('owned')['receipts'] == ()
    assert db.read_context_stop('owned')['control'] == control
    with db._read_ctx() as conn:
        with pytest.raises(ContextContinuationError, match='CONTEXT_INPUT_STOPPED'):
            db._assert_context_input_after_stop_on_conn(conn, receipt.conversation_root, receipt)


def test_matching_new_epoch_cannot_backfill_a_historical_duplicate(db):
    receipt = send(db)
    original_origin = db.read_context_input_origin('owned', receipt)
    control = db.record_context_stop('owned')
    with pytest.raises(ContextContinuationError, match='CONTEXT_INPUT_ORIGIN_COLLISION'):
        send(db, revision=control['revision'])
    assert db.read_context_input_origin('owned', receipt) == original_origin
    assert db.read_context_input_media('owned', receipt) == (png(),)
    assert db.read_context_input_work('owned')['receipts'] == ()


def test_media_publication_keeps_sqlite_writer_reservation_until_receipt_commit(db, monkeypatch):
    probe = sqlite3.connect(db.db_path, timeout=0, isolation_level=None)
    publish = db._publish_context_media
    attempts = []
    def under_reservation(root, profile, item):
        # A real second connection attempts the same write reservation while
        # media is being published; no negative wall-clock race is inferred.
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            probe.execute('BEGIN IMMEDIATE')
        attempts.append('reservation_refused')
        return publish(root, profile, item)
    try:
        with monkeypatch.context() as m:
            m.setattr(db, '_publish_context_media', under_reservation)
            receipt = send(db)
        assert attempts == ['reservation_refused']
        assert db.read_context_input('owned', source='desktop', event_id='native-origin') == receipt
        # The later real Stop includes that committed occurrence in its cut.
        control = db.record_context_stop('owned')
        assert control['input_sequence'] == receipt.sequence
        assert db.read_context_input_work('owned')['receipts'] == ()
        assert db.read_context_input_media('owned', receipt) == (png(),)
    finally:
        probe.rollback()
        probe.close()


def test_a_stop_on_another_profile_root_does_not_change_this_origin(db):
    control = db.record_context_stop('foreign')
    assert control['revision'] > 0
    receipt = send(db)
    assert db.read_context_stop('owned') is None
    assert db.read_context_input_origin('owned', receipt)['stop_revision'] == 0
    assert db.read_context_input_work('owned')['receipts'] == (receipt,)


def store_control(owner, control):
    root = owner.read_context_stop('owned')['conversation_root']
    owner._execute_write(lambda conn: conn.execute('UPDATE state_meta SET value=? WHERE key=?',
        (_canonical(control), 'context-control:'+root)))


def test_legacy_cut_refuses_new_media_but_is_not_rewritten(db):
    legacy = db.record_context_stop('owned')
    legacy['schema'] = 'SessionDBContextControlV1'
    legacy.pop('input_sequence')
    store_control(db, legacy)
    with db._read_ctx() as conn:
        # Preserve the existing text owner's deliberate watermark semantics.
        root, _ = db._context_input_scope_on_conn(conn, 'owned')
        assert db._context_input_stop_cut_on_conn(conn, root) is None
    with pytest.raises(ContextContinuationError, match='CONTEXT_INPUT_STOP_UNCONFIRMED'):
        send(db, revision=legacy['revision'])
    assert_no_admission(db)
    assert db.read_context_stop('owned')['control'] == legacy


@pytest.mark.parametrize('fault', ['boolean_revision', 'missing_sequence', 'unknown_schema'])
def test_unconfirmed_stop_control_never_produces_a_media_origin(db, fault):
    malformed = db.record_context_stop('owned')
    if fault == 'boolean_revision':
        malformed['revision'] = True
    elif fault == 'missing_sequence':
        malformed.pop('input_sequence')
    else:
        malformed['schema'] = 'unsupported'
    store_control(db, malformed)
    with pytest.raises(ContextContinuationError, match='CONTEXT_INPUT_STOP_UNCONFIRMED'):
        send(db)
    assert_no_admission(db)
    assert db.read_context_stop('owned')['control'] == malformed
