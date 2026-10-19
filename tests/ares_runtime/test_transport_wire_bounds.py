"""Wire identity is independent of logical JSON projections and SDK kwargs."""
import json
from types import SimpleNamespace

from blake3 import blake3
import httpx
import pytest

from ares_runtime.physical_dispatch import bind_http_client
from hermes_cli import middleware
from tests.ares_runtime.test_physical_http_transport import endpoint  # noqa: F401
from tests.ares_runtime.test_transport_lease import lease


@pytest.mark.parametrize('body',[
    b'{"max_tokens":8}',b'{"max_tokens":7,"max_completion_tokens":7}',
    b'{"max_tokens":true}',b'{"max_tokens":7,"max_tokens":1}',
    b'{"max_tokens":7,"n":true}',b'{"max_tokens":7,"n":2}',
    b'{"max_tokens":7.0}',b'{}',b'{"max_tokens":null}',
])
def test_invalid_actual_output_field_cannot_be_repaired(endpoint,monkeypatch,body):
    url,bodies,_=endpoint;settles=[]
    monkeypatch.setattr(middleware,'_get_middleware_callbacks',lambda _: [lambda **kw:
        SimpleNamespace(transport_lease=lease(),settle=lambda **kw:settles.append(kw))])
    with bind_http_client(httpx.Client(trust_env=False)) as client:
        with pytest.raises(middleware.EgressDispatchDenied):
            client.post(url+'/chat/completions',content=body)
    assert not bodies
    assert len(settles)==1 and settles[0]['outcome']=='denied_by_peer'


@pytest.mark.parametrize('provider',['openai','anthropic','gemini','responses','bedrock'])
def test_off_enforce_exact_sdk_body_and_hash(endpoint,monkeypatch,provider):
    import openai,anthropic
    if provider=='bedrock':
        boto3 = pytest.importorskip("boto3", reason="bedrock extra not installed")
        from botocore import UNSIGNED
        from botocore.config import Config
    from ares_runtime.physical_dispatch import bind_inference_client,bind_bedrock_client
    from agent.gemini_native_adapter import GeminiNativeClient
    url,bodies,_=endpoint;observations=[];settles=[]
    if provider=='bedrock':
        sdk=bind_bedrock_client(boto3.client('bedrock-runtime',region_name='us-east-1',endpoint_url=url,
            aws_access_key_id='local',aws_secret_access_key='local',config=Config(signature_version=UNSIGNED,proxies={},retries={'max_attempts':0})))
        call=lambda:sdk.converse(modelId='local',messages=[],inferenceConfig={'maxTokens':7})
    elif provider=='gemini':
        sdk=GeminiNativeClient(api_key='local',base_url=url,http_client=httpx.Client(trust_env=False))
        call=lambda:sdk.chat.completions.create(model='local',messages=[],max_tokens=7)
    elif provider=='anthropic':
        sdk=bind_inference_client(anthropic.Anthropic(api_key='local',base_url=url,max_retries=0,http_client=httpx.Client(trust_env=False)))
        call=lambda:sdk.messages.create(model='local',messages=[],max_tokens=7)
    else:
        sdk=bind_inference_client(openai.OpenAI(api_key='local',base_url=url,max_retries=0,http_client=httpx.Client(trust_env=False)))
        call=(lambda:sdk.responses.create(model='local',input='hello',max_output_tokens=7)) if provider=='responses' else (lambda:sdk.chat.completions.create(model='local',messages=[],max_tokens=7))
    try:
        monkeypatch.setattr(middleware,'_get_middleware_callbacks',lambda _: [])
        call()
        def govern(*,payload,**kw):
            observations.append(payload)
            return SimpleNamespace(transport_lease=lease(),settle=lambda **kw:settles.append(kw))
        monkeypatch.setattr(middleware,'_get_middleware_callbacks',lambda _: [govern])
        call()
    finally:
        sdk.close()
    assert len(bodies)==2 and bodies[0]==bodies[1]
    assert len(observations)==len(settles)==1
    assert observations[0]['provider_request_digest']==blake3(bodies[0]).hexdigest()
    assert observations[0]['provider_request_bytes']==len(bodies[0])
    assert settles[0]['outcome']=='ok'
