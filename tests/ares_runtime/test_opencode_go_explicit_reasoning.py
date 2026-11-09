"""Go/GLM explicit intent at the actual SDK physical-send boundary; offline only."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from openai import OpenAI
import pytest

from ares_runtime.physical_dispatch import bind_http_client, explicit_reasoning_request_scope, inference_call_scope
from hermes_cli.middleware import EgressDispatchDenied
from providers import get_provider_profile

MODEL = 'glm-5.3-flash'


def typed_denial(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, EgressDispatchDenied):
            return error
        error = error.__cause__ or error.__context__
    return None


def send(expected, *, model=MODEL):
    expected = deepcopy(expected)
    from agent import reasoning_effort
    from ares_runtime import physical_dispatch
    root = Path(__file__).resolve().parents[2]
    assert Path(reasoning_effort.__file__).resolve().is_relative_to(root)
    assert Path(physical_dispatch.__file__).resolve().is_relative_to(root)
    profile = get_provider_profile('opencode-go')
    assert profile is not None
    assert Path(type(profile).build_api_kwargs_extras.__code__.co_filename).resolve().is_relative_to(root)
    extra, top = profile.build_api_kwargs_extras(reasoning_config=expected, supports_reasoning=True, model=model)
    sent = []
    def receive(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={'id': 'offline-go', 'object': 'chat.completion', 'created': 1,
            'model': model, 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'fixture'}, 'finish_reason': 'stop'}]})
    owner = SimpleNamespace(session_id='offline-go-explicit', model=model, provider='opencode-go', api_mode='chat_completions')
    messages = [{'role': 'user', 'content': 'synthetic fixture'}]
    logical = {'model': model, 'max_tokens': 16, 'messages': messages, **extra, **top}
    error, result = None, None
    with bind_http_client(httpx.Client(transport=httpx.MockTransport(receive), trust_env=False)) as client:
        sdk = OpenAI(api_key='synthetic-offline', base_url='https://fixture.invalid/v1', http_client=client, max_retries=0)
        try:
            with explicit_reasoning_request_scope(owner, expected), inference_call_scope(owner, logical):
                result = sdk.chat.completions.create(model=model, messages=messages, max_tokens=16, extra_body=extra, **top)
        except Exception as caught:
            error = caught
        finally:
            sdk.close()
    return result, sent, error


def test_supported_high_reaches_physical_wire_without_alias_controls():
    result, sent, error = send({'enabled': True, 'effort': 'high'})
    assert error is None, repr(error)
    assert result.choices[0].message.content == 'fixture'
    assert len(sent) == 1
    assert sent[0]['reasoning_effort'] == 'high'
    assert 'reasoning' not in sent[0]


@pytest.mark.parametrize('effort', ['medium', 'xhigh'])
def test_provider_clamp_is_not_exact_invocation_equivalence(effort):
    _, sent, error = send({'enabled': True, 'effort': effort})
    denial = typed_denial(error)
    assert denial is not None, repr(error)
    assert denial.code == 'EXPLICIT_REASONING_MISMATCH'
    assert sent == []


def test_unrepresented_explicit_disable_cannot_mean_relay_disabled():
    _, sent, error = send({'enabled': False})
    denial = typed_denial(error)
    assert denial is not None, repr(error)
    assert denial.code == 'EXPLICIT_REASONING_MISMATCH'
    assert sent == []


def test_unqualified_future_model_cannot_inherit_flash_high_permission():
    _, sent, error = send({'enabled': True, 'effort': 'high'}, model='glm-5.4-future')
    denial = typed_denial(error)
    assert denial is not None, repr(error)
    assert denial.code == 'EXPLICIT_REASONING_MISMATCH'
    assert sent == []
