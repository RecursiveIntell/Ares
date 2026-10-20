"""Real main-loop governance failures cannot enter API retry/fallback handling."""
from types import SimpleNamespace
import pytest
from hermes_cli import middleware as m
from tests.ares_runtime.test_continuity_dispatch import durable_agent  # noqa: F401
from tests.agent.test_context_dispatch_relay_boundary import _run, _mock_response, agent  # noqa: F401

@pytest.mark.parametrize("after_send",[False,True])
def test_governance_is_nonretryable_with_correct_budget_and_send_accounting(durable_agent,monkeypatch,after_send):
    agent,db=durable_agent
    agent.tools=[]
    agent._disable_streaming=True
    agent.client.chat.completions.create.return_value=_mock_response(content="withhold this",finish_reason="stop")
    def settle(**kwargs):
        raise OSError("outcome ACK lost")
    def govern(**kwargs):
        if not after_send:
            raise m.EgressDispatchDenied("GRANT_MISSING", "start the configured governance daemon")
        return SimpleNamespace(settle=settle)
    monkeypatch.setattr(m,"_get_middleware_callbacks",lambda kind:[govern] if kind=="llm_dispatch" else [])
    classified=[]
    def classify(*args,**kwargs):
        classified.append(True)
        raise AssertionError("governance may not reach provider retry classifier")
    monkeypatch.setattr("agent.conversation_loop.classify_api_error",classify)
    result=_run(agent)
    assert classified==[]
    assert result["failed"] is True and result["completed"] is False
    assert result["failure_retryable"] is False
    assert result["failure_reason"]==("egress_outcome_unrecorded" if after_send else "egress_denied")
    assert result["provider_attempted"] is after_send
    assert result["api_calls"]==int(after_send)
    assert agent.client.chat.completions.create.call_count==int(after_send)
    assert result.get("final_response")!="withhold this"
