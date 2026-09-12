"""Tests for the bounded cross-process session turn-lease wait."""

from __future__ import annotations

import run_agent


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
