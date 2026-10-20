"""Model-facing restores must keep the owner-marked summary discriminator.

``SessionDB.get_messages_as_conversation`` omits the durable
``_compressed_summary`` flag unless the caller opts in. Context Governor maps
that flag into the authenticated receipt projection, so any restore that feeds
the model without it makes the next compaction differ from its signed parent
and be refused with ``lineage_integrity_mismatch``. The compute-host cold
replay was fixed first; these tests pin the sibling restore owners (gateway,
ACP, TUI resume, context-input reloads, run-loop lease reload) to the same
contract, and keep user-authored lookalike text from gaining summary
authority.
"""
from __future__ import annotations

import types

import pytest

from hermes_state import SessionDB

SUMMARY = "Authenticated prior summary."
LOOKALIKE = "This literal text says _compressed_summary=true."


def _seed(db: SessionDB, session_id: str, *, source: str = "tui") -> None:
    db.create_session(session_id, source=source)
    db.append_message(session_id, "user", "Original active task.")
    db.append_message(session_id, "assistant", SUMMARY, _compressed_summary=True)
    db.append_message(session_id, "user", LOOKALIKE)
    db.append_message(session_id, "assistant", "Latest reply.")


def _assert_marker_contract(history) -> None:
    flagged = [m for m in history if m.get("_compressed_summary") is True]
    assert [m["content"] for m in flagged] == [SUMMARY]
    lookalikes = [m for m in history if m.get("content") == LOOKALIKE]
    assert lookalikes and not any(m.get("_compressed_summary") for m in lookalikes)


@pytest.fixture
def db(tmp_path):
    database = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield database
    finally:
        database.close()


def test_inspection_projection_stays_unmarked(db):
    _seed(db, "plain")
    assert not any(m.get("_compressed_summary") for m in db.get_messages_as_conversation("plain"))


def test_gateway_load_transcript_keeps_summary_marker(db, tmp_path):
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore

    _seed(db, "gw-session", source="telegram")
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    store._db = db
    _assert_marker_contract(store.load_transcript("gw-session"))


def test_acp_restore_keeps_summary_marker(db):
    from unittest.mock import MagicMock

    from acp_adapter.session import SessionManager

    _seed(db, "acp-session", source="acp")
    manager = SessionManager(agent_factory=lambda: MagicMock(name="agent"), db=db)
    state = manager.get_session("acp-session")
    assert state is not None
    _assert_marker_contract(state.history)


def _tui_resume(monkeypatch, db, params):
    from tui_gateway import server

    captured = {}
    lease = types.SimpleNamespace(session_id=params["session_id"], release=lambda: None)

    def _record(*args, **kwargs):
        captured["history"] = kwargs.get("history")
        return {"created_at": 1.0}

    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_claim_active_session_slot", lambda *a, **k: (lease, None))
    monkeypatch.setattr(server, "_deferred_session_record", _record)
    monkeypatch.setattr(server, "_claim_or_reuse_live", lambda *a, **k: None)
    monkeypatch.setattr(server, "_child_run_active", lambda *a, **k: False)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda *a, **k: None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a, **k: None)
    resp = server.handle_request({"id": "1", "method": "session.resume", "params": params})
    assert "error" not in resp, resp
    return captured["history"]


def test_tui_lazy_watch_resume_keeps_summary_marker(monkeypatch, db):
    _seed(db, "watch-session")
    history = _tui_resume(monkeypatch, db, {"session_id": "watch-session", "lazy": True})
    _assert_marker_contract(history)


def test_tui_resume_without_messages_keeps_summary_marker(monkeypatch, db):
    _seed(db, "omit-session")
    history = _tui_resume(
        monkeypatch, db, {"session_id": "omit-session", "omit_messages": True}
    )
    _assert_marker_contract(history)


