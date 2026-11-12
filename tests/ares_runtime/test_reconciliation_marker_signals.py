"""Failed marker writes stay observable without adopting an unreconciled child."""

import pytest

from ares_runtime.continuity import runtime
from hermes_state import SessionDB
from tests.ares_runtime.test_continuity_recovery import attempt
from tests.ares_runtime.test_continuity_runtime import setup  # noqa: F401


def test_marker_write_error_is_typed_and_preserves_its_cause():
    write_error = OSError("injected marker persistence failure")

    class FailingMarker:
        def mark_context_rebase_reconciliation_required(self, transition_id):
            raise write_error

    with pytest.raises(runtime.ReconciliationMarkerError) as raised:
        runtime._mark_reconciliation_required(FailingMarker(), "transition-1")

    assert raised.value.__cause__ is write_error
    assert "transition-1" in str(raised.value)


def _fail_marker_during_owner_rebind(setup, monkeypatch):
    db, agent, _, _, _ = setup
    original_engine = agent._transition_context_engine_session
    marker_calls = []

    def unavailable_engine(**kwargs):
        raise RuntimeError("injected engine rebind failure")

    def failed_marker(self, transition_id):
        marker_calls.append(transition_id)
        raise OSError("injected marker persistence failure")

    agent._transition_context_engine_session = unavailable_engine
    with monkeypatch.context() as patch:
        # The helper intentionally resolves the method on the actual owner type;
        # patching only an instance would not exercise the write-failure path.
        patch.setattr(type(db), "mark_context_rebase_reconciliation_required", failed_marker)
        result = attempt(setup)

    assert marker_calls == [result.transition_id]
    assert result.status is runtime.AutomaticRebaseStatus.RECONCILIATION_REQUIRED
    assert result.reason == "CONTEXT_ENGINE_REBIND_FAILED;RECONCILIATION_MARKER_NOT_DURABLE"
    return result, original_engine


def test_marker_failure_keeps_the_committed_child_unadopted(setup, monkeypatch):
    db, agent, _, _, engine_transitions = setup
    original_prompt = agent._cached_system_prompt
    published_contexts = []
    monkeypatch.setattr(runtime, "_publish_runtime_session_context", lambda *args: published_contexts.append(args))

    result, _ = _fail_marker_during_owner_rebind(setup, monkeypatch)

    assert not result.ready
    assert agent.session_id == "s0"
    assert agent._cached_system_prompt == original_prompt
    assert engine_transitions == []
    assert published_contexts == []
    # Publication is durable; do not pretend that failure reopened the parent.
    assert db.get_session("s0")["end_reason"] == "context_rebase"
    with SessionDB(db_path=db.db_path) as reopened:
        transition = reopened.read_context_rebase_transition(result.transition_id)
        assert transition.state == "committed_pending_activation"
        assert transition.child_session_id == result.session_id
        assert reopened.get_context_continuation_tip("s0") == result.session_id


def test_marker_can_be_written_after_failure_and_same_child_recovers(setup, monkeypatch):
    db, agent, _, _, _ = setup
    result, original_engine = _fail_marker_during_owner_rebind(setup, monkeypatch)
    original_message_count = db.message_count(result.session_id)

    try:
        with SessionDB(db_path=db.db_path) as reopened:
            agent._session_db = reopened
            runtime._mark_reconciliation_required(reopened, result.transition_id)
            marked = reopened.read_context_rebase_transition(result.transition_id)
            assert marked.state == "reconciliation_required"
            assert marked.child_session_id == result.session_id
            # Prove the successful marker is visible through another real read
            # connection, not only a returned object or a mock's call counter.
            assert db.read_context_rebase_transition(result.transition_id) == marked

            agent._transition_context_engine_session = original_engine
            recovered = runtime.reconcile_context_rebase(agent)
            assert recovered.ready, recovered.reason
            assert recovered.transition_id == result.transition_id
            assert recovered.session_id == result.session_id
            assert agent.session_id == result.session_id
            assert reopened.read_context_rebase_transition(result.transition_id).state == "ready"
            assert reopened.message_count(result.session_id) == original_message_count
            assert reopened.get_context_continuation_tip("s0") == result.session_id
    finally:
        agent._session_db = db
