"""Total deadline is not a resetting idle-read timeout."""
import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import openai
import pytest

from ares_runtime.physical_dispatch import bind_inference_client
from hermes_cli import middleware
from tests.ares_runtime.test_transport_lease import lease
from tests.ares_runtime.test_physical_http_transport import endpoint  # noqa: F401


@pytest.fixture
def dripping():
    bodies, mode = [], ['body']
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            bodies.append(self.rfile.read(int(self.headers['content-length'])))
            try:
                if mode[0] == 'headers':
                    time.sleep(1)
                data = json.dumps({'id':'x', 'choices':[{'index':0,'message':{'role':'assistant','content':'ok'}}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(data)))
                self.end_headers()
                if mode[0]=='truncate':
                    self.wfile.write(data[:1]);self.wfile.flush();return
                for byte in data:
                    self.wfile.write(bytes([byte]));self.wfile.flush();time.sleep(.025)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever);thread.start()
    yield f'http://127.0.0.1:{server.server_port}/v1',bodies,mode
    server.shutdown();server.server_close();thread.join()


def govern(monkeypatch, **bounds):
    outcomes=[]
    value=lease();value.update(bounds)
    monkeypatch.setattr(middleware,'_get_middleware_callbacks',lambda _: [lambda **kw:
        SimpleNamespace(transport_lease=value,settle=lambda **kw: outcomes.append(kw))])
    return outcomes


@pytest.mark.parametrize('mode',['body','headers'])
def test_sync_total_deadline_stops_actual_sdk_without_retry(dripping, monkeypatch, mode):
    url,bodies,setting=dripping;setting[0]=mode
    outcomes=govern(monkeypatch,send_timeout_ms=180)
    with bind_inference_client(openai.OpenAI(api_key='local',base_url=url,max_retries=2,
            http_client=httpx.Client(trust_env=False,timeout=5))) as sdk:
        started=time.monotonic()
        with pytest.raises(middleware.EgressOutcomeUnrecorded) as caught:
            sdk.chat.completions.create(model='x',messages=[],max_tokens=7)
        elapsed=time.monotonic()-started
    assert caught.value.code=='TRANSPORT_DEADLINE_EXCEEDED'
    # SDK lazy imports/transforms precede physical admission. The transport
    # duration starts at the actual physical callback, not SDK invocation.
    assert elapsed < 2.5
    assert len(bodies)==len(outcomes)==1
    assert outcomes[0]['response']['duration_ms'] < 900
    assert outcomes[0]['outcome']!='ok'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['body','headers'])
async def test_async_total_deadline_stops_actual_sdk_without_retry(dripping, monkeypatch, mode):
    url,bodies,setting=dripping;setting[0]=mode
    outcomes=govern(monkeypatch,send_timeout_ms=180)
    async with bind_inference_client(openai.AsyncOpenAI(api_key='local',base_url=url,max_retries=2,
            http_client=httpx.AsyncClient(trust_env=False,timeout=5))) as sdk:
        started=time.monotonic()
        with pytest.raises(middleware.EgressOutcomeUnrecorded) as caught:
            await sdk.chat.completions.create(model='x',messages=[],max_tokens=7)
        elapsed=time.monotonic()-started
    assert caught.value.code=='TRANSPORT_DEADLINE_EXCEEDED'
    # SDK lazy imports/transforms precede physical admission. The transport
    # duration starts at the actual physical callback, not SDK invocation.
    assert elapsed < 2.5
    assert len(bodies)==len(outcomes)==1
    assert outcomes[0]['response']['duration_ms'] < 900
    assert outcomes[0]['outcome']!='ok'


@pytest.mark.asyncio
async def test_async_cancel_settles_once_and_no_retry(dripping,monkeypatch):
    url,bodies,_=dripping;outcomes=govern(monkeypatch)
    async with bind_inference_client(openai.AsyncOpenAI(api_key='local',base_url=url,max_retries=2,
            http_client=httpx.AsyncClient(trust_env=False))) as sdk:
        task=asyncio.create_task(sdk.chat.completions.create(model='x',messages=[],max_tokens=7))
        while not bodies:
            await asyncio.sleep(.01)
        await asyncio.sleep(.06)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(outcomes)==len(bodies)==1
    assert outcomes[0]['outcome']=='interrupted'


@pytest.mark.parametrize('asynchronous',[False,True])
def test_dns_owner_is_killed_and_reaped_under_total_deadline(endpoint,monkeypatch,asynchronous):
    from ares_runtime import bounded_dns
    url,bodies,_=endpoint;url=url.replace('127.0.0.1','localhost')
    monkeypatch.setattr(bounded_dns,'_CODE','import time;time.sleep(10)')
    outcomes=govern(monkeypatch,send_timeout_ms=180)
    started=time.monotonic()
    async def run():
        async with bind_inference_client(openai.AsyncOpenAI(api_key='local',base_url=url,max_retries=2,
                http_client=httpx.AsyncClient(trust_env=False))) as sdk:
            await sdk.chat.completions.create(model='x',messages=[],max_tokens=7)
    with pytest.raises(middleware.EgressOutcomeUnrecorded) as caught:
        if asynchronous:
            asyncio.run(run())
        else:
            with bind_inference_client(openai.OpenAI(api_key='local',base_url=url,max_retries=2,
                    http_client=httpx.Client(trust_env=False))) as sdk:
                sdk.chat.completions.create(model='x',messages=[],max_tokens=7)
    assert caught.value.code=='TRANSPORT_DEADLINE_EXCEEDED'
    assert time.monotonic()-started<.9
    assert not bodies
    assert len(outcomes)==1


