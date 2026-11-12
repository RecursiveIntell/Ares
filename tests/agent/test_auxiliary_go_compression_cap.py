"""OpenCode Go compression must not silently discard its requested output cap."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import asyncio
import json

import httpx
import openai

import pytest

from agent import auxiliary_client as ac


@pytest.mark.parametrize('limit', [100, 1040, 4000])
def test_go_compression_keeps_requested_cap(limit):
    kwargs = ac._build_call_kwargs(
        provider='opencode-go', model='glm-5.3-flash',
        base_url='https://opencode.ai/zen/go/v1', task='compression',
        messages=[{'role': 'user', 'content': 'synthetic'}], max_tokens=limit,
    )
    assert kwargs.get('max_tokens') == limit
    assert 'max_completion_tokens' not in kwargs


@pytest.mark.parametrize('provider,task', [
    ('opencode-go', 'title_generation'),
    ('opencode-go', None),
    ('openrouter', 'compression'),
])
def test_unrelated_openai_compatible_paths_remain_unchanged(provider, task):
    kwargs = ac._build_call_kwargs(
        provider=provider, model='glm-5.3-flash', base_url='https://example.invalid/v1',
        task=task, messages=[{'role': 'user', 'content': 'synthetic'}], max_tokens=100,
    )
    assert 'max_tokens' not in kwargs
    assert 'max_completion_tokens' not in kwargs


def test_sdk_wire_contains_cap_through_real_auxiliary_path():
    from ares_runtime.physical_dispatch import bind_inference_client

    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            'id': 'synthetic', 'object': 'chat.completion', 'created': 0,
            'model': 'glm-5.3-flash', 'choices': [{'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': 'synthetic response'}}],
        })
    http_client = httpx.Client(transport=httpx.MockTransport(respond), trust_env=False)
    sdk = bind_inference_client(openai.OpenAI(
        api_key='synthetic-not-a-real-key', base_url='https://opencode.ai/zen/go/v1',
        http_client=http_client, max_retries=0,
    ))
    try:
        with (patch.object(ac, '_ensure_auxiliary_context_qualified'),
              patch.object(ac, '_acquire_sync_aux_semaphore', return_value=None),
              patch.object(ac, '_resolve_task_provider_model', return_value=('opencode-go', 'glm-5.3-flash', '', '', '')),
              patch.object(ac, '_get_cached_client', return_value=(sdk, 'glm-5.3-flash')),
              patch.object(ac, '_effective_provider_for_client', return_value='opencode-go')):
            response = ac.call_llm(task='compression', provider='opencode-go', model='glm-5.3-flash',
                                   messages=[{'role': 'user', 'content': 'synthetic'}], max_tokens=1040)
    finally:
        sdk.close()
    assert response.choices[0].message.content == 'synthetic response'
    assert len(requests) == 1
    assert requests[0]['max_tokens'] == 1040
    assert 'max_completion_tokens' not in requests[0]


def test_async_rejected_cap_is_not_retried_without_cap():
    client = SimpleNamespace(base_url='https://opencode.ai/zen/go/v1')
    relay = AsyncMock(side_effect=RuntimeError('unsupported_parameter max_tokens'))
    with (patch.object(ac, '_ensure_auxiliary_context_qualified'),
          patch.object(ac, '_acquire_async_aux_semaphore', return_value=None),
          patch.object(ac, '_resolve_task_provider_model', return_value=('opencode-go', 'glm-5.3-flash', '', '', '')),
          patch.object(ac, '_get_cached_client', return_value=(client, 'glm-5.3-flash')),
          patch.object(ac, '_effective_provider_for_client', return_value='opencode-go'),
          patch.object(ac, '_relay_async_completion', relay)):
        with pytest.raises(RuntimeError, match='unsupported_parameter max_tokens'):
            asyncio.run(ac.async_call_llm(task='compression', provider='opencode-go', model='glm-5.3-flash',
                        messages=[{'role': 'user', 'content': 'synthetic'}], max_tokens=100))
    assert relay.await_count == 1
    assert relay.call_args.args[1]['max_tokens'] == 100


def test_rejected_cap_is_not_retried_without_cap():
    client = SimpleNamespace(base_url='https://opencode.ai/zen/go/v1')
    with (patch.object(ac, '_ensure_auxiliary_context_qualified'),
          patch.object(ac, '_acquire_sync_aux_semaphore', return_value=None),
          patch.object(ac, '_resolve_task_provider_model', return_value=('opencode-go', 'glm-5.3-flash', '', '', '')),
          patch.object(ac, '_get_cached_client', return_value=(client, 'glm-5.3-flash')),
          patch.object(ac, '_effective_provider_for_client', return_value='opencode-go'),
          patch.object(ac, '_relay_sync_completion', side_effect=RuntimeError('unsupported_parameter max_tokens')) as relay):
        with pytest.raises(RuntimeError, match='unsupported_parameter max_tokens'):
            ac.call_llm(task='compression', provider='opencode-go', model='glm-5.3-flash',
                        messages=[{'role': 'user', 'content': 'synthetic'}], max_tokens=100)
    assert relay.call_count == 1
    assert relay.call_args.args[1]['max_tokens'] == 100
