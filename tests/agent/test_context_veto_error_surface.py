"""Task 3.2 — context-veto failures must not surface as retryable.

Observed 2026-10-26 (session 20261025_161506_c06222): the desktop showed
``retryable=true`` for a turn whose compaction was vetoed by
``lineage_integrity_mismatch``; unchanged retries re-vetoed 5 times in 5
minutes. A resend cannot fix an exact-prefix failure — recovery is a
context-recovery action, not a resend.
"""
from __future__ import annotations

from agent.error_surface import (
    build_error_surface_from_exception,
    build_error_surface_from_result,
)


class TestContextVetoNotRetryable:
    def test_result_lineage_veto_not_retryable(self):
        result = {
            "failed": True,
            "error": (
                "context-governor compaction failed; keeping original messages: "
                "context-governor compact-v2 failed [lineage_integrity_mismatch]"
            ),
            "failure_reason": "api_error",
        }
        surface = build_error_surface_from_result(result, provider="openai-codex")
        assert surface is not None
        assert surface["code"] == "lineage_integrity_mismatch"
        assert surface["retryable"] is False

    def test_exception_preflight_exhausted_not_retryable(self):
        class ContextDispatchError(Exception):
            pass

        exc = ContextDispatchError("CONTEXT_PREFLIGHT_EXHAUSTED")
        surface = build_error_surface_from_exception(exc, provider="openai-codex")
        assert surface is not None
        assert surface["code"] == "context_preflight_exhausted"
        assert surface["retryable"] is False

    def test_exception_lineage_veto_not_retryable(self):
        exc = RuntimeError(
            "compact-v2 failed [lineage_integrity_mismatch]"
        )
        surface = build_error_surface_from_exception(exc)
        assert surface is not None
        assert surface["code"] == "lineage_integrity_mismatch"
        assert surface["retryable"] is False

    def test_other_gateway_exceptions_still_retryable(self):
        # Control: the generic non-API fallback keeps its legacy verdict.
        exc = RuntimeError("some unrelated dispatcher bug")
        surface = build_error_surface_from_exception(exc)
        assert surface is not None
        assert surface["retryable"] is True
        assert surface["layer"] == "gateway"
