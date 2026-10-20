"""Legacy threaded facades are not cancellation-qualified bounded routes."""
from types import SimpleNamespace

import pytest

from hermes_cli import middleware
from tests.ares_runtime.test_transport_lease import lease


@pytest.mark.asyncio
@pytest.mark.parametrize('adapter_name',[
    '_AsyncBedrockCompletionsAdapter','_AsyncCodexCompletionsAdapter','_AsyncAnthropicCompletionsAdapter'])
async def test_enforced_thread_bridge_refuses_before_worker(adapter_name,monkeypatch):
    from agent import auxiliary_client
    sends=[];settles=[]
    sync=SimpleNamespace(create=lambda **kw:sends.append(kw))
    monkeypatch.setattr(middleware,'_get_middleware_callbacks',lambda _: [lambda **kw:
        SimpleNamespace(transport_lease=lease(),settle=lambda **kw:settles.append(kw))])
    adapter=getattr(auxiliary_client,adapter_name)(sync)
    with pytest.raises(middleware.EgressDispatchDenied) as caught:
        await adapter.create(model='local',messages=[],max_tokens=7)
    assert caught.value.code=='UNSUPPORTED_TRANSPORT'
    assert not sends
    assert len(settles)==1 and settles[0]['outcome']=='denied_by_peer'



def test_protected_daemon_worker_refuses_lease_before_start(monkeypatch):
    from agent import auxiliary_client
    sends=[];settles=[]
    monkeypatch.setattr(auxiliary_client,'_capture_aux_cancel_check',lambda: lambda:False)
    monkeypatch.setattr(auxiliary_client,'_aux_interrupt_protected',lambda:True)
    monkeypatch.setattr(middleware,'_get_middleware_callbacks',lambda _: [lambda **kw:
        SimpleNamespace(transport_lease=lease(),settle=lambda **kw:settles.append(kw))])
    with pytest.raises(middleware.EgressDispatchDenied) as caught:
        auxiliary_client._run_protected_sync_provider_call(lambda kw:sends.append(kw),{'max_tokens':7})
    assert caught.value.code=='UNSUPPORTED_TRANSPORT'
    assert not sends
    assert len(settles)==1 and settles[0]['outcome']=='denied_by_peer'


@pytest.mark.parametrize('method',['turn/start','turn/steer','thread/compact/start','thread/compact'])
def test_codex_app_server_refuses_before_inference_rpc(monkeypatch,method):
    import threading
    from agent.transports.codex_app_server import CodexAppServerClient
    pipe=object.__new__(CodexAppServerClient)
    pipe._pending_lock=threading.Lock();pipe._closed=False;pipe._pending={};pipe._next_id=1
    sent=[]
    def send(frame,**kw):
        sent.append(frame)
        pipe._pending[frame['id']].queue.put({'result':{}})
    pipe._send=send
    monkeypatch.setattr(middleware,'_get_middleware_callbacks',lambda _: [lambda **kw:
        SimpleNamespace(transport_lease=lease(),settle=lambda **kw:None)])
    with pytest.raises(middleware.EgressDispatchDenied,match='UNSUPPORTED_TRANSPORT'):
        pipe.request(method,{'input':'private'},timeout=.1)
    assert not sent
