"""Policy failures are terminal even when their wording resembles API outages."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock
import pytest
from agent import auxiliary_client as aux
from hermes_cli import middleware as m


@pytest.mark.parametrize("kind",["sync","async","stream"])
@pytest.mark.parametrize("after_send",[False,True])
def test_public_auxiliary_governance_never_classifies_retries_or_constructs_fallback(monkeypatch,kind,after_send):
    error=(m.EgressOutcomeUnrecorded(message="Connection error: outcome ACK lost") if after_send else
           m.EgressDispatchDenied("DAEMON_UNREACHABLE","Connection error; unsupported temperature"))
    def settle(**kwargs):raise error
    def govern(**kwargs):
        if not after_send:raise error
        return SimpleNamespace(settle=settle)
    monkeypatch.setattr(m,"_get_middleware_callbacks",lambda name:[govern] if name=="llm_dispatch" else [])
    response=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="withhold"),finish_reason="stop")])
    create=AsyncMock(return_value=response) if kind=="async" else Mock(return_value=iter(["chunk"]) if kind=="stream" else response)
    client=SimpleNamespace(base_url="https://provider.invalid/v1",api_key="fixture-key",chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    factory=Mock(return_value=(client,"fixture-model"))
    monkeypatch.setattr(aux,"_get_cached_client",factory)
    monkeypatch.setattr(aux,"_resolve_task_provider_model",lambda *a,**kw:("openai","fixture-model","https://provider.invalid/v1","fixture-key","chat_completions"))
    monkeypatch.setattr(aux,"_get_auxiliary_task_config",lambda task:{"fallback_chain":[
        {"provider":"openai","model":"fixture-model"},
        {"provider":"openrouter","model":"unused-fallback"}]})
    fallback=Mock(side_effect=AssertionError("governance denial constructed a fallback provider"))
    monkeypatch.setattr(aux,"_resolve_fallback_entry",fallback)
    classifiers=[]
    def classify(*args,**kwargs):
        classifiers.append(True)
        raise AssertionError("governance reached provider retry classifier")
    for name in ["_is_transient_transport_error","_is_auth_error","_is_payment_error","_is_connection_error",
                 "_is_rate_limit_error","_is_model_incompatible_error","_is_invalid_aux_response_error",
                 "_is_unsupported_temperature_error"]:
        monkeypatch.setattr(aux,name,classify)
    kwargs={"task":"compression","messages":[{"role":"user","content":"fixture"}],"max_tokens":32}
    with pytest.raises(type(error)) as result:
        if kind=="async":asyncio.run(aux.async_call_llm(**kwargs))
        else:
            value=aux.call_llm(**kwargs,stream=kind=="stream")
            if kind=="stream":list(value)
    assert result.value is error
    assert classifiers==[]
    assert create.call_count==int(after_send)
    assert factory.call_count==1
    fallback.assert_not_called()
