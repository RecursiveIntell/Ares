"""Real AIAgent streaming path (worker thread + Relay + SDK-chunk fences) under an owned progress ledger.

Neutral fake SDK stream only: no provider, network or live config. Proves the
ContextVar owner crosses the streaming worker thread and that only validated,
non-empty deltas from the canonical stream earn credit.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.operation_progress import progress_owner_scope
from agent.progress_witness import InactivityLimits, ProgressWitness
from tests.run_agent.test_streaming import _make_empty_chunk, _make_stream_chunk


def make_agent():
    from run_agent import AIAgent
    agent = AIAgent(api_key='test-key', base_url='https://neutral.invalid/v1', model='neutral-model',
                    provider='custom', quiet_mode=True, skip_context_files=True, skip_memory=True)
    agent.api_mode = 'chat_completions'
    agent._interrupt_requested = False
    return agent


def ledger():
    now = [0.0]
    return ProgressWitness(generation='g', limits=InactivityLimits(5, 10, 12),
                           clock=lambda: now[0]), now


def stream_of(chunks):
    client = MagicMock()
    client.chat.completions.create.return_value = iter(chunks)
    return client


@patch('run_agent.AIAgent._create_request_openai_client')
@patch('run_agent.AIAgent._close_request_openai_client')
def test_validated_stream_credits_deltas_and_completion_across_worker_thread(mock_close, mock_create):
    chunks = [_make_stream_chunk(content='Hello'), _make_stream_chunk(content=' world'),
              _make_stream_chunk(content='!', finish_reason='stop', model='neutral-model'),
              _make_empty_chunk(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=3))]
    mock_create.return_value = stream_of(chunks)
    agent = make_agent()
    witness, _ = ledger()
    with progress_owner_scope(witness, 'g'):
        response = agent._interruptible_streaming_api_call({})
    assert response.choices[0].message.content == 'Hello world!'
    snapshot = witness.snapshot()
    assert snapshot.accepted.get('model.delta') == 3
    assert snapshot.accepted.get('model.complete') == 1
    assert snapshot.open_requests == 0


@patch('run_agent.AIAgent._create_request_openai_client')
@patch('run_agent.AIAgent._close_request_openai_client')
def test_role_only_empty_and_usage_frames_earn_no_delta_credit(mock_close, mock_create):
    role_only = SimpleNamespace(choices=[SimpleNamespace(index=0, finish_reason=None, delta=SimpleNamespace(
        content=None, tool_calls=None, reasoning_content=None, reasoning=None, role='assistant'))],
        model=None, usage=None)
    chunks = [role_only, _make_stream_chunk(content=''), _make_stream_chunk(content='\n'),
              _make_empty_chunk(), _make_stream_chunk(content='ok', finish_reason='stop', model='neutral-model')]
    mock_create.return_value = stream_of(chunks)
    agent = make_agent()
    witness, _ = ledger()
    with progress_owner_scope(witness, 'g'):
        agent._interruptible_streaming_api_call({})
    assert witness.snapshot().accepted.get('model.delta') == 1


@patch('run_agent.AIAgent._create_request_openai_client')
@patch('run_agent.AIAgent._close_request_openai_client')
def test_stream_without_owner_is_unchanged_and_creates_no_ledger_state(mock_close, mock_create):
    mock_create.return_value = stream_of([
        _make_stream_chunk(content='plain', finish_reason='stop', model='neutral-model')])
    agent = make_agent()
    response = agent._interruptible_streaming_api_call({})
    assert response.choices[0].message.content == 'plain'


@patch('run_agent.AIAgent._create_request_openai_client')
@patch('run_agent.AIAgent._close_request_openai_client')
def test_dropped_stream_stub_is_never_credited_as_a_completed_response(mock_close, mock_create):
    """The runtime converts a mid-stream drop into a synthesized length-truncated stub.

    That stub is not provider-validated completion; only the real deltas that
    preceded the drop may count, never model.complete.
    """
    from hermes_constants import PARTIAL_STREAM_STUB_ID

    def exploding():
        yield _make_stream_chunk(content='partial')
        raise OSError('neutral stream failure')
    client = MagicMock()
    client.chat.completions.create.return_value = exploding()
    mock_create.return_value = client
    agent = make_agent()
    witness, _ = ledger()
    with progress_owner_scope(witness, 'g'):
        response = agent._interruptible_streaming_api_call({})
    assert response.id == PARTIAL_STREAM_STUB_ID
    snapshot = witness.snapshot()
    assert 'model.complete' not in snapshot.accepted
    assert snapshot.accepted.get('model.delta') == 1
    assert snapshot.open_requests == 0


@patch('run_agent.AIAgent._create_request_openai_client')
@patch('run_agent.AIAgent._close_request_openai_client')
def test_text_only_drop_without_finish_reason_is_not_a_provider_validated_completion(mock_close, mock_create):
    """Stream ends cleanly with text but NO finish_reason: the runtime synthesizes a
    length-truncated stub. A stub is a recovery artifact, never model.complete credit."""
    from hermes_constants import PARTIAL_STREAM_STUB_ID
    mock_create.return_value = stream_of([_make_stream_chunk(content='half a thought')])
    agent = make_agent()
    witness, _ = ledger()
    with progress_owner_scope(witness, 'g'):
        response = agent._interruptible_streaming_api_call({})
    assert response.id == PARTIAL_STREAM_STUB_ID
    snapshot = witness.snapshot()
    assert snapshot.accepted.get('model.delta') == 1
    assert 'model.complete' not in snapshot.accepted
    assert snapshot.open_requests == 0
