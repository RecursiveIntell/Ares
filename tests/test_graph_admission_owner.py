"""Atomic Graph effect claims must respect the canonical session lifecycle."""
import sqlite3
import pytest
from agent.orchestration_policy import OrchestrationError
from hermes_state import SessionDB

@pytest.fixture
def db(tmp_path):
    owner=SessionDB(tmp_path/'state.db')
    owner.create_session('live-root','cli')
    yield owner
    owner.close()


def claim(db):
    return db.claim_orchestration_effect('live-root',expected_revision=0,effect='graph',
        route={'provider':'openai-codex','model':'fixture','reasoning_effort':'low'},
        config_fingerprint='fixture-config')


def test_graph_claim_denies_ended_session_in_the_owner_transaction(db):
    db.end_session('live-root','test-finished')
    with pytest.raises(OrchestrationError,match='graph_session_not_live'):
        claim(db)
    with sqlite3.connect(db.db_path) as connection:
        assert connection.execute('SELECT count(*) FROM session_orchestration_attempts').fetchone()[0]==0


def test_graph_claim_does_not_bypass_an_existing_unqualified_native_owner(db,monkeypatch):
    # Simulate the canonical owner's participation observation, not a real grant.
    monkeypatch.setattr(db,'_native_context_authority_on_conn',lambda conn,sid:{'fixture':'enrolled'})
    with pytest.raises(OrchestrationError,match='graph_native_context_unqualified'):
        claim(db)
    with sqlite3.connect(db.db_path) as connection:
        assert connection.execute('SELECT count(*) FROM session_orchestration_attempts').fetchone()[0]==0


def test_graph_claim_owner_read_failure_is_not_permission(db,monkeypatch):
    def unavailable(conn,sid):
        raise RuntimeError('fixture authority owner unavailable')
    monkeypatch.setattr(db,'_native_context_authority_on_conn',unavailable)
    with pytest.raises(OrchestrationError,match='graph_admission_owner_unavailable'):
        claim(db)
