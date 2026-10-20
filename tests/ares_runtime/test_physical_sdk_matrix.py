"""Real installed transport conformance on isolated loopback only."""
import json
from types import SimpleNamespace

import httpx
import pytest
from blake3 import blake3

from tests.ares_runtime.test_physical_http_transport import endpoint  # noqa: F401
from ares_runtime.physical_dispatch import bind_bedrock_client
from hermes_cli import middleware


@pytest.fixture
def observed(monkeypatch):
    begins, settles = [], []
    def govern(*, payload, dispatch_phase, **kw):
        if dispatch_phase != 'physical_send':
            return None
        begins.append(payload)
        return SimpleNamespace(settle=lambda **kw: settles.append(kw))
    monkeypatch.setattr(middleware, '_get_middleware_callbacks', lambda _: [govern])
    return begins, settles


def verify(bodies, observed):
    begins, settles = observed
    assert len(bodies) == len(begins) == len(settles) > 0
    for body, observation in zip(bodies, begins):
        assert observation['provider_request_digest'] == blake3(body).hexdigest()
        assert observation['provider_request_bytes'] == len(body)
    return begins, settles


def test_main_canonical_openai_factory_and_callback(endpoint, observed):
    from agent.process_bootstrap import OpenAI
    from ares_runtime.continuity.runtime import context_dispatch_physical_call
    url, bodies, _ = endpoint
    with OpenAI(api_key='local', base_url=url, max_retries=0, http_client=httpx.Client(trust_env=False)) as client:
        agent = SimpleNamespace(session_id=None, provider='openai', model='local', api_mode='chat_completions')
        result = context_dispatch_physical_call(agent, {'model':'local','messages':[], 'max_tokens':7},
            lambda body: client.chat.completions.create(**body))
        assert result.choices[0].message.content == 'ok'
    verify(bodies, observed)


def test_anthropic_canonical_factory(endpoint, observed):
    from agent.anthropic_adapter import build_anthropic_client
    url, bodies, _ = endpoint
    with build_anthropic_client('local', base_url=url) as client:
        result = client.messages.create(model='local', messages=[{'role':'user','content':'hello'}], max_tokens=7)
        assert result.content[0].text == 'ok'
    verify(bodies, observed)


def test_gemini_canonical_factory(endpoint, observed):
    from agent.gemini_native_adapter import GeminiNativeClient
    url, bodies, _ = endpoint
    with GeminiNativeClient(api_key='local', base_url=url, http_client=httpx.Client(trust_env=False)) as client:
        result = client.chat.completions.create(model='local', messages=[{'role':'user','content':'hello'}], max_tokens=7)
        assert result.choices[0].message.content == 'ok'
    verify(bodies, observed)


@pytest.mark.asyncio
async def test_async_auxiliary_actual_sdk(endpoint, observed):
    from agent.auxiliary_client import _create_openai_client, _to_async_client, _relay_async_completion
    url, bodies, _ = endpoint
    sync = _create_openai_client(api_key='local', base_url=url, http_client=httpx.Client(trust_env=False))
    client, model = _to_async_client(sync, 'local')
    try:
        result = await _relay_async_completion(client, {'model':model, 'messages':[], 'max_tokens':7}, provider='openai', api_mode='chat_completions')
        assert result.choices[0].message.content == 'ok'
    finally:
        await client.close()
        sync.close()
    verify(bodies, observed)


def test_bedrock_converse_actual_botocore_retry(endpoint, observed):
    boto3 = pytest.importorskip("boto3", reason="bedrock extra not installed")
    from botocore import UNSIGNED
    from botocore.config import Config
    url, bodies, statuses = endpoint
    client = bind_bedrock_client(boto3.client('bedrock-runtime', region_name='us-east-1',
        endpoint_url=url, aws_access_key_id='local', aws_secret_access_key='local',
        config=Config(signature_version=UNSIGNED, retries={'max_attempts':1,'mode':'standard'}, proxies={})))
    statuses.extend([500, 200])
    try:
        result = client.converse(modelId='local', messages=[{'role':'user', 'content':[{'text':'hello'}]}], inferenceConfig={'maxTokens':7})
        assert result['output']['message']['content'][0]['text'] == 'ok'
    finally:
        client.close()
    begins, _ = verify(bodies, observed)
    assert len(begins) == 2
    assert begins[0]['dispatch_intent_ref'] != begins[1]['dispatch_intent_ref']


@pytest.mark.parametrize('kind', ['deny', 'settle'])
def test_bedrock_typed_failure_zero_or_one_send(endpoint, monkeypatch, kind):
    boto3 = pytest.importorskip("boto3", reason="bedrock extra not installed")
    from botocore import UNSIGNED
    from botocore.config import Config
    url, bodies, _ = endpoint
    original = middleware.EgressDispatchDenied('DENIED') if kind == 'deny' else middleware.EgressOutcomeUnrecorded()
    def govern(**kwargs):
        if kind == 'deny':
            raise original
        def settle(**kw):
            raise original
        return SimpleNamespace(settle=settle)
    monkeypatch.setattr(middleware, '_get_middleware_callbacks', lambda _: [govern])
    client = bind_bedrock_client(boto3.client('bedrock-runtime', region_name='us-east-1',
        endpoint_url=url, aws_access_key_id='local', aws_secret_access_key='local',
        config=Config(signature_version=UNSIGNED, retries={'max_attempts':2,'mode':'standard'}, proxies={})))
    try:
        with pytest.raises(type(original)) as caught:
            client.converse(modelId='local', messages=[], inferenceConfig={'maxTokens':7})
        assert caught.value is original
    finally:
        client.close()
    assert len(bodies) == (0 if kind == 'deny' else 1)



def test_bedrock_policy_denial_cannot_trigger_iam_stream_fallback(endpoint,monkeypatch):
    boto3 = pytest.importorskip("boto3", reason="bedrock extra not installed")
    from botocore import UNSIGNED
    from botocore.config import Config
    from agent import bedrock_adapter
    url,bodies,_=endpoint;begins=[]
    sdk=bind_bedrock_client(boto3.client('bedrock-runtime',region_name='us-east-1',endpoint_url=url,
        aws_access_key_id='local',aws_secret_access_key='local',
        config=Config(signature_version=UNSIGNED,retries={'max_attempts':2},proxies={})))
    monkeypatch.setattr(bedrock_adapter,'_get_bedrock_runtime_client',lambda _:sdk)
    error=middleware.EgressDispatchDenied('AccessDeniedException')
    def deny(**kw):
        begins.append(kw)
        raise error
    monkeypatch.setattr(middleware,'_get_middleware_callbacks',lambda _: [deny])
    try:
        with pytest.raises(middleware.EgressDispatchDenied) as caught:
            bedrock_adapter.call_converse_stream('us-east-1','local',[],max_tokens=7)
    finally:
        sdk.close()
    assert caught.value is error
    assert len(begins)==1
    assert not bodies
