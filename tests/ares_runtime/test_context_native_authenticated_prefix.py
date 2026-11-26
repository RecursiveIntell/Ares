"""Real Governor + SessionDB regression for authenticated-row preservation.

Exercises the reported chain against the real ``context-governor`` binary and a
real ``SessionDB`` under a temporary ``HERMES_HOME``:

1. A model-switch marker (role=user) precedes the next user turn; alternation
   repair merges them into one row.
2. Compaction signs the merged row; the host persists the authenticated
   projection to SessionDB and activates the receipt.
3. The turn-end persist override tries to restore the clean user text into
   that authenticated row. It must not.
4. After a reload of both SessionDB and the Governor, a recursive compaction of
   the persisted transcript must activate instead of failing closed with
   ``lineage_integrity_mismatch``.

Requires the exact pinned native binary supplied by the Context continuity
qualification job through ``HERMES_TEST_CONTEXT_GOVERNOR_BIN``; it skips
elsewhere, like ``test_continuity_native_governor.py``.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.agent_runtime_helpers import repair_message_sequence
from hermes_state import SessionDB
from plugins.context_engine._context_governor import ContextGovernorEngine
from plugins.context_engine._context_governor.key_state import ContextGovernorKeyState

MARKER = (
    "[System: The active model for this chat has changed to m via provider p. "
    "From this point forward, use this runtime metadata when answering "
    "questions about what model/provider is active.]"
)
CLEAN_USER_TEXT = "finish everything here"
SESSION_ID = "authenticated-prefix-override-e2e"
CONFIG = {
    "context": {
        "governor": {
            "summary_mode": "llm",
            "checkpoint_strategy": "after_n:999",
            "token_budget": 1024,
            "min_net_savings_tokens": 0,
            "allocator": "deterministic_v1",
            "budget_mode": "hard_cascade",
            "protect_first_n": 0,
            "protect_last_n": 1,
            "telemetry_max_additional_protected_messages": 0,
            "max_lineage_generation": 8,
        }
    }
}


def _binary() -> str:
    binary = os.environ.get("HERMES_TEST_CONTEXT_GOVERNOR_BIN")
    if not binary:
        pytest.skip("exact native binary must be supplied by the paired qualification job")
    assert Path(binary).is_file(), "configured native binary is missing"
    return binary


def _engine(binary, store, session_db) -> ContextGovernorEngine:
    with patch("hermes_cli.config.load_config", return_value=CONFIG):
        engine = ContextGovernorEngine(binary=binary, store_dir=store)
        engine.on_session_start(SESSION_ID, session_db=session_db)
        engine.update_model("fixture", context_length=16_000, provider="openai-codex")
    return engine


def _compact_and_commit(engine, session_db, messages):
    with patch("hermes_cli.config.load_config", return_value=CONFIG):
        compacted = engine.compress(messages, current_tokens=100)
        assert engine._pending_admission is not None, (
            f"outcome={engine.last_outcome!r} error={engine.last_error!r}"
        )
        assert engine.validate_pending_compression(compacted) is True
        session_db.archive_and_compact(SESSION_ID, compacted)
        engine.commit_pending_compression(compacted)
    assert engine.last_error is None
    assert engine._pending_admission is None
    return compacted


def _content_rows(messages):
    return [(m.get("role"), m.get("content")) for m in messages]


def test_authenticated_merged_row_survives_override_reload_and_recompaction(
    tmp_path, monkeypatch
):
    binary = _binary()
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    ContextGovernorKeyState(home, binary).initialize_first_install().close()
    store = home / "context-governor"
    db_path = home / "state.db"
    session_db = SessionDB(db_path=db_path)
    session_db.create_session(SESSION_ID, source="cli")

    live = [
        {"role": "user", "content": "Preserve the root human intent."},
        {"role": "assistant", "content": "initial context " * 300},
        {"role": "user", "content": MARKER, "display_kind": "model_switch"},
        {"role": "user", "content": CLEAN_USER_TEXT},
    ]
    assert repair_message_sequence(None, live) == 1
    merged_idx = len(live) - 1
    assert MARKER in live[merged_idx]["content"]
    assert CLEAN_USER_TEXT in live[merged_idx]["content"]

    engine = _engine(binary, store, session_db)
    compacted = _compact_and_commit(engine, session_db, live)
    merged_rows = [
        i for i, m in enumerate(compacted)
        if isinstance(m.get("content"), str) and MARKER in m["content"]
    ]
    assert merged_rows, "the merged marker row must be part of the signed projection"
    locked = engine.authenticated_prefix_length(SESSION_ID)
    assert locked > merged_rows[-1], "merged row must lie inside the authenticated prefix"

    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent.context_compressor = engine
    agent.session_id = SESSION_ID
    agent._persist_user_message_idx = merged_rows[-1]
    agent._persist_user_message_override = CLEAN_USER_TEXT
    agent._persist_user_message_timestamp = 1730000000
    signed = copy.deepcopy(compacted)
    agent._apply_persist_user_message_override(compacted)
    assert compacted == signed, "persist override rewrote an authenticated row"

    # Reload both durable owners exactly as the gateway/CLI resume paths do,
    # then compact the persisted transcript again.
    session_db.close()
    session_db = SessionDB(db_path=db_path)
    persisted = session_db.get_messages_as_conversation(
        SESSION_ID, repair_alternation=True, include_summary_markers=True
    )
    assert _content_rows(persisted)[: len(signed)] == _content_rows(signed)

    restarted = _engine(binary, store, session_db)
    next_turn = persisted + [
        {"role": "assistant", "content": "new context " * 300},
        {"role": "user", "content": "continue the verified task"},
    ]
    _compact_and_commit(restarted, session_db, next_turn)
    assert restarted.compression_count >= 1
    assert not list((store / ".pending").glob("*.json"))
    session_db.close()
