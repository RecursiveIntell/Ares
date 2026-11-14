"""Operator session controls must persist only policy, never main-model defaults."""
import importlib
import json

import pytest

from hermes_state import SessionDB


@pytest.fixture
def state(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("exact-session", "cli", model="main-unchanged", model_config={"provider": "main-provider"})
    try:
        yield db
    finally:
        db.close()


def control(db, text):
    api = importlib.import_module("hermes_cli.orchestration")
    return api.apply_operator_control(db, "exact-session", text)


def test_off_and_prefer_are_durable_and_do_not_change_main_route(state):
    before = state.get_session("exact-session")
    off = control(state, "off")
    assert off["policy"]["allow_delegation"] is False
    assert off["policy"]["allow_graph"] is False
    preferred = control(state, "prefer")
    assert preferred["revision"] == off["revision"] + 1
    assert preferred["policy"]["allow_delegation"] is True
    assert state.get_session("exact-session")["model"] == before["model"]
    assert state.get_session("exact-session")["model_config"] == before["model_config"]


def test_secondary_selection_does_not_reenable_a_disabled_lane(state):
    control(state, "off")
    selected = control(state, "secondary openai-codex exact-secondary low")
    assert selected["policy"]["secondary_route"]["model"] == "exact-secondary"
    assert selected["policy"]["secondary_route"]["reasoning_effort"] == "low"
    assert selected["policy"]["allow_delegation"] is False
    assert selected["policy"]["allow_graph"] is False


def test_show_is_read_only_and_clear_secondary_preserves_permissions(state):
    selected = control(state, "secondary provider exact:model high")
    assert control(state, "show") == selected
    cleared = control(state, "inherit-secondary")
    assert cleared["policy"]["secondary_route"] is None
    assert cleared["policy"]["allow_delegation"] is True


@pytest.mark.parametrize("text", ["secondary provider", "secondary p m unsupported", "off extra", "unknown", "secondary ' bad-provider' model"])
def test_bad_control_never_creates_a_record(state, text):
    with pytest.raises(ValueError):
        control(state, text)
    assert state.get_orchestration_policy("exact-session") is None


def test_actual_cli_slash_dispatch_updates_only_current_session(state):
    from types import SimpleNamespace
    from unittest.mock import Mock, patch
    from cli import HermesCLI
    host = SimpleNamespace(_session_db=state, session_id="exact-session", model="main-unchanged",
                           _console_print=Mock(), _quick_commands={}, config={})
    with patch("cli._cprint"):
        HermesCLI.process_command(host, "/orchestration off")
    record = state.get_orchestration_policy("exact-session")
    assert record is not None
    assert record["policy"]["allow_delegation"] is False
    assert state.get_session("exact-session")["model"] == "main-unchanged"


def test_cli_parser_does_not_reuse_main_model_flags():
    import argparse
    api = importlib.import_module("hermes_cli.orchestration")
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="command")
    api.register_cli(sub)
    args = root.parse_args(["orchestration", "--session-id", "exact-session", "secondary", "--provider", "provider", "--model", "exact-model", "--reasoning", "low"])
    assert args.secondary_model == "exact-model"
    assert args.secondary_provider == "provider"
    assert not hasattr(args, "model")
