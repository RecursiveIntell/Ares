"""Old-release reader accepts only a proven, never-dispatched V2 cut."""
import hashlib
import json
import time

import pytest

from hermes_state import SessionDB
from hermes_state_continuity import ContextContinuationError, _canonical


def key(root):
    return 'context-input-turn:' + hashlib.sha256(root.encode()).hexdigest()


def digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


@pytest.fixture
def fixture_db(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('s', source='cli')
    db.append_message('s', 'user', 'Original', timestamp=1.0)
    assert db.try_acquire_session_turn_lease('s', 'holder', ttl_seconds=300)
    receipt = db.accept_context_input('s', source='cli', event_id='A', content='first')
    before = db.begin_context_input_turn('s', receipt=receipt, turn_lease_holder='holder')
    assert before['schema'] == 'SessionDBContextInputTurnV1'
    control = db.record_context_stop('s')
    assert control['schema'] == 'SessionDBContextControlV2'
    proof_key = f'{key("s")}:phase:{before["phase_id"]}:cancelled'
    phase = db.read_context_input_work('s')['phase']
    if phase['schema'] == 'SessionDBContextInputTurnV1':
        # The prior reader has no cancellation writer; stage the exact V2
        # fixture it must accept. The current writer already commits this proof.
        recorded_at = time.time()
        proof_digest = digest({'before': before, 'control': control, 'recorded_at': recorded_at})
        cancelled = dict(before, schema='SessionDBContextInputTurnV2', state='cancelled',
                         stop_disposition_digest=proof_digest)
        proof = {'schema': 'SessionDBContextInputStopDispositionV1', 'conversation_root': 's',
                 'profile_name': 'default', 'phase_id': before['phase_id'], 'before': before,
                 'after': cancelled, 'control': control, 'recorded_at': recorded_at}
        def stage(conn):
            conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (proof_key, _canonical(proof)))
            conn.execute('UPDATE state_meta SET value=? WHERE key=?', (_canonical(cancelled), key('s')))
        db._execute_write(stage)
    else:
        assert phase['schema'] == 'SessionDBContextInputTurnV2' and phase['state'] == 'cancelled'
        raw_proof = db.get_meta(proof_key)
        assert raw_proof is not None
        proof = json.loads(raw_proof)
        assert proof['before'] == before and proof['after'] == phase and proof['control'] == control
    yield db, proof_key
    db.close()


def mutate(db, target, change):
    def edit(conn):
        value = json.loads(conn.execute('SELECT value FROM state_meta WHERE key=?', (target,)).fetchone()[0])
        change(value)
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(value), target))
    db._execute_write(edit)


def test_proven_cancelled_phase_is_readable_without_replaying_cut_receipt(fixture_db):
    db, _ = fixture_db
    work = db.read_context_input_work('s')
    assert work['phase']['state'] == 'cancelled'
    assert work['phase']['schema'] == 'SessionDBContextInputTurnV2'
    assert work['receipts'] == ()


def test_later_stop_revision_preserves_cancelled_cut(fixture_db):
    db, _ = fixture_db
    prior = db.read_context_input_work('s')['phase']
    updated = db.record_context_stop('s')
    assert updated['revision'] > 1
    work = db.read_context_input_work('s')
    assert work['phase'] == prior
    assert work['receipts'] == ()


def test_post_cut_input_starts_new_phase_without_replaying_stopped_receipt(fixture_db):
    db, _ = fixture_db
    old = db.read_context_input_work('s')['phase']
    fresh_receipt = db.accept_context_input('s', source='cli', event_id='C', content='second')
    wake = db.reserve_context_input_wake('s', receipt=fresh_receipt)
    assert wake['attempts'] == 1
    fresh = db.begin_context_input_turn('s', receipt=fresh_receipt, turn_lease_holder='holder')
    assert fresh['phase_id'] != old['phase_id']
    assert fresh['first_sequence'] == fresh_receipt.sequence
    assert fresh['last_sequence'] == fresh_receipt.sequence


@pytest.mark.parametrize('target,change', [
    ('phase', lambda v: v.update(stop_disposition_digest='0' * 64)),
    ('phase', lambda v: v.update(dispatch_attempts=['attempt'])),
    ('phase', lambda v: v.update(state='uncertain')),
    ('phase', lambda v: v.update(schema='SessionDBContextInputTurnV3')),
    ('proof', lambda v: v['before'].update(dispatch_attempts=['attempt'])),
    ('proof', lambda v: v['before'].update(schema='SessionDBContextInputTurnV2')),
    ('proof', lambda v: v['control'].update(input_sequence=0)),
    ('control', lambda v: v.update(input_sequence=0)),
    ('control', lambda v: v.update(revision=v['revision'] + 1, input_sequence=0)),
])
def test_tamper_and_unknown_state_refuse(fixture_db, target, change):
    db, proof_key = fixture_db
    row = {'phase': key('s'), 'proof': proof_key, 'control': 'context-control:s'}[target]
    mutate(db, row, change)
    with pytest.raises(ContextContinuationError):
        db.read_context_input_work('s')
