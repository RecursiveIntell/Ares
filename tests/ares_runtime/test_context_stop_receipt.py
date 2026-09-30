"""Durable Stop evidence comes from the canonical SessionDB control row."""
from hermes_state import SessionDB
from tests.ares_runtime.test_continuity_input import db, accept  # noqa: F401
from tests.ares_runtime.test_continuity_dispatch import durable_agent  # noqa: F401
from tests.run_agent.test_run_agent import agent  # noqa: F401


def test_stop_readback_survives_reopen_without_another_write(db):
    receipt = accept(db)
    assert db.read_context_stop("s") is None
    control = db.record_context_stop("s")
    other = SessionDB(db.db_path)
    try:
        assert other.read_context_stop("s") == {
            "conversation_root": receipt.conversation_root, "control": control}
        assert other.read_context_stop("s") == db.read_context_stop("s")
    finally:
        other.close()


def test_hard_interrupt_exposes_only_committed_stop_receipt(durable_agent):
    agent, db = durable_agent
    agent.hard_interrupt()
    receipt = agent._context_stop_receipt
    assert receipt["session_key"] == agent.session_id
    assert receipt["control"] == db.read_context_stop(agent.session_id)["control"]
    assert agent._context_stop_unacknowledged is False


def test_failed_stop_write_cannot_reuse_earlier_receipt(durable_agent, monkeypatch):
    agent, db = durable_agent
    agent.hard_interrupt()
    assert agent._context_stop_receipt
    def fail(*args):
        raise OSError("stop persistence failed")
    monkeypatch.setattr(db, "record_context_stop", fail)
    agent.hard_interrupt()
    assert agent._context_stop_receipt is None
    assert agent._context_stop_unacknowledged is True
