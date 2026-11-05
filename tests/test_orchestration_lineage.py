"""Real owner-transaction witnesses for fork inheritance and effect admission."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from agent.orchestration_policy import OrchestrationError, OrchestrationPolicy, SecondaryRoute
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    owner = SessionDB(tmp_path / 'state.db')
    owner.create_session('parent', 'cli')
    yield owner
    owner.close()


def seed(db):
    policy = OrchestrationPolicy(allow_graph=False,
        secondary_route=SecondaryRoute('openai-codex', 'exact-worker', 'low')).to_dict()
    return db.set_orchestration_policy('parent', policy, expected_revision=0, origin='operator-command')


def test_fork_and_compression_copy_policy_without_widening_and_keep_provenance(db):
    original = seed(db)
    db.create_session('branch', 'cli', parent_session_id='parent')
    record = db.get_orchestration_policy('branch')
    assert record['policy'] == original['policy']
    assert record['origin'] == 'inherit:parent:1'
    db.create_session('compressed', 'cli', parent_session_id='branch')
    assert db.get_orchestration_policy('compressed')['policy'] == original['policy']
    db.create_session('fresh', 'cli')
    assert db.get_orchestration_policy('fresh') is None
    # Repeated row publication cannot clobber a deliberate child update.
    new = OrchestrationPolicy(allow_delegation=False, allow_graph=False).to_dict()
    db.set_orchestration_policy('branch', new, expected_revision=1, origin='operator-command')
    db.create_session('branch', 'cli', parent_session_id='parent')
    assert db.get_orchestration_policy('branch')['policy'] == new


def test_unqualified_legacy_parent_cannot_publish_descendant(db):
    seed(db)
    db.create_session('legacy-child', 'cli', parent_session_id='parent')
    with sqlite3.connect(db.db_path) as conn:
        conn.execute("DELETE FROM session_orchestration_policy WHERE session_id='legacy-child'")
    with pytest.raises(OrchestrationError, match='orchestration_lineage_unqualified'):
        db.create_session('grandchild', 'cli', parent_session_id='legacy-child')
    assert db.get_session('grandchild') is None
    assert db.get_orchestration_policy('legacy-child') is None


def test_corrupt_parent_cannot_publish_permissive_child(db):
    seed(db)
    with sqlite3.connect(db.db_path) as c:
        c.execute("UPDATE session_orchestration_policy SET policy_json='[]' WHERE session_id='parent'")
    with pytest.raises(OrchestrationError, match='policy_corrupt'):
        db.create_session('branch', 'cli', parent_session_id='parent')
    assert db.get_session('branch') is None


def test_effect_claim_is_durable_distinct_and_rejects_stale_policy(db):
    seed(db)
    first = db.claim_orchestration_effect('parent', expected_revision=1, effect='delegation',
        route={'provider':'openai-codex','model':'exact-worker','reasoning_effort':'low'},
        config_fingerprint='test-config')
    second = db.claim_orchestration_effect('parent', expected_revision=1, effect='delegation',
        route={'provider':'openai-codex','model':'exact-worker','reasoning_effort':'low'},
        config_fingerprint='test-config')
    assert first['attempt_id'] != second['attempt_id']
    assert first['status'] == 'admitted'
    db.set_orchestration_policy('parent', OrchestrationPolicy(allow_delegation=False).to_dict(),
        expected_revision=1, origin='operator-command')
    with pytest.raises(OrchestrationError, match='orchestration_revision_changed'):
        db.claim_orchestration_effect('parent', expected_revision=1, effect='delegation',
            route={}, config_fingerprint='test-config')
    with sqlite3.connect(db.db_path) as c:
        assert c.execute('SELECT count(*) FROM session_orchestration_attempts').fetchone()[0] == 2


def test_claim_and_optout_have_one_sqlite_linearization_order(db):
    seed(db)
    peer = SessionDB(db.db_path)
    barrier = Barrier(2)
    def claim():
        barrier.wait(timeout=5)
        try:
            return peer.claim_orchestration_effect('parent', expected_revision=1, effect='delegation',
                route={}, config_fingerprint='test')['status']
        except OrchestrationError as e:
            return e.code
    def deny():
        barrier.wait(timeout=5)
        db.set_orchestration_policy('parent', OrchestrationPolicy(allow_delegation=False).to_dict(),
            expected_revision=1, origin='operator-command')
    try:
        with ThreadPoolExecutor(2) as pool:
            a, b = pool.submit(claim), pool.submit(deny)
            outcome = a.result(timeout=10)
            b.result(timeout=10)
        assert outcome in {'admitted', 'orchestration_revision_changed'}
        with pytest.raises(OrchestrationError):
            peer.claim_orchestration_effect('parent', expected_revision=1, effect='delegation',
                route={}, config_fingerprint='test')
    finally:
        peer.close()
