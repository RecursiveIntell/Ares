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

import ast
import types
from pathlib import Path

import pytest

from hermes_state import SessionDB

REPO = Path(__file__).resolve().parents[2]
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


# Restore owners whose result becomes the model-facing working conversation.
# Display, export, rewind-comparison and trace readers are deliberately absent:
# they keep the verbatim inspection projection.
_MODEL_FACING_RESTORES = {
    ("acp_adapter/session.py", "_restore"),
    ("agent/context_input.py", "project_turn_inputs"),
    ("agent/context_input.py", "complete_turn_inputs"),
    ("gateway/session.py", "load_transcript"),
    ("hermes_cli/cli_agent_setup_mixin.py", "_init_agent"),
    ("run_agent.py", "run_conversation"),
    ("tui_gateway/compute_host.py", "_ensure_server_session"),
}


def _restore_calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = []
    stack = []

    class _Visitor(ast.NodeVisitor):
        def _fn(self, node):
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        visit_FunctionDef = visit_AsyncFunctionDef = _fn

        def visit_Call(self, node):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "get_messages_as_conversation":
                kwargs = {
                    k.arg: (k.value.value if isinstance(k.value, ast.Constant) else None)
                    for k in node.keywords
                    if k.arg
                }
                calls.append((tuple(stack), node.lineno, kwargs))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return calls


def _source_files():
    for path in sorted(REPO.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith(("tests/", "website/", ".venv/", "node_modules/")) or "/node_modules/" in rel:
            continue
        if rel == "hermes_state.py":
            continue
        yield rel, path


def test_model_facing_restores_request_summary_markers():
    """Live-replay reads (repair_alternation=True) and known owners must opt in.

    A repaired read is by contract a LIVE REPLAY restore (see
    ``get_messages_as_conversation``), so it is model-facing wherever it lives;
    the explicit owner list covers unrepaired model-facing reloads.
    """
    missing = []
    for rel, path in _source_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "get_messages_as_conversation" not in text:
            continue
        for scope, lineno, kwargs in _restore_calls(path):
            owner = any((rel, name) in _MODEL_FACING_RESTORES for name in scope)
            live_replay = kwargs.get("repair_alternation") is True
            if (owner or live_replay) and kwargs.get("include_summary_markers") is not True:
                missing.append(f"{rel}:{lineno} {'.'.join(scope)}")
    assert not missing, "model-facing restores omit summary markers:\n" + "\n".join(missing)
