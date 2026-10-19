"""Auxiliary callback funnels cannot bypass strict governance or change bytes."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock
import pytest
from agent import auxiliary_client as aux
from hermes_cli import middleware as m

@pytest.fixture(params=[False,True])
def lane(request,monkeypatch):
    routed=request.param
    route=("provider:fixture","model:fixture",{"auxiliary_task":"fixture"}) if routed else None
    monkeypatch.setattr(aux,"_relay_auxiliary_metadata",lambda **kw:route)
    from agent import relay_llm
    monkeypatch.setattr(relay_llm,"execute_current",lambda body,callback,**kw:callback(body))
    async def execute_async(body,callback,**kw):return await callback(body)
    monkeypatch.setattr(relay_llm,"execute_current_async",execute_async)
    monkeypatch.setattr(relay_llm,"stream_current",lambda body,callback,**kw:callback(body))
    return routed

@pytest.mark.parametrize("kind",["sync","async","stream"])
@pytest.mark.parametrize("denied",[False,True])
def test_all_auxiliary_funnels_are_governed_before_provider_for_routed_and_unrouted_calls(lane,monkeypatch,kind,denied):
    events=[];settlements=[]
    def govern(**kwargs):
        events.append(("begin",kwargs["payload"]))
        if denied:raise m.EgressDispatchDenied("ROUTE_NOT_ALLOWED","not a provider outage")
        return SimpleNamespace(settle=lambda **kw:settlements.append(kw))
    monkeypatch.setattr(m,"_get_middleware_callbacks",lambda name:[govern] if name=="llm_dispatch" else [])
    body={"model":"model:fixture","messages":[{"role":"user","content":"do not rewrite"}]}
    def send(**kwargs):
        assert events and events[0][0]=="begin"
        assert kwargs==body
        events.append(("send",kwargs))
        return iter(["chunk"]) if kind=="stream" else "response"
    create=Mock(side_effect=send)
    if kind=="async":
        async def async_send(**kwargs):return send(**kwargs)
        create=AsyncMock(side_effect=async_send)
    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    def run():
        if kind=="async":return asyncio.run(aux._relay_async_completion(client,body))
        if kind=="stream":return list(aux._relay_sync_stream(client,body))
        return aux._relay_sync_completion(client,body)
    if denied:
        with pytest.raises(m.EgressDispatchDenied):run()
        create.assert_not_called()
        assert settlements==[]
    else:
        assert run()==(["chunk"] if kind=="stream" else "response")
        assert create.call_count==1
        assert len(settlements)==1 and settlements[0]["outcome"]=="ok"

@pytest.mark.parametrize("kind",["sync","async"])
def test_auxiliary_settlement_failure_withholds_response_after_exactly_one_send(lane,monkeypatch,kind):
    def settle(**kwargs):raise OSError("lost outcome ACK")
    monkeypatch.setattr(m,"_get_middleware_callbacks",lambda name:[lambda **kw:SimpleNamespace(settle=settle)] if name=="llm_dispatch" else [])
    create=AsyncMock(return_value="do not return") if kind=="async" else Mock(return_value="do not return")
    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(m.EgressOutcomeUnrecorded):
        if kind=="async":asyncio.run(aux._relay_async_completion(client,{"model":"fixture"}))
        else:aux._relay_sync_completion(client,{"model":"fixture"})
    assert create.call_count==1
