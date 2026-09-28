"""Exhausted preflight must not send an unchanged over-window request."""
from unittest.mock import patch

import pytest

from ares_runtime.continuity.runtime import ContextDispatchError
from tests.run_agent.test_preflight_compression_cap_e2e import _make_agent, _stop_response


@pytest.mark.parametrize("tokens,blocked", [(200_000, True), (75_000, False)])
def test_no_progress_compaction_requires_safe_admission(monkeypatch, tmp_path, tokens, blocked):
    agent = _make_agent(monkeypatch, tmp_path, max_attempts=2)
    agent.context_rebase_enabled = False
    compressor = agent.context_compressor
    compressor.context_length = 100_000
    compressor.threshold_tokens = 50_000
    monkeypatch.setattr(compressor, "should_compress", lambda *_: True)
    monkeypatch.setattr(compressor, "should_defer_preflight_to_real_usage", lambda *_: False)
    monkeypatch.setattr(compressor, "get_active_compression_failure_cooldown", lambda: None)
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"fixture {i}"}
               for i in range(60)]
    agent.client.chat.completions.create.return_value = _stop_response()
    try:
        with (
            patch("agent.turn_context.estimate_request_tokens_rough", return_value=tokens),
            patch.object(agent, "_compress_context", side_effect=lambda messages, system, **kw: (messages, system)),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            if blocked:
                with pytest.raises(ContextDispatchError, match="CONTEXT_PREFLIGHT_EXHAUSTED"):
                    agent.run_conversation("Preserve this authentic request", conversation_history=history)
                agent.client.chat.completions.create.assert_not_called()
                rows = agent._session_db.get_messages(agent.session_id)
                assert any(row["role"] == "user" and row["content"] == "Preserve this authentic request"
                           for row in rows)
            else:
                result = agent.run_conversation("Preserve this authentic request", conversation_history=history)
                assert result["completed"] is True
                agent.client.chat.completions.create.assert_called_once()
    finally:
        agent._session_db.close()
