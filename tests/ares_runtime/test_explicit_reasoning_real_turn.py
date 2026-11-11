"""Real AIAgent/SDK and fallback witnesses for explicit effort (offline)."""
from copy import deepcopy
import contextvars
import json
from unittest.mock import patch

import pytest

from ares_runtime.physical_dispatch import (
    bind_inference_client, capture_physical_dispatch, explicit_reasoning_override,
    explicit_reasoning_request_scope,
)
from tests.agent.test_final_sdk_context_dispatch import (
    _codex, _managed, _run_route, agent, durable_agent,  # noqa: F401
)
from tests.run_agent.test_provider_fallback import _make_agent, _mock_client


@pytest.mark.parametrize("mismatch", [False, True])
@pytest.mark.parametrize("outer_streaming", [False, True])
def test_real_turn_checks_final_sdk_body_and_stops_denial_without_retry(
        durable_agent, monkeypatch, record_property, mismatch, outer_streaming):
    item, _ = durable_agent
    item.model = "gpt-6-luna"
    item.reasoning_config = {"enabled": True, "effort": "high"}
    from ares_runtime import managed_calls, physical_dispatch
    scope_trace = []
    context_probe = contextvars.ContextVar("explicit_reasoning_fixture_probe", default=False)
    materialize = managed_calls.produce_dispatch_materialization
    def trace_scope(owner, payload, **kwargs):
        scope_trace.append({"argument_type": type(owner).__name__, "argument_is_item": owner is item,
            "purpose": kwargs.get("purpose"), "canonical_owner_is_item": managed_calls._model_call_owner.get() is item,
            "explicit_at_materialization": explicit_reasoning_override(owner),
            "ordinary_context_probe": context_probe.get(),
            "parent_has_explicit": bool((physical_dispatch._call.get() or {}).get("explicit_reasoning_request"))})
        return materialize(owner, payload, **kwargs)
    monkeypatch.setattr(managed_calls, "produce_dispatch_materialization", trace_scope)
    capture = _codex(item, monkeypatch, outer_streaming=outer_streaming, sealed=False)
    bind_inference_client(capture.client)
    capture.client.max_retries = 3
    _managed(monkeypatch, item.session_id, lambda payload: payload)
    fallback = []
    monkeypatch.setattr(item, "_try_activate_fallback", lambda *a, **k: fallback.append(True) or False)
    if mismatch:
        item.request_overrides = {"extra_body": {"reasoning": {"effort": "medium"}}}
    probe_token = context_probe.set(True)
    try:
        with capture_physical_dispatch() as observed:
            with explicit_reasoning_request_scope(item, {"enabled": True, "effort": "high"}):
                record_property("explicit_at_entry", json.dumps(explicit_reasoning_override(item)))
                result = _run_route(item, "Explain this isolated fixture configuration.")
        events = observed.snapshot()
        record_property("scope_trace", json.dumps(scope_trace))
        record_property("observed_events", json.dumps(events))
        record_property("wire_count", len(capture.wire))
        assert len(events) == 1
        assert events[0]["explicit_reasoning_request"] == {"enabled": True, "effort": "high"}, scope_trace
        assert fallback == []
        if mismatch:
            assert capture.wire == []
            assert result["failed"] and result["failure_reason"] == "egress_denied"
            assert result["failure_retryable"] is False
            # This legacy flag tracks SDK callback entry, not a physical send.
            # Pin that distinction to observed callback entry; the actual wire
            # list above and the single denied physical ticket prove no send
            # and no SDK retry. Do not relabel callback accounting as egress.
            assert result["provider_attempted"] is bool(capture.kwargs)
            assert events[0]["outcome"] == "denied_by_peer"
            assert events[0]["error_code"].startswith("EXPLICIT_REASONING_")
        else:
            assert result.get("error") is None and result["final_response"] == "Done"
            assert len(capture.wire) == 1
            assert capture.wire[0]["reasoning"]["effort"] == "high"
            assert events[0]["native_controls"]["reasoning"]["effort"] == "high"
    finally:
        context_probe.reset(probe_token)
        capture.client.close()


@pytest.mark.parametrize("explicit", [False, True])
def test_actual_configured_fallback_preserves_explicit_but_not_omitted_override(monkeypatch, explicit):
    cfg = {"agent": {"reasoning_effort": "low"}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: deepcopy(cfg))
    item = _make_agent(fallback_model={"provider": "openai", "model": "gpt-6-luna"})
    item.reasoning_config = {"enabled": True, "effort": "high"}
    before = deepcopy(cfg)
    try:
        with patch("agent.auxiliary_client.resolve_provider_client", return_value=(_mock_client(), "gpt-6-luna")):
            with explicit_reasoning_request_scope(item, {"enabled": True, "effort": "high"} if explicit else None):
                assert item._try_activate_fallback() is True
                assert item.model == "gpt-6-luna"
                assert item.reasoning_config == {"enabled": True, "effort": "high" if explicit else "low"}
                if explicit:
                    detached = explicit_reasoning_override(item)
                    detached["effort"] = "medium"
                    assert explicit_reasoning_override(item)["effort"] == "high"
        assert explicit_reasoning_override(item) is None
        assert cfg == before
    finally:
        item.close()
