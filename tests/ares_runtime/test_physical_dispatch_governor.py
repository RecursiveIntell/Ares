"""Callback-scope governance behavior; not network-attempt certification."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from hermes_cli import middleware as m
from ares_runtime.continuity.runtime import ContextDispatchPhysicalScope, context_dispatch_physical_call

@pytest.fixture
def governed(monkeypatch):
    settlements = []
    seen = []
    def begin(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(settle=lambda **kw:settlements.append(kw))
    monkeypatch.setattr(m, "_get_middleware_callbacks", lambda kind:[begin] if kind == "llm_dispatch" else [])
    agent = SimpleNamespace(session_id="session:fixture",provider="fixture",model="model:fixture",api_mode="chat_completions")
    return agent,seen,settlements


def scope(agent):
    return ContextDispatchPhysicalScope(agent,None,attempt_id="attempt:fixture",materialization_digest=None,route_identity="route:fixture")


def test_unsealed_callback_gets_preflight_before_send_and_response_settlement(governed):
    agent,seen,settlements=governed
    response=object()
    with scope(agent) as physical:
        def sdk(body):
            assert len(seen)==1 and settlements==[]
            return response
        assert physical.call({"model":"fixture"},sdk) is response
        physical.settle(response)
    assert settlements==[{"outcome":"ok","response":response,"error":None}]


@pytest.mark.parametrize("error",[OSError("provider"),InterruptedError("cancel"),KeyboardInterrupt()])
def test_callback_error_is_settled_once_and_original_is_preserved(governed,error):
    agent,seen,settlements=governed
    with pytest.raises(type(error)) as result:
        with scope(agent) as physical:
            physical.call({},lambda body:(_ for _ in ()).throw(error))
    assert result.value is error
    assert len(seen)==len(settlements)==1
    assert settlements[0]["outcome"]==("interrupted" if isinstance(error,(InterruptedError,KeyboardInterrupt)) else "provider_error")
    assert settlements[0]["error"] is error


def test_scope_exit_without_settle_is_abandoned(governed):
    agent,_,settlements=governed
    with scope(agent) as physical:
        physical.call({},lambda body:object())
    assert [x["outcome"] for x in settlements]==["abandoned"]


def test_second_callback_discards_first_before_next_begin(governed):
    agent,seen,settlements=governed
    with scope(agent) as physical:
        physical.call({},lambda body:"old")
        def second(body):
            assert len(seen)==2
            assert [x["outcome"] for x in settlements]==["discarded"]
            return "new"
        physical.call({},second)
        physical.settle("new")
    assert [x["outcome"] for x in settlements]==["discarded","ok"]


def test_scope_denial_does_not_mark_provider_attempted(governed,monkeypatch):
    agent,_,settlements=governed
    def denied(**kwargs):
        raise m.EgressDispatchDenied("GRANT_MISSING")
    monkeypatch.setattr(m,"_get_middleware_callbacks",lambda kind:[denied])
    sdk=Mock()
    with scope(agent) as physical:
        with pytest.raises(m.EgressDispatchDenied):physical.call({},sdk)
        assert physical.attempted is False
    sdk.assert_not_called()
    assert settlements==[]


def test_no_scope_nonstream_is_governed_and_settled(governed):
    agent,seen,settlements=governed
    assert context_dispatch_physical_call(agent,{},lambda body:"answer")=="answer"
    assert len(seen)==1
    assert settlements==[{"outcome":"ok","response":"answer","error":None}]


@pytest.mark.parametrize("consume",[True,False])
def test_no_scope_stream_retains_ticket_until_exhaustion_or_close(governed,consume):
    agent,seen,settlements=governed
    stream=context_dispatch_physical_call(agent,{"stream":True},lambda body:iter(["one","two"]))
    assert len(seen)==1 and settlements==[]
    if consume:
        assert list(stream)==["one","two"]
    else:
        assert next(stream)=="one"
        stream.close()
    assert [x["outcome"] for x in settlements]==["ok" if consume else "abandoned"]
    stream.close()
    assert len(settlements)==1


def test_off_no_scope_preserves_stream_identity(monkeypatch):
    monkeypatch.setattr(m,"_get_middleware_callbacks",lambda kind:[])
    stream=iter([1])
    agent=SimpleNamespace(session_id="fixture")
    assert context_dispatch_physical_call(agent,{"stream":True},lambda body:stream) is stream



def test_distinct_fallback_payloads_never_reuse_logical_materialization(governed,monkeypatch):
    from ares_runtime.physical_dispatch import _call
    agent,_,_=governed
    produced=[]
    def produce(agent,payload,**kw):
        basis={'materialization':dict(payload),'qualification_error':None}
        produced.append(basis)
        return basis
    monkeypatch.setattr('ares_runtime.managed_calls.produce_dispatch_materialization',produce)
    with scope(agent) as physical:
        first=physical.call({'model':'first'},lambda body:_call.get())
        second=physical.call({'model':'fallback'},lambda body:_call.get())
        physical.settle()
    assert first['materialization']['model']=='first'
    assert second['materialization']['model']=='fallback'
    assert len(produced)==2
