"""Canonical SDK progress scopes: operation observations are not authority."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent.operation_progress import (
    OperationProgressUnavailable, progress_owner_scope, model_request_scope,
    observe_chat_completion_delta, observe_sdk_request_started,
)
from agent.progress_witness import InactivityLimits, ProgressWitness


@pytest.fixture
def progress():
    now = [0.0]
    witness = ProgressWitness(generation='owned',
        limits=InactivityLimits(5, 10, 12), clock=lambda: now[0])
    return witness, now


def response(content='validated text', role='assistant', finish='stop', calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
        message=SimpleNamespace(role=role, content=content, tool_calls=calls))])


@contextmanager
def stubbed_sdk_request_scope(*args, **kwargs):
    """Unit-fixture SDK factory marker; real transport witnesses are separate."""
    with model_request_scope(*args, **kwargs) as request:
        observe_sdk_request_started()
        yield request


def test_interceptor_only_response_has_no_sdk_progress_credit(progress):
    witness, _ = progress
    with progress_owner_scope(witness, 'owned'):
        with model_request_scope('not-physically-started', streaming=True) as request:
            assert observe_chat_completion_delta(SimpleNamespace(content='interceptor text')) is False
            assert request.complete_response(response()) is False
    assert witness.snapshot().accepted == {}


def test_sdk_start_is_bookkeeping_not_progress(progress):
    witness, now = progress
    with progress_owner_scope(witness, 'owned'):
        with stubbed_sdk_request_scope('stream', streaming=True):
            now[0] = 6
            assert witness.snapshot().accepted == {}
            assert witness.evaluate().state == 'stalled'


def test_no_owner_does_not_interpret_or_modify_response():
    with progress_owner_scope(None, None):
        with stubbed_sdk_request_scope('operation', streaming=False) as request:
            assert request is None
            assert observe_chat_completion_delta(object()) is False


def test_valid_model_response_is_progress_and_closes_actual_request(progress):
    witness, now = progress
    with progress_owner_scope(witness, 'owned'):
        with stubbed_sdk_request_scope('operation', streaming=False) as request:
            assert witness.snapshot().open_requests == 1
            assert witness.snapshot().last_progress_at is None
            now[0] = 3
            assert request.complete_response(response()) is True
    snapshot = witness.snapshot()
    assert snapshot.open_requests == 0
    assert snapshot.accepted == {'model.complete': 1}
    assert snapshot.last_progress_at == 3


@pytest.mark.parametrize('value', [None, response(content=''), response(content='   '),
    response(role='user'), response(finish=None), object()])
def test_unvalidated_response_never_counts_and_operation_is_abandoned(progress, value):
    witness, _ = progress
    with progress_owner_scope(witness, 'owned'):
        with stubbed_sdk_request_scope('operation', streaming=False) as request:
            assert request.complete_response(value) is False
    assert witness.snapshot().accepted == {}
    assert witness.snapshot().open_requests == 0


def test_exception_and_retry_do_not_reset_stall_clock(progress):
    witness, now = progress
    with progress_owner_scope(witness, 'owned'):
        for operation in ('attempt-1', 'attempt-2'):
            with pytest.raises(OSError):
                with stubbed_sdk_request_scope(operation, streaming=False):
                    now[0] += 3
                    raise OSError('neutral failed SDK call')
    assert witness.snapshot().open_requests == 0
    assert witness.snapshot().accepted == {}
    assert witness.evaluate().state == 'stalled'


@pytest.mark.parametrize('delta', [SimpleNamespace(role='assistant'),
    SimpleNamespace(content=''), SimpleNamespace(content='\n'),
    SimpleNamespace(content=None), SimpleNamespace(tool_calls=[])])
def test_role_keepalive_and_empty_chunks_are_not_progress(progress, delta):
    witness, _ = progress
    with progress_owner_scope(witness, 'owned'):
        with stubbed_sdk_request_scope('stream', streaming=True):
            assert observe_chat_completion_delta(delta) is False
    assert witness.snapshot().accepted == {}


def test_validated_text_and_reasoning_deltas_are_counted_in_request_scope(progress):
    witness, _ = progress
    with progress_owner_scope(witness, 'owned'):
        with stubbed_sdk_request_scope('stream', streaming=True):
            assert observe_chat_completion_delta(SimpleNamespace(content='text'))
            assert observe_chat_completion_delta(SimpleNamespace(reasoning_content='reasoning'))
    assert witness.snapshot().accepted == {'model.delta': 2}
    assert observe_chat_completion_delta(SimpleNamespace(content='late')) is False


def test_total_child_scope_never_borrows_parent_request_credit(progress):
    witness, _ = progress
    with progress_owner_scope(witness, 'owned'):
        with stubbed_sdk_request_scope('parent', streaming=True):
            with progress_owner_scope(None, None):
                assert observe_chat_completion_delta(SimpleNamespace(content='foreign child text')) is False
            assert observe_chat_completion_delta(SimpleNamespace(content='owned text')) is True
    assert witness.snapshot().accepted == {'model.delta': 1}


def test_generation_mismatch_refuses_before_request_and_foreign_witness_untouched(progress):
    witness, _ = progress
    with pytest.raises(OperationProgressUnavailable):
        with progress_owner_scope(witness, 'foreign'):
            pytest.fail('foreign generation was entered')
    assert witness.snapshot().open_requests == 0
    assert witness.snapshot().closed is False


def test_closed_generation_never_credits_late_sdk_result(progress):
    witness, _ = progress
    with progress_owner_scope(witness, 'owned'):
        with stubbed_sdk_request_scope('stream', streaming=True) as request:
            witness.close()
            assert observe_chat_completion_delta(SimpleNamespace(content='late')) is False
            assert request.complete_response(response()) is False
    assert witness.snapshot().accepted == {}
