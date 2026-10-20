"""Closed lease admission uses real SDK sends on isolated loopback."""
import time
from types import SimpleNamespace

import pytest

from tests.ares_runtime.test_physical_http_transport import endpoint, client, invoke  # noqa: F401
from hermes_cli import middleware


def lease(**overrides):
    value = dict(schema_version='ares.transport-lease/v1',
                 expires_at_unix_ms=int(time.time() * 1000) + 10000,
                 send_timeout_ms=8000, request_bytes_limit=10000,
                 output_tokens_limit=7)
    value.update(overrides)
    return value


def test_useful_bounded_sync_request_is_admitted_without_body_rewrite(endpoint, monkeypatch):
    url, bodies, _ = endpoint
    monkeypatch.setattr(middleware, '_get_middleware_callbacks', lambda _: [])
    with client(url) as sdk:
        invoke(sdk)
    original = bodies[0]
    results = []
    def govern(**kw):
        return SimpleNamespace(transport_lease=lease(), settle=lambda **kw: results.append(kw))
    monkeypatch.setattr(middleware, '_get_middleware_callbacks', lambda _: [govern])
    with client(url) as sdk:
        assert invoke(sdk).choices[0].message.content == 'ok'
    assert bodies == [original, original]
    assert len(results) == 1
    assert results[0]['outcome'] == 'ok'


@pytest.mark.parametrize('overrides,code', [
    ({'extra':1}, 'INVALID_TRANSPORT_LEASE'),
    ({'send_timeout_ms':True}, 'INVALID_TRANSPORT_LEASE'),
    ({'output_tokens_limit':2**63}, 'INVALID_TRANSPORT_LEASE'),
    ({'output_tokens_limit':6}, 'OUTPUT_TOKENS_EXCEEDED'),
    ({'request_bytes_limit':1}, 'REQUEST_BYTES_EXCEEDED'),
    ({'expires_at_unix_ms':1}, 'TRANSPORT_LEASE_EXPIRED'),
])
def test_precise_presend_denial_has_zero_network(endpoint, monkeypatch, overrides, code):
    url,bodies,_=endpoint;settles=[]
    monkeypatch.setattr(middleware,'_get_middleware_callbacks',lambda _: [lambda **kw:
        SimpleNamespace(transport_lease=lease(**overrides),settle=lambda **kw:settles.append(kw))])
    with client(url,retries=2) as sdk, pytest.raises(middleware.EgressDispatchDenied) as caught:
        invoke(sdk)
    assert caught.value.code==code
    assert not bodies
    assert len(settles)==1 and settles[0]['outcome']=='denied_by_peer'


@pytest.mark.parametrize('provider',['anthropic','gemini','bedrock','responses'])
def test_real_sdk_lease_paths(endpoint,monkeypatch,provider):
    import httpx
    from ares_runtime.physical_dispatch import bind_bedrock_client,bind_inference_client
    url,bodies,_=endpoint;settles=[]
    monkeypatch.setattr(middleware,'_get_middleware_callbacks',lambda _: [lambda **kw:
        SimpleNamespace(transport_lease=lease(),settle=lambda **kw:settles.append(kw))])
    if provider=='anthropic':
        from agent.anthropic_adapter import build_anthropic_client
        with build_anthropic_client('local',base_url=url) as sdk:
            sdk.messages.create(model='local',messages=[],max_tokens=7)
    elif provider=='gemini':
        from agent.gemini_native_adapter import GeminiNativeClient
        with GeminiNativeClient(api_key='local',base_url=url,http_client=httpx.Client(trust_env=False)) as sdk:
            sdk.chat.completions.create(model='local',messages=[],max_tokens=7)
    elif provider=='responses':
        with client(url) as sdk:
            sdk.responses.create(model='local',input='hello',max_output_tokens=7)
    else:
        boto3 = pytest.importorskip("boto3", reason="bedrock extra not installed")
        from botocore import UNSIGNED
        from botocore.config import Config
        sdk=bind_bedrock_client(boto3.client('bedrock-runtime',region_name='us-east-1',endpoint_url=url,
            aws_access_key_id='local',aws_secret_access_key='local',
            config=Config(signature_version=UNSIGNED,retries={'max_attempts':0},proxies={})))
        try:
            sdk.converse(modelId='local',messages=[],inferenceConfig={'maxTokens':7})
        finally:
            sdk.close()
    assert len(bodies)==len(settles)==1
    assert settles[0]['outcome']=='ok'


@pytest.mark.asyncio
@pytest.mark.parametrize('provider',['openai','anthropic'])
async def test_async_sdk_leases(endpoint,monkeypatch,provider):
    import httpx,openai,anthropic
    from ares_runtime.physical_dispatch import bind_inference_client
    url,bodies,_=endpoint;settles=[]
    monkeypatch.setattr(middleware,'_get_middleware_callbacks',lambda _: [lambda **kw:
        SimpleNamespace(transport_lease=lease(),settle=lambda **kw:settles.append(kw))])
    factory=openai.AsyncOpenAI if provider=='openai' else anthropic.AsyncAnthropic
    async with bind_inference_client(factory(api_key='local',base_url=url,max_retries=0,
            http_client=httpx.AsyncClient(trust_env=False))) as sdk:
        resource=sdk.chat.completions if provider=='openai' else sdk.messages
        await resource.create(model='local',messages=[],max_tokens=7)
    assert len(bodies)==len(settles)==1 and settles[0]['outcome']=='ok'



def test_private_network_contract_version_drift_refuses_before_send(endpoint,monkeypatch):
    import httpcore
    from tests.ares_runtime.test_transport_deadline import govern
    url,bodies,_=endpoint;settles=govern(monkeypatch)
    monkeypatch.setattr(httpcore,'__version__','unqualified')
    with client(url,retries=2) as sdk,pytest.raises(middleware.EgressDispatchDenied,match='UNSUPPORTED_TRANSPORT'):
        invoke(sdk)
    assert not bodies
    assert len(settles)==1 and settles[0]['outcome']=='denied_by_peer'