def test_context_input_publication_reload_keeps_summary_marker(db):
    from agent import context_input

    _seed(db, "input-session")

    class _DB:
        def __getattr__(self, name):
            return getattr(db, name)

        def read_context_input_work(self, _sid):
            return {"receipts": []}

        def confirm_context_input_response(self, *_a, **_k):
            return {"confirmed": True}

    agent = types.SimpleNamespace(
        session_id="input-session",
        _session_db=_DB(),
        _context_input_phase={"phase_id": "p1"},
        _context_input_authorizer=None,
        _context_response_admission=None,
        _active_session_turn_lease_holder="holder",
    )
    messages = [{"role": "assistant", "content": "Latest reply."}]  # no _row_id
    context_input.complete_turn_inputs(agent, messages)
    _assert_marker_contract(messages)


class _RestoreObserved(BaseException):
    """Stop before unrelated agent/provider construction, after the real DB read."""


def test_cli_init_resume_keeps_summary_marker(monkeypatch, db):
    import cli
    from hermes_cli import mcp_startup
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin

    _seed(db, "cli-session", source="cli")
    noop = lambda *_a, **_k: None
    monkeypatch.setattr(cli, "_prepare_deferred_agent_startup", noop)
    monkeypatch.setattr(mcp_startup, "ensure_mcp_discovery_before_agent_build", noop)

    def after_restore(*_a, **_k):
        raise _RestoreObserved

    cli_state = types.SimpleNamespace(
        agent=None, _session_db=db, session_id="cli-session", _resumed=True,
        conversation_history=[], tool_progress_mode="off", finalize_preloaded_skills=noop,
        _install_tool_callbacks=noop, _ensure_tirith_security=noop,
        _ensure_runtime_credentials=lambda: True,
        _resume_history_limit_error=lambda **_k: None,
        _restore_session_cwd=after_restore,
    )
    with pytest.raises(_RestoreObserved):
        CLIAgentSetupMixin._init_agent(cli_state)
    _assert_marker_contract(cli_state.conversation_history)


@pytest.mark.parametrize("existing_projection", [False, True])
def test_context_input_projection_reloads_keep_summary_marker(monkeypatch, db, existing_projection):
    from agent.context_input import project_turn_inputs

    _seed(db, "project-session")
    tail_id = db.get_messages_as_conversation("project-session", include_row_ids=True)[-1]["_row_id"]
    # The merged tree fails closed on any receipt whose content is not a str
    # (CONTEXT_INPUT_TEXT_ROUTE_REQUIRED), before phase creation or projection.
    # The fixture provides that shape so the projection itself is what runs.
    receipt = types.SimpleNamespace(sequence=1, content="Original active task.")
    # Native input admission is not under test here; its result selects the
    # two reload branches. The transcript projection itself is real SessionDB.
    monkeypatch.setattr(db, "read_context_input_work", lambda _s: {"receipts": [receipt]})
    monkeypatch.setattr(db, "read_pending_context_inputs", lambda _s: ())
    monkeypatch.setattr(db, "read_context_input_origin", lambda *_a: None)
    monkeypatch.setattr(db, "begin_context_input_turn", lambda *_a, **_k: {"phase_id": "p"})
    monkeypatch.setattr(db, "project_context_inputs_before", lambda *_a, **_k: {
        "projection": {"row_id": tail_id} if existing_projection else None,
        "inserted": not existing_projection,
    })
    agent = types.SimpleNamespace(session_id="project-session", _session_db=db)
    history = project_turn_inputs(agent, receipt, holder="holder", conversation_history=[])
    _assert_marker_contract(history)
    if existing_projection:
        assert agent._context_input_existing_message["_row_id"] == tail_id
        assert all(m.get("_row_id") != tail_id for m in history)


def test_tui_eager_omit_resume_keeps_summary_marker(monkeypatch, db):
    from tui_gateway import server

    _seed(db, "eager-session")
    captured = []
    load = db.get_messages_as_conversation

    def observe_restore(*args, **kwargs):
        captured.extend(load(*args, **kwargs))
        raise _RestoreObserved

    monkeypatch.setattr(db, "get_messages_as_conversation", observe_restore)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    with pytest.raises(_RestoreObserved):
        server.handle_request({"id": "eager", "method": "session.resume", "params": {
            "session_id": "eager-session", "omit_messages": True, "eager_build": True,
        }})
    _assert_marker_contract(captured)
