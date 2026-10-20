"""Real chat-completions dispatch path under an explicitly owned progress ledger.

The SDK client is a neutral fake; the code under test is the production
_dispatch_nonstreaming_api_request seam. No provider, network or live config.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.chat_completion_helpers import _dispatch_nonstreaming_api_request
from agent.operation_progress import progress_owner_scope
from agent.progress_witness import InactivityLimits, ProgressWitness

KWARGS = {'model': 'neutral', 'messages': [{'role': 'user', 'content': 'hi'}]}


def agent():
    return SimpleNamespace(api_mode='chat_completions', provider='neutral-provider')


def good_response():
    message = SimpleNamespace(role='assistant', content='validated reply', tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=message)])


def client_returning(value, seen=None):
    def create(**kwargs):
        if seen is not None:
            seen.append(kwargs)
        if isinstance(value, BaseException):
            raise value
        return value
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


@pytest.fixture
def witness():
    now = [0.0]
    value = ProgressWitness(generation='g', limits=InactivityLimits(5, 10, 12), clock=lambda: now[0])
    return value, now


def dispatch(client):
    with patch('agent.chat_completion_helpers._should_use_ri_pipeline', return_value=False):
        return _dispatch_nonstreaming_api_request(agent(), dict(KWARGS), make_client=lambda *_: client)


def test_validated_nonstreaming_response_is_credited_once_and_request_closed(witness):
    value, now = witness
    now[0] = 2
    response = good_response()
    with progress_owner_scope(value, 'g'):
        assert dispatch(client_returning(response)) is response
    snapshot = value.snapshot()
    assert snapshot.accepted == {'model.complete': 1}
    assert snapshot.open_requests == 0
    assert snapshot.last_progress_at == 2


def test_sdk_exception_gets_no_credit_and_does_not_reset_stall(witness):
    value, now = witness
    with progress_owner_scope(value, 'g'):
        for _ in range(2):
            now[0] += 3
            with pytest.raises(OSError):
                dispatch(client_returning(OSError('neutral transport failure')))
    assert value.snapshot().accepted == {}
    assert value.snapshot().open_requests == 0
    assert value.evaluate().state == 'stalled'


@pytest.mark.parametrize('bad', [None, SimpleNamespace(choices=[]), SimpleNamespace(id='no-choices')])
def test_unvalidated_sdk_return_is_returned_unchanged_but_never_credited(witness, bad):
    value, _ = witness
    with progress_owner_scope(value, 'g'):
        assert dispatch(client_returning(bad)) is bad
    assert value.snapshot().accepted == {}
    assert value.snapshot().open_requests == 0


def test_without_owner_dispatch_is_byte_for_byte_passthrough():
    seen = []
    response = good_response()
    assert dispatch(client_returning(response, seen)) is response
    assert seen == [KWARGS]


def test_stalled_ledger_during_open_sdk_request_is_governed_by_request_limit_not_stall(witness):
    value, now = witness
    with progress_owner_scope(value, 'g'):
        def hang_then_check(**kwargs):
            now[0] = 6   # beyond stall budget; nonstreaming request is governed by its own limit
            assert value.evaluate().state == 'ok'
            now[0] = 11
            assert value.evaluate().state == 'request_limit'
            return good_response()
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=hang_then_check)))
        dispatch(client)
