"""Stream ownership follows real raw HTTP/SDK iteration and disposal."""
import asyncio
import gc
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import openai
import pytest

from ares_runtime.physical_dispatch import bind_inference_client, bind_http_client
from hermes_cli import middleware
from tests.ares_runtime.test_transport_deadline import govern


@pytest.fixture
def stream_endpoint():
    bodies=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_POST(self):
            bodies.append(self.rfile.read(int(self.headers['content-length'])))
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            chunk={'id':'x','object':'chat.completion.chunk','model':'x','choices':[{'index':0,'delta':{'content':'ok'},'finish_reason':None}]}
            try:
                self.wfile.write(('data: '+json.dumps(chunk)+'\n\n').encode());self.wfile.flush()
                if len(bodies)==1:
                    time.sleep(.4)
                self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError):
                pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever);thread.start()
    yield f'http://127.0.0.1:{server.server_port}/v1',bodies
    server.shutdown();server.server_close();thread.join()


def test_sdk_sync_stream_deadline_is_terminal_once(stream_endpoint,monkeypatch):
    url,bodies=stream_endpoint;outcomes=govern(monkeypatch,send_timeout_ms=180)
    with bind_inference_client(openai.OpenAI(api_key='local',base_url=url,max_retries=2,
            http_client=httpx.Client(trust_env=False))) as sdk:
        stream=sdk.chat.completions.create(model='x',messages=[],max_tokens=7,stream=True)
        assert next(stream).choices[0].delta.content=='ok'
        with pytest.raises(middleware.EgressOutcomeUnrecorded):
            list(stream)
        stream.close()
    assert len(outcomes)==len(bodies)==1 and outcomes[0]['outcome']!='ok'


@pytest.mark.asyncio
async def test_sdk_async_stream_deadline_is_terminal_once(stream_endpoint,monkeypatch):
    url,bodies=stream_endpoint;outcomes=govern(monkeypatch,send_timeout_ms=180)
    async with bind_inference_client(openai.AsyncOpenAI(api_key='local',base_url=url,max_retries=2,
            http_client=httpx.AsyncClient(trust_env=False))) as sdk:
        stream=await sdk.chat.completions.create(model='x',messages=[],max_tokens=7,stream=True)
        assert (await anext(stream)).choices[0].delta.content=='ok'
        with pytest.raises(middleware.EgressOutcomeUnrecorded):
            async for _ in stream:
                pass
        await stream.close()
    assert len(outcomes)==len(bodies)==1 and outcomes[0]['outcome']!='ok'


@pytest.mark.asyncio
async def test_async_unconsumed_response_gc_owns_terminal(stream_endpoint,monkeypatch):
    url,bodies=stream_endpoint;outcomes=govern(monkeypatch)
    async with bind_http_client(httpx.AsyncClient(trust_env=False)) as client:
        request=client.build_request('POST',url+'/chat/completions',json={'max_tokens':7})
        response=await client.send(request,stream=True)
        assert not outcomes
        del response
        gc.collect()
        await asyncio.sleep(0)
        gc.collect()
        assert len(outcomes)==1 and outcomes[0]['outcome']=='abandoned'
    assert len(bodies)==1


def test_sync_unconsumed_response_gc_owns_terminal(stream_endpoint,monkeypatch):
    url,bodies=stream_endpoint;outcomes=govern(monkeypatch)
    with bind_http_client(httpx.Client(trust_env=False)) as client:
        response=client.send(client.build_request('POST',url+'/chat/completions',json={'max_tokens':7}),stream=True)
        assert not outcomes
        del response
        gc.collect()
        assert len(outcomes)==1 and outcomes[0]['outcome']=='abandoned'
    assert len(bodies)==1


@pytest.mark.asyncio
async def test_async_lazy_manager_retains_its_materialization_scope(stream_endpoint,monkeypatch):
    url,bodies=stream_endpoint;observations=[]
    def govern(*,payload,dispatch_phase,**kw):
        if dispatch_phase=='physical_send':
            observations.append(payload)
    monkeypatch.setattr(middleware,'_get_middleware_callbacks',lambda _: [govern])
    async with bind_http_client(httpx.AsyncClient(trust_env=False)) as client:
        async def callback(payload):
            return client.stream('POST',url+'/chat/completions',json=payload)
        manager=await middleware.async_run_llm_dispatch({'max_tokens':7},callback)
        async with manager as response:
            await response.aread()
    assert len(bodies)==len(observations)==1
    assert observations[0]['qualification_error']=='SESSION_IDENTITY_UNAVAILABLE'


def test_sdk_stream_proxy_gc_terminates_invocation_and_physical_tickets(stream_endpoint,monkeypatch):
    from types import SimpleNamespace
    url,bodies=stream_endpoint;outcomes=[]
    def govern(*,dispatch_phase,**kw):
        return SimpleNamespace(settle=lambda **kw:outcomes.append((dispatch_phase,kw)))
    monkeypatch.setattr(middleware,'_get_middleware_callbacks',lambda _: [govern])
    with bind_inference_client(openai.OpenAI(api_key='local',base_url=url,max_retries=0,
            http_client=httpx.Client(trust_env=False))) as sdk:
        result=middleware.run_llm_dispatch({'model':'x','messages':[],'max_tokens':7,'stream':True},
            lambda payload:sdk.chat.completions.create(**payload),stream=True)
        assert not outcomes
        del result;gc.collect()
        assert sorted(phase for phase,_ in outcomes)==['physical_send','sdk_invocation']
        assert all(kw['outcome']=='abandoned' for _,kw in outcomes)
    assert len(bodies)==1
