"""Task 2.2 acceptance — the preflight estimate-drift log line is exercised.

Verifier run-1 left this as "not checked": the drift telemetry added in
8ef3f74722 had no test. Exercises the exact branch in build_turn_context:
when the rough preflight estimate exceeds the last REAL provider-reported
prompt by >= 15 percent, one bounded INFO line is logged; below the
threshold (or without a real reading), no line.

Agent/build harness mirrors tests/agent/test_api_content_sidecar.py.
"""
from __future__ import annotations

import logging
import types

import agent.turn_context as tc


class _FakeTodoStore:
    def snapshot_sections(self, *_a, **_k):
        return {}


class _FakeGuardrails:
    def reset_for_turn(self):
        pass


class _Compressor(types.SimpleNamespace):
    def __init__(self, *, real):
        super().__init__(
            protect_first_n=3,
            protect_last_n=1,
            threshold_tokens=787_500,
            last_prompt_tokens=-1,
            last_real_prompt_tokens=real,
            context_length=1_050_000,
        )

    def should_defer_preflight_to_real_usage(self, _tokens):
        return False

    def snapshot_preflight_display_tokens(self):
        return None

    def get_active_compression_failure_cooldown(self):
        return None

    def should_compress(self, messages, rough_tokens=None):
        # The preflight probe asks whether compaction would run; the drift
        # log fires before that decision, so False is fine here.
        return False

    def has_content_to_compress(self, messages):
        return True


class _FakeAgent:
    """Minimal stand-in covering only what the prologue touches
    (mirrors tests/agent/test_api_content_sidecar.py)."""

    def __init__(self, comp):
        self.session_id = "sess-drift"
        self.model = "test/model"
        self.provider = "openrouter"
        self.base_url = "https://openrouter.ai/api/v1"
        self.api_key = "sk-x"
        self.api_mode = "chat_completions"
        self.platform = "cli"
        self.quiet_mode = True
        self.max_iterations = 90
        self.tools = []
        self.valid_tool_names = set()
        self._skip_mcp_refresh = True
        self.compression_enabled = True
        self.context_compressor = comp
        self._cached_system_prompt = "SYSTEM"
        self._memory_store = None
        self._memory_manager = None
        self._memory_nudge_interval = 0
        self._turns_since_memory = 0
        self._user_turn_count = 0
        self._todo_store = _FakeTodoStore()
        self._tool_guardrails = _FakeGuardrails()
        self._compression_warning = None
        self._interrupt_requested = False
        self._memory_write_origin = "assistant_tool"
        self._stream_context_scrubber = None
        self._stream_think_scrubber = None

    def _ensure_db_session(self):
        pass

    def _restore_primary_runtime(self):
        pass

    def _cleanup_dead_connections(self):
        return False

    def _emit_status(self, _msg):
        pass

    def _replay_compression_warning(self):
        pass

    def _hydrate_todo_store(self, *_a, **_k):
        pass

    def _safe_print(self, *_a, **_k):
        pass

    def _persist_session(self, messages, _history=None):
        pass


def _build(agent):
    return tc.build_turn_context(
        agent=agent,
        user_message="hello",
        system_message=None,
        conversation_history=None,
        task_id=None,
        stream_callback=None,
        persist_user_message=None,
        restore_or_build_system_prompt=lambda *a, **k: None,
        install_safe_stdio=lambda: None,
        sanitize_surrogates=lambda s: s,
        summarize_user_message_for_log=lambda s: s,
        set_session_context=lambda _sid: None,
        set_current_write_origin=lambda _o: None,
        ra=lambda: types.SimpleNamespace(_set_interrupt=lambda *a, **k: None),
        moa_active=False,
    )


class TestDriftTelemetry:
    def _run(self, caplog, est, real, monkeypatch):
        # The real estimator is patched out: this test owns the LOG BRANCH,
        # not the estimator (test_token_estimate_shadow.py owns that).
        monkeypatch.setattr(tc, "estimate_request_tokens_rough", lambda *a, **k: est)
        monkeypatch.setattr(
            tc, "_should_run_preflight_estimate", lambda *a, **k: True
        )
        monkeypatch.setattr(
            tc, "_review_fork_first_request_pending", lambda _a: False
        )
        agent = _FakeAgent(_Compressor(real=real))
        with caplog.at_level(logging.INFO, logger="agent.turn_context"):
            _build(agent)

    def test_drift_line_logged_at_15pct_gap(self, caplog, monkeypatch):
        # The measured incident shape: estimate 1,086,932 vs real 898,481 (+20.9%).
        self._run(caplog, est=1_086_932, real=898_481, monkeypatch=monkeypatch)
        drift = [r for r in caplog.records if "Preflight estimate drift" in r.getMessage()]
        assert drift, "drift line must be logged at >=15% gap"
        msg = drift[0].getMessage()
        assert "1,086,932" in msg and "898,481" in msg and "+21.0%" in msg

    def test_no_drift_line_below_threshold(self, caplog, monkeypatch):
        self._run(caplog, est=930_000, real=898_481, monkeypatch=monkeypatch)  # +3.5%
        drift = [r for r in caplog.records if "Preflight estimate drift" in r.getMessage()]
        assert not drift

    def test_no_drift_line_without_real_usage(self, caplog, monkeypatch):
        self._run(caplog, est=1_086_932, real=-1, monkeypatch=monkeypatch)  # no provider report yet
        drift = [r for r in caplog.records if "Preflight estimate drift" in r.getMessage()]
        assert not drift
