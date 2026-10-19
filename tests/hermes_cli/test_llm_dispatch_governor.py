"""Strict governance is separate from the generic fail-open execution chain."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from hermes_cli import middleware as m

@pytest.fixture
def callbacks(monkeypatch):
    selected = []
    monkeypatch.setattr(m, "_get_middleware_callbacks", lambda kind: selected if kind == "llm_dispatch" else [])
    return selected


def test_no_governor_returns_no_tickets_or_payload_copy(callbacks):
    class NoCopy:
        def __deepcopy__(self, memo):
            raise AssertionError("off mode must not inspect this payload")
    assert m.begin_llm_dispatch({"body":NoCopy()}) == []


@pytest.mark.parametrize("known", [False, True])
def test_denial_is_pre_send_and_never_falls_through(callbacks, known):
    denied = m.EgressDispatchDenied("GRANT_MISSING", "no grant")
    def governor(**kwargs):
        raise denied if known else OSError("IPC unavailable")
    callbacks.append(governor)
    sdk = Mock()
    with pytest.raises(m.EgressDispatchDenied) as result:
        m.begin_llm_dispatch({"model":"fixture"})
        sdk()
    assert sdk.call_count == 0
    assert result.value.retryable is False
    assert result.value.provider_attempted is False
    assert result.value.code == ("GRANT_MISSING" if known else "DISPATCH_GOVERNOR_ERROR")
    if known:
        assert result.value is denied


def test_each_governor_receives_independent_payload_and_versioned_context(callbacks):
    body = {"messages":[{"role":"user","content":"unchanged"}]}
    original = deepcopy(body)
    seen = []
    first = Mock()
    def inspect(**kwargs):
        seen.append(deepcopy(kwargs))
        kwargs["payload"]["messages"][0]["content"] = "not the provider payload"
        return first
    callbacks.extend([inspect, inspect])
    assert m.begin_llm_dispatch(body, session_id="session:one") == [first, first]
    assert body == original
    assert all(row["payload"] == original and row["session_id"] == "session:one" for row in seen)
    assert all(row["middleware_schema_version"] == m.MIDDLEWARE_SCHEMA_VERSION for row in seen)


def test_strict_copy_failure_never_shares_live_nested_state(callbacks):
    class Opaque:
        def __deepcopy__(self, memo):
            raise TypeError("uncopyable")
    callback = Mock()
    callbacks.append(callback)
    with pytest.raises(m.EgressDispatchDenied, match="DISPATCH_GOVERNOR_ERROR"):
        m.begin_llm_dispatch({"opaque":Opaque(),"messages":[]})
    callback.assert_not_called()


def test_second_governor_denial_settles_all_earlier_tickets_without_egress(callbacks):
    calls = []
    ticket = SimpleNamespace(settle=lambda **kw:calls.append(kw))
    def deny(**kw):
        raise m.EgressDispatchDenied("ROUTE_NOT_ALLOWED", "denied")
    callbacks.extend([lambda **kw:ticket, deny])
    with pytest.raises(m.EgressDispatchDenied):
        m.begin_llm_dispatch({})
    assert calls == [{"outcome":"denied_by_peer","response":None,"error":None}]


def test_malformed_ticket_is_a_pre_send_denial(callbacks):
    callbacks.append(lambda **kw:object())
    with pytest.raises(m.EgressDispatchDenied) as result:
        m.begin_llm_dispatch({})
    assert result.value.code == "DISPATCH_GOVERNOR_ERROR"


def test_settlement_failure_is_post_send_nonretryable_and_does_not_skip_other_tickets():
    settled = []
    def fail(**kwargs):
        raise OSError("lost ACK")
    tickets = [SimpleNamespace(settle=fail),SimpleNamespace(settle=lambda **kw:settled.append(kw))]
    response = object()
    with pytest.raises(m.EgressOutcomeUnrecorded) as result:
        m.settle_llm_dispatch(tickets,outcome="ok",response=response)
    assert result.value.code == "DISPATCH_OUTCOME_UNRECORDED"
    assert result.value.provider_attempted is True
    assert result.value.retryable is False
    assert settled == [{"outcome":"ok","response":response,"error":None}]


@pytest.mark.parametrize("outcome",["ok","provider_error","interrupted","discarded","abandoned","denied_by_peer"])
def test_closed_outcomes_are_forwarded_exactly_once(outcome):
    settle = Mock()
    response,error = object(),RuntimeError("provider failure")
    m.settle_llm_dispatch([SimpleNamespace(settle=settle)],outcome=outcome,response=response,error=error)
    settle.assert_called_once_with(outcome=outcome,response=response,error=error)


def test_unknown_outcome_is_rejected_before_ticket_callbacks():
    settle = Mock()
    with pytest.raises(ValueError):
        m.settle_llm_dispatch([SimpleNamespace(settle=settle)],outcome="success-ish")
    settle.assert_not_called()
