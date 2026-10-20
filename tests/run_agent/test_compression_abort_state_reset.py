"""Regression tests for #58630: every compression abort path must reset
per-attempt in-place compaction state.

After a successful in-place compaction sets ``_last_compaction_in_place=True``
(run-level gateway signal), a later attempt that aborts or skips through ANY
early-return path in ``compress_context`` must NOT reuse that stale flag as
the flush baseline: ``conversation_history_after_compression()`` would then
treat all current messages (including unflushed new turns) as persisted, and a
restart would lose them.

The fix records a per-attempt outcome (``_last_compression_attempt_recorded``/
``_last_compression_attempt_in_place``) at the very top of
``compress_context`` — before the codex-app-server route, breaker gates, lock
acquisition, rotated-parent skips, compressor-abort, no-progress and
empty-transcript returns — so every abort path leaves the attempt outcome
``None`` and callers retain the previous flush baseline.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch


def _make_agent(session_db):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=session_db,
            session_id="abort-state-session",
            skip_context_files=True,
            skip_memory=True,
        )
    return agent


class _InPlaceSuccessCompressor:
    _last_compress_aborted = False
    _last_summary_error = None
    compression_count = 1
    _last_compression_made_progress = True
    _last_summary_fallback_used = False
    last_compression_rough_tokens = 0
    last_prompt_tokens = 0
    last_completion_tokens = 0
    awaiting_real_usage_after_compression = False

    def compress(self, _messages, **_kwargs):
        return [
            {"role": "user", "content": "[summary] earlier state"},
            {"role": "assistant", "content": "retained tail"},
        ]


class _BreakerBlockedCompressor(_InPlaceSuccessCompressor):
    """Trips the pre-lock automatic-compression breaker gate."""

    def _automatic_compression_blocked(self):
        return True

    def compress(self, messages, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("compress() must not be reached when blocked")


class _NoProgressCompressor(_InPlaceSuccessCompressor):
    """Returns a semantically-equal transcript (no-op attempt)."""

    _last_compression_made_progress = False

    def compress(self, messages, **_kwargs):
        return [dict(m) for m in messages]


class TestAbortPathsResetPerAttemptState:
    def _in_place_success(self, agent, messages):
        from agent.conversation_compression import (
            compress_context,
            conversation_history_after_compression,
        )
        agent.context_compressor = _InPlaceSuccessCompressor()
        compacted, _ = compress_context(
            agent, messages, "system", approx_tokens=100_000
        )
        assert agent._last_compaction_in_place is True
        assert agent._last_compression_attempt_in_place is True
        return compacted, conversation_history_after_compression(
            agent, compacted, None
        )

    def test_breaker_blocked_skip_retains_previous_baseline(self):
        """Pre-lock breaker skip after an in-place success must keep baseline."""
        from agent.conversation_compression import (
            compress_context,
            conversation_history_after_compression,
        )
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = _make_agent(db)
            agent.compression_in_place = True
            original = [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ]
            agent._flush_messages_to_session_db(original, [])
            compacted, history = self._in_place_success(agent, original)

            messages = compacted + [
                {"role": "user", "content": "new request"},
                {"role": "assistant", "content": "new answer"},
            ]
            agent.context_compressor = _BreakerBlockedCompressor()
            returned, _ = compress_context(
                agent, messages, "system", approx_tokens=100_000
            )
            assert returned is messages
            # Per-attempt outcome must be reset even though this attempt
            # returned before acquiring the lock.
            assert agent._last_compression_attempt_recorded is True
            assert agent._last_compression_attempt_in_place is None
            new_history = conversation_history_after_compression(
                agent, returned, history
            )
            # Skip = previous baseline stays authoritative: not all-persisted
            # (would drop the new pair on restart), not None (would re-append
            # the compacted rows).
            assert new_history is history
            db.close()

    def test_automatic_abort_warning_does_not_claim_lineage_recovery(self):
        from unittest.mock import MagicMock

        from agent.context_engine import CONTEXT_REBASE_LINEAGE_INTEGRITY_MISMATCH
        from agent.conversation_compression import compress_context
        from hermes_state import SessionDB

        class LineageAbortCompressor(_InPlaceSuccessCompressor):
            _last_compress_aborted = True
            _last_summary_error = (
                "context-governor compact-v2 failed [lineage_integrity_mismatch]"
            )
            last_outcome = {
                "kind": "compaction_failed_closed",
                "source_operation": "compact-v2",
                "source_error_code": "lineage_integrity_mismatch",
            }
            _last_compression_made_progress = False

            def automatic_rebase_block_reason(self):
                return CONTEXT_REBASE_LINEAGE_INTEGRITY_MISMATCH

            def compress(self, messages, **_kwargs):
                return messages

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "warning.db")
            agent = _make_agent(db)
            agent._emit_warning = MagicMock()
            agent.context_compressor = LineageAbortCompressor()
            messages = [
                {"role": "user", "content": "current request"},
                {"role": "assistant", "content": "earlier answer"},
            ]

            returned, _ = compress_context(
                agent, messages, "system", approx_tokens=100_000
            )

            assert returned == messages
            agent._emit_warning.assert_called_once()
            warning = agent._emit_warning.call_args.args[0].lower()
            assert "lineage integrity mismatch" in warning
            assert "no compacted transcript was committed" in warning
            assert "automatic durable-state rebase is blocked" in warning
            assert "conversation continues unchanged" not in warning
            assert "run /compress to retry" not in warning
            db.close()


