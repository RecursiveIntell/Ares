"""Tests for the bounded cross-process session turn-lease wait."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import run_agent
from hermes_state import SessionDB


def test_session_turn_lease_wait_defaults_to_thirty_seconds(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})

    assert run_agent._resolved_session_turn_lease_wait_seconds() == 30.0


def test_session_turn_lease_wait_reads_explicit_value(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"agent": {"session_turn_lease_wait_seconds": 7.5}},
    )

    assert run_agent._resolved_session_turn_lease_wait_seconds() == 7.5


def test_session_turn_lease_wait_allows_no_wait(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"agent": {"session_turn_lease_wait_seconds": 0}},
    )

    assert run_agent._resolved_session_turn_lease_wait_seconds() == 0.0


def test_session_turn_lease_wait_rejects_invalid_values(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"agent": {"session_turn_lease_wait_seconds": -1}},
    )

    assert run_agent._resolved_session_turn_lease_wait_seconds() == 30.0


def test_session_turn_lease_wait_is_capped(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"agent": {"session_turn_lease_wait_seconds": 99999}},
    )

    assert run_agent._resolved_session_turn_lease_wait_seconds() == 1800.0


def test_run_conversation_passes_resolved_wait_to_durable_lease(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "lease-integration"
    db.create_session(session_id, source="test")
    captured: list[float] = []
    original_acquire = db.acquire_session_turn_lease

    def capture_acquire(session, holder, *, ttl_seconds=300.0, wait_seconds=0.0, **kwargs):
        captured.append(wait_seconds)
        return original_acquire(
            session,
            holder,
            ttl_seconds=ttl_seconds,
            wait_seconds=wait_seconds,
            **kwargs,
        )

    monkeypatch.setattr(db, "acquire_session_turn_lease", capture_acquire)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"agent": {"session_turn_lease_wait_seconds": 2.5}},
    )
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="done", tool_calls=None),
                finish_reason="stop",
            )
        ],
        model="test",
        usage=None,
    )

    try:
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
        ):
            agent = run_agent.AIAgent(
                api_key="test-key",
                base_url="https://example.invalid",
                provider="test",
                model="test",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                session_db=db,
                session_id=session_id,
            )
        agent._session_db_created = True
        agent.client = MagicMock()
        agent.client.chat.completions.create.return_value = response

        result = agent.run_conversation("hello")

        assert result["completed"] is True
        assert captured == [2.5]
    finally:
        db.close()


def test_no_wait_lease_contention_does_not_execute_waiting_model(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    holder_db = SessionDB(db_path=db_path)
    waiting_db = SessionDB(db_path=db_path)
    session_id = "lease-contention"
    holder = "other-process-turn"
    holder_db.create_session(session_id, source="test")
    assert holder_db.acquire_session_turn_lease(
        session_id,
        holder,
        ttl_seconds=30,
        wait_seconds=0,
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"agent": {"session_turn_lease_wait_seconds": 0}},
    )
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="must-not-run", tool_calls=None),
                finish_reason="stop",
            )
        ],
        model="test",
        usage=None,
    )

    try:
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
        ):
            agent = run_agent.AIAgent(
                api_key="test-key",
                base_url="https://example.invalid",
                provider="test",
                model="test",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                session_db=waiting_db,
                session_id=session_id,
            )
        agent._session_db_created = True
        agent.client = MagicMock()
        agent.client.chat.completions.create.return_value = response

        result = agent.run_conversation("waiting message")

        assert result["completed"] is False
        assert result["error"] == f"session_turn_lease_timeout:{session_id}"
        assert agent.client.chat.completions.create.call_count == 0
    finally:
        holder_db.release_session_turn_lease(session_id, holder)
        holder_db.close()
        waiting_db.close()
