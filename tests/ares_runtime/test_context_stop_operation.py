"""Operation receipts use the real SQLite owner and preserve the V2 cut."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from hermes_state import SessionDB
from hermes_state_continuity import ContextContinuationError, _valid_context_stop_v2
from tests.ares_runtime.test_continuity_input import db, accept  # noqa: F401


def test_replay_preserves_exact_cut_and_post_cut_input(db):
    accept(db)
    first = db.record_context_stop_operation('s', operation_id='op')
    after = accept(db, 'after')
    replay = db.record_context_stop_operation('s', operation_id='op')
    assert first['disposition'] == 'applied'
    assert replay == dict(first, disposition='already_committed')
    assert db.read_context_stop_operation('s', operation_id='op') == first['receipt']
    assert db.read_context_stop('s')['control'] == first['receipt']['control']
    assert after.sequence > first['receipt']['control']['input_sequence']
    assert _valid_context_stop_v2(first['receipt']['control'])
    second = db.record_context_stop_operation('s', operation_id='new')
    assert second['receipt']['control']['input_sequence'] == after.sequence
    assert second['receipt']['control']['revision'] == first['receipt']['control']['revision'] + 1
    assert db.read_context_stop_operation('s', operation_id='op') == first['receipt']
    assert db.read_context_stop('s')['control'] != first['receipt']['control']


def test_cross_root_collision_and_legacy_shape(db):
    db.create_session('other', source='cli')
    first = db.record_context_stop_operation('s', operation_id='op')
    with pytest.raises(ContextContinuationError, match='COLLISION'):
        db.record_context_stop_operation('other', operation_id='op')
    with pytest.raises(ContextContinuationError, match='COLLISION'):
        db.read_context_stop_operation('other', operation_id='op')
    assert db.read_context_stop('other') is None
    legacy = db.record_context_stop('s')
    assert _valid_context_stop_v2(legacy)
    assert set(legacy) == set(first['receipt']['control'])


def test_actual_after_commit_exception_recovers_original(db, monkeypatch):
    original = db._execute_write
    def commit_then_throw(fn):
        original(fn)
        raise OSError('private storage detail')
    monkeypatch.setattr(db, '_execute_write', commit_then_throw)
    with pytest.raises(OSError):
        db.record_context_stop_operation('s', operation_id='op')
    receipt = db.read_context_stop_operation('s', operation_id='op')
    monkeypatch.setattr(db, '_execute_write', original)
    accept(db, 'after')
    assert db.record_context_stop_operation('s', operation_id='op') == {
        'disposition': 'already_committed', 'receipt': receipt}
    assert db.read_context_stop('s')['control'] == receipt['control']


def test_concurrent_same_operation_has_one_winner(db):
    other = SessionDB(db.db_path)
    barrier = threading.Barrier(2)
    def stop(owner):
        barrier.wait(timeout=3)
        return owner.record_context_stop_operation('s', operation_id='race')
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(stop, [db, other]))
        assert sorted(r['disposition'] for r in results) == ['already_committed', 'applied']
        assert results[0]['receipt'] == results[1]['receipt']
        assert db.read_context_stop('s')['control']['revision'] == 1
    finally:
        other.close()


@pytest.mark.parametrize('iteration', range(5))
def test_retrospective_replay_racing_new_cut_never_reapplies_old_cut(db, iteration):
    """Additional race control after the owner RED/GREEN, not a claimed RED."""
    first = db.record_context_stop_operation('s', operation_id='original')
    post = accept(db, 'post')
    other = SessionDB(db.db_path)
    barrier = threading.Barrier(2)
    def stop(pair):
        owner, operation = pair
        barrier.wait(timeout=3)
        return owner.record_context_stop_operation('s', operation_id=operation)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            old, new = list(pool.map(stop, [(db, 'original'), (other, 'new')]))
        assert old == dict(first, disposition='already_committed')
        assert new['disposition'] == 'applied'
        assert new['receipt']['control']['revision'] == 2
        assert new['receipt']['control']['input_sequence'] == post.sequence
        assert db.read_context_stop('s')['control'] == new['receipt']['control']
    finally:
        other.close()