@pytest.mark.asyncio
async def test_gemini_async_cancel_closes_send_before_return(dripping,monkeypatch):
    from agent.gemini_native_adapter import GeminiNativeClient,AsyncGeminiNativeClient
    url,bodies,_=dripping;outcomes=govern(monkeypatch)
    sync=GeminiNativeClient(api_key='local',base_url=url,http_client=httpx.Client(trust_env=False))
    client=AsyncGeminiNativeClient(sync)
    try:
        task=asyncio.create_task(client.chat.completions.create(model='local',messages=[],max_tokens=7))
        while not bodies:
            await asyncio.sleep(.01)
        await asyncio.sleep(.06);task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(outcomes)==1 and outcomes[0]['outcome']=='interrupted'
    finally:
        await client.close();sync.close()


@pytest.mark.parametrize('provider',['anthropic','gemini','bedrock'])
def test_other_sync_providers_share_total_deadline(dripping,monkeypatch,provider):
    url,bodies,_=dripping;outcomes=govern(monkeypatch,send_timeout_ms=180)
    def run():
        if provider=='anthropic':
            import anthropic
            with bind_inference_client(anthropic.Anthropic(api_key='local',base_url=url,max_retries=2,
                    http_client=httpx.Client(trust_env=False))) as sdk:
                sdk.messages.create(model='local',messages=[],max_tokens=7)
        elif provider=='gemini':
            from agent.gemini_native_adapter import GeminiNativeClient
            with GeminiNativeClient(api_key='local',base_url=url,http_client=httpx.Client(trust_env=False)) as sdk:
                sdk.chat.completions.create(model='local',messages=[],max_tokens=7)
        else:
            boto3 = pytest.importorskip("boto3", reason="bedrock extra not installed")
            from botocore import UNSIGNED
            from botocore.config import Config
            from ares_runtime.physical_dispatch import bind_bedrock_client
            sdk=bind_bedrock_client(boto3.client('bedrock-runtime',region_name='us-east-1',endpoint_url=url,
                aws_access_key_id='local',aws_secret_access_key='local',
                config=Config(signature_version=UNSIGNED,retries={'max_attempts':2},proxies={})))
            try:
                sdk.converse(modelId='local',messages=[],inferenceConfig={'maxTokens':7})
            finally:
                sdk.close()
    with pytest.raises(middleware.EgressOutcomeUnrecorded) as caught:
        run()
    assert caught.value.code=='TRANSPORT_DEADLINE_EXCEEDED'
    assert len(bodies)==len(outcomes)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('provider',['anthropic','gemini'])
async def test_other_async_providers_share_total_deadline(dripping,monkeypatch,provider):
    url,bodies,_=dripping;outcomes=govern(monkeypatch,send_timeout_ms=180)
    if provider=='anthropic':
        import anthropic
        sdk=bind_inference_client(anthropic.AsyncAnthropic(api_key='local',base_url=url,max_retries=2,
            http_client=httpx.AsyncClient(trust_env=False)))
        async def run():
            return await sdk.messages.create(model='local',messages=[],max_tokens=7)
    else:
        from agent.gemini_native_adapter import GeminiNativeClient,AsyncGeminiNativeClient
        sync=GeminiNativeClient(api_key='local',base_url=url,http_client=httpx.Client(trust_env=False))
        sdk=AsyncGeminiNativeClient(sync)
        async def run():
            return await sdk.chat.completions.create(model='local',messages=[],max_tokens=7)
    try:
        with pytest.raises(middleware.EgressOutcomeUnrecorded) as caught:
            await run()
    finally:
        await sdk.close()
        if provider=='gemini':
            sync.close()
    assert caught.value.code=='TRANSPORT_DEADLINE_EXCEEDED'
    assert len(bodies)==len(outcomes)==1



def test_shorter_idle_timeout_is_unknown_not_relabelled_total(dripping,monkeypatch):
    url,bodies,mode=dripping;mode[0]='headers';outcomes=govern(monkeypatch)
    with bind_inference_client(openai.OpenAI(api_key='local',base_url=url,max_retries=2,
            http_client=httpx.Client(trust_env=False,timeout=.06))) as sdk:
        with pytest.raises(middleware.EgressOutcomeUnrecorded) as caught:
            sdk.chat.completions.create(model='x',messages=[],max_tokens=7)
    assert caught.value.code=='TRANSPORT_IO_UNCONFIRMED'
    assert len(bodies)==len(outcomes)==1



def test_partial_response_is_unknown_and_never_retried(dripping,monkeypatch):
    url,bodies,mode=dripping;mode[0]='truncate';outcomes=govern(monkeypatch)
    with bind_inference_client(openai.OpenAI(api_key='local',base_url=url,max_retries=2,
            http_client=httpx.Client(trust_env=False))) as sdk:
        with pytest.raises(middleware.EgressOutcomeUnrecorded) as caught:
            sdk.chat.completions.create(model='x',messages=[],max_tokens=7)
    assert caught.value.code=='TRANSPORT_IO_UNCONFIRMED'
    assert len(bodies)==len(outcomes)==1
    assert outcomes[0]['response']['response_bytes']==1
