"""The persist-override must not rewrite a receipt-authenticated row.

Observed chain (lineage_integrity_mismatch wedging every later compaction):

1. A model-switch marker (role=user) sits in the live history and the next
   user turn arrives. Pre-request alternation repair merges the user text into
   the marker dict in place.
2. Preflight compaction signs and durably commits the merged row as the last
   message of the authenticated prefix; the current-turn anchor falls back to
   that row.
3. At turn end ``_apply_persist_user_message_override`` restored the clean
   user text into that dict -- in memory only. The next recursive compaction
   saw a prefix that no longer matched its parent receipt.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from agent.agent_runtime_helpers import repair_message_sequence

MARKER = (
    "[System: The active model for this chat has changed to m via provider p. "
    "From this point forward, use this runtime metadata when answering "
    "questions about what model/provider is active.]"
)


class _Engine:
    def __init__(self, length):
        self.length = length
        self.calls = []

    def authenticated_prefix_length(self, session_id):
        self.calls.append(session_id)
        if isinstance(self.length, Exception):
            raise self.length
        return self.length


def _agent(engine):
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent.context_compressor = engine
    agent.session_id = "sess-auth-prefix"
    return agent


def _merged_history():
    history = [
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "Operation interrupted."},
        {"role": "user", "content": MARKER, "display_kind": "model_switch"},
        {"role": "user", "content": "finish everything here"},
    ]
    assert repair_message_sequence(None, history) == 1
    assert len(history) == 3
    return history


def _arm(agent, idx, override, timestamp=None):
    agent._persist_user_message_idx = idx
    agent._persist_user_message_override = override
    agent._persist_user_message_timestamp = timestamp


def test_authenticated_merged_row_is_not_rewritten():
    history = _merged_history()
    committed = history[2]["content"]
    engine = _Engine(len(history))  # compaction committed all three rows
    agent = _agent(engine)
    _arm(agent, 2, "finish everything here", timestamp=1730000000)

    agent._apply_persist_user_message_override(history)

    assert history[2]["content"] == committed
    assert history[2]["timestamp"] == 1730000000
    assert engine.calls == ["sess-auth-prefix"]


def test_row_after_authenticated_prefix_is_still_cleaned():
    history = _merged_history()
    agent = _agent(_Engine(2))
    _arm(agent, 2, "finish everything here")

    agent._apply_persist_user_message_override(history)

    assert history[2]["content"] == "finish everything here"


def test_resolver_error_fails_open_to_previous_behavior():
    history = _merged_history()
    agent = _agent(_Engine(RuntimeError("store unavailable")))
    _arm(agent, 2, "finish everything here")

    agent._apply_persist_user_message_override(history)

    assert history[2]["content"] == "finish everything here"


def test_non_int_resolver_result_fails_open():
    history = _merged_history()
    agent = _agent(MagicMock())
    _arm(agent, 2, "finish everything here")

    agent._apply_persist_user_message_override(history)

    assert history[2]["content"] == "finish everything here"


def test_no_lookup_when_override_is_already_equal():
    history = [{"role": "user", "content": "same"}]
    engine = _Engine(1)
    agent = _agent(engine)
    _arm(agent, 0, "same")

    agent._apply_persist_user_message_override(history)

    assert history[0]["content"] == "same"
    assert engine.calls == []
