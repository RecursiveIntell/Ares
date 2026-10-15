"""Generation-bound operation observations for explicitly owned progress ledgers.

The delegation owner installs the ledger in a ContextVar. SDK request owners
open/close operations and attest normalized responses; SDK chunk owners attest
nonempty deltas. Presentation callbacks, polling and retry ticks have no emitter.
This module never admits calls, changes routes, kills work or releases capacity.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import json

from hermes_constants import PARTIAL_STREAM_STUB_ID

from agent.progress_witness import ProgressWitness


class OperationProgressUnavailable(RuntimeError):
    pass


_OWNER = ContextVar('operation_progress_owner', default=None)
_REQUEST = ContextVar('operation_progress_request', default=None)


def _field(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _nonempty(value):
    return type(value) is str and bool(value.strip())


def _reject_constant(value):
    raise ValueError('nonfinite JSON constant')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON property')
        result[key] = value
    return result


def _validated_response(value):
    """Normalized SDK envelope validation, not tool/effect authorization."""
    try:
        # Runtime-synthesized recovery stubs (mid-stream drops) are not provider completions.
        if _field(value, 'id') == PARTIAL_STREAM_STUB_ID:
            return False
        choices = _field(value, 'choices')
        if type(choices) not in (list, tuple) or not choices:
            return False
        choice = choices[0]
        if _field(choice, 'finish_reason') not in ('stop', 'length', 'tool_calls', 'function_call', 'content_filter'):
            return False
        message = _field(choice, 'message')
        if _field(message, 'role') != 'assistant':
            return False
        if _nonempty(_field(message, 'content')) or _nonempty(_field(message, 'reasoning_content')):
            return True
        calls = _field(message, 'tool_calls')
        if type(calls) not in (list, tuple) or not calls:
            return False
        for call in calls:
            function = _field(call, 'function')
            if not _nonempty(_field(function, 'name')):
                return False
            arguments = _field(function, 'arguments')
            if not _nonempty(arguments):
                return False
            parsed = json.loads(arguments, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
            if type(parsed) is not dict:
                return False
        return True
    except (AttributeError, TypeError, ValueError, RecursionError):
        return False


@contextmanager
def progress_owner_scope(witness, generation):
    """Capture an owner, or clear inherited parent credit for an unqualified child."""
    if witness is not None:
        if type(witness) is not ProgressWitness or type(generation) is not str or not generation:
            raise OperationProgressUnavailable('OPERATION_PROGRESS_OWNER_INVALID')
        snapshot = witness.snapshot()
        if snapshot.generation != generation or snapshot.closed:
            raise OperationProgressUnavailable('OPERATION_PROGRESS_GENERATION_INVALID')
    elif generation is not None:
        raise OperationProgressUnavailable('OPERATION_PROGRESS_OWNER_INVALID')
    owner_token = _OWNER.set(None if witness is None else (witness, generation))
    request_token = _REQUEST.set(None)
    try:
        yield
    finally:
        _REQUEST.reset(request_token)
        _OWNER.reset(owner_token)


class _ModelRequestProgress:
    __slots__ = ('witness', 'generation', 'operation', '_completed', '_sdk_started')

    def __init__(self, witness, generation, operation):
        self.witness = witness
        self.generation = generation
        self.operation = operation
        self._completed = False
        self._sdk_started = False

    def complete_response(self, response):
        if not self._sdk_started or not _validated_response(response):
            return False
        accepted = self.witness.complete_request(self.operation, generation=self.generation)
        if accepted:
            self._completed = True
        return accepted

    def abandon(self):
        if not self._completed:
            self.witness.abandon_request(self.operation, generation=self.generation)


@contextmanager
def model_request_scope(operation, *, streaming):
    owner = _OWNER.get()
    request = None
    if owner is not None:
        witness, generation = owner
        if type(operation) is not str or not operation or type(streaming) is not bool:
            raise OperationProgressUnavailable('OPERATION_PROGRESS_REQUEST_INVALID')
        if not witness.open_request(operation, generation=generation, streaming=streaming):
            raise OperationProgressUnavailable('OPERATION_PROGRESS_REQUEST_REFUSED')
        request = _ModelRequestProgress(witness, generation, operation)
    token = _REQUEST.set(request)
    try:
        yield request
    finally:
        try:
            if request is not None:
                request.abandon()
        finally:
            _REQUEST.reset(token)


def observe_sdk_request_started():
    """Canonical factory reached; bookkeeping only, never progress credit."""
    request = _REQUEST.get()
    if request is None:
        return False
    request._sdk_started = True
    return True


def observe_chat_completion_delta(delta):
    """Called only after the canonical SDK stream's stale/writer fences."""
    request = _REQUEST.get()
    if request is None or not request._sdk_started:
        return False
    try:
        pieces = [_field(delta, name) for name in ('content', 'reasoning_content', 'reasoning')]
        calls = _field(delta, 'tool_calls')
        if type(calls) in (list, tuple):
            # Name/role metadata alone is not growing output. Argument deltas
            # come from the actual SDK stream, not a re-polled tool status.
            pieces.extend(_field(_field(call, 'function'), 'arguments') for call in calls)
        nbytes = sum(len(piece.encode('utf-8')) for piece in pieces if _nonempty(piece))
    except (AttributeError, TypeError, ValueError, UnicodeError):
        return False
    if not nbytes:
        return False
    return request.witness.record_model_delta(request.operation, nbytes, generation=request.generation)
