"""Per-inference-client exact-body observation and explicit-request equality.

The SDK invocation seam and this physical-send seam are distinct phases.
A native governor must consume only physical_send for physical permits. Missing
lineage is explicitly represented, never converted into a synthetic session.
"""

from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass
import hashlib
import inspect
import time
import types
import uuid
import weakref
from typing import Any, NoReturn
from ares_runtime.transport_lease import SendBudget


class _DispatchTickets(list[Any]):
    budget: SendBudget | None = None
    started: float


_bound_sdk = weakref.WeakSet()
_bound_http = weakref.WeakSet()

import httpx

from hermes_cli.middleware import (
    EgressDispatchDenied,
    EgressOutcomeUnrecorded,
    begin_llm_dispatch,
    settle_llm_dispatch,
    _dispatch_error_outcome,
    _dispatch_error_settlement,
)

_call: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("ares_inference_call", default=None)
_capture: contextvars.ContextVar[PhysicalDispatchCapture | None] = contextvars.ContextVar("ares_physical_capture", default=None)
_sdk_capture_tickets: contextvars.ContextVar[Any] = contextvars.ContextVar("ares_sdk_capture_tickets", default=None)
_capture_handoff: contextvars.ContextVar[Any] = contextvars.ContextVar("ares_capture_handoff", default=None)


@dataclass(frozen=True)
class _ExplicitReasoningRequest:
    owner: object
    enabled: bool
    effort: str | None


_explicit_reasoning: contextvars.ContextVar[_ExplicitReasoningRequest | None] = contextvars.ContextVar(
    "ares_explicit_reasoning_request", default=None)


@contextlib.contextmanager
def explicit_reasoning_request_scope(agent, config):
    """Bind normalized invocation data, not policy authority, to this agent.

    The shared logical producer carries only normalized request data into its
    deferred call basis. No ambient Context, configuration or authority is
    copied. Other logical owners do not inherit the request.
    """
    if config is None:
        yield
        return
    if agent is None:
        raise EgressDispatchDenied("EXPLICIT_REASONING_UNQUALIFIED", "explicit request requires its logical owner")
    from hermes_constants import parse_reasoning_effort

    valid = (type(config) is dict and type(config.get("enabled")) is bool)
    effort = config.get("effort") if valid and config["enabled"] else None
    canonical = parse_reasoning_effort(effort if effort is not None else "none")
    if not valid or canonical != config:
        raise EgressDispatchDenied("EXPLICIT_REASONING_UNQUALIFIED", "normalized explicit invocation required")
    token = _explicit_reasoning.set(_ExplicitReasoningRequest(agent, config["enabled"], effort))
    try:
        yield
    finally:
        _explicit_reasoning.reset(token)


def explicit_reasoning_override(agent):
    """Detached override for the bound owner's configured fallback transition."""
    request = _explicit_reasoning.get()
    if request is None or request.owner is not agent:
        return None
    return ({"enabled": True, "effort": request.effort} if request.enabled else {"enabled": False})


class PhysicalDispatchCapture:
    """Run-local, detached evidence; no plugin registration or durable state."""

    def __init__(self):
        from threading import RLock

        self._lock = RLock()
        self._records = []
        self._handoffs = {}
        self._handoff_conflict = False

    def evidence_snapshot(self):
        """One detached cut of registered observer work, never universal coverage."""
        from copy import deepcopy

        with self._lock:
            requests = deepcopy(self._records)
            handoffs = deepcopy(list(self._handoffs.values()))
            return {"requests": requests, "handoffs": handoffs,
                    "handoff_conflict": self._handoff_conflict,
                    "registered_work_settled": (
                        not self._handoff_conflict
                        and all(item["state"] in {"returned", "raised"} for item in handoffs)
                        and all(item["outcome"] is not None for item in requests))}

    def snapshot(self):
        from copy import deepcopy

        with self._lock:
            return deepcopy(self._records)

    def begin(self, payload):
        from copy import deepcopy

        fields = ("dispatch_intent_ref", "execution_id", "qualification_error",
                  "provider_request_digest", "provider_request_sha256", "provider_request_bytes",
                  "resolved_route", "route_qualification_error", "output_token_ceiling",
                  "output_bound_error", "native_controls", "controls_qualification_error", "explicit_reasoning_request")
        record = {key: deepcopy(payload.get(key)) for key in fields}
        record.update(schema="ares.physical-capture/v1", outcome=None, response=None,
                      error_type=None, error_code=None, provider_response_model=None)
        with self._lock:
            binding = _capture_handoff.get()
            record["capture_handoff_id"] = (
                binding[1] if binding is not None and binding[0] is self else None)
            self._records.append(record)
        ticket = _CaptureTicket(self, record)
        sdk_tickets = _sdk_capture_tickets.get()
        if sdk_tickets is not None:
            sdk_tickets.append(ticket)
        return ticket


def bind_capture_observer(callback, *, purpose):
    """Share only an opt-in observer collector with one scheduled callback.

    No logical call, SDK tickets, secret scope, credentials, policy, lease or
    other ContextVar is copied. Purpose is an observational label supplied by
    the scheduling source, not execution authority. Register before scheduling
    so a never-started or late callback cannot disappear from the snapshot.
    """
    from functools import wraps
    import re

    owner = _capture.get()
    if owner is None:
        return callback
    if not callable(callback) or type(purpose) is not str or not re.fullmatch(r"[a-z][a-z0-9_.:-]{0,63}", purpose):
        raise ValueError("capture handoff needs a callable and bounded purpose")
    registered_id = uuid.uuid4().hex
    with owner._lock:
        owner._handoffs[registered_id] = {"id": registered_id, "purpose": purpose,
                                         "state": "scheduled", "error_type": None}

    @wraps(callback)
    def observed(*args, **kwargs):
        active_id = registered_id
        with owner._lock:
            if owner._handoffs[registered_id]["state"] != "scheduled":
                # Observe unexpected re-use without suppressing the callback or
                # overwriting the original invocation's settlement evidence.
                owner._handoff_conflict = True
                active_id = uuid.uuid4().hex
                owner._handoffs[active_id] = {
                    "id": active_id, "purpose": purpose, "state": "scheduled",
                    "error_type": None, "reused_registration": registered_id}
            owner._handoffs[active_id]["state"] = "running"
        capture_token = _capture.set(owner)
        handoff_token = _capture_handoff.set((owner, active_id))
        try:
            result = callback(*args, **kwargs)
        except BaseException as error:
            with owner._lock:
                owner._handoffs[active_id].update(state="raised", error_type=type(error).__name__[:128])
            raise
        else:
            # Returning a coroutine/generator/opaque work handle is not an
            # admitted completion witness. Preserve its value and leave the
            # handoff explicitly unsettled; only synchronous scalar/None values
            # and HTTPX responses with separately observed body tickets have a
            # bounded settlement interpretation here.
            deferred = (result is not None
                        and type(result) not in {bool, int, float, str, bytes}
                        and not isinstance(result, httpx.Response))
            with owner._lock:
                owner._handoffs[active_id]["state"] = "returned_deferred" if deferred else "returned"
            return result
        finally:
            _capture_handoff.reset(handoff_token)
            _capture.reset(capture_token)

    return observed


def _note_sdk_model(ticket, value):
    candidates = (value, getattr(value, "message", None), getattr(value, "response", None))
    model = next((getattr(item, "model", None) for item in candidates
                  if type(getattr(item, "model", None)) is str), None)
    if not model or len(model.encode("utf-8")) > 4096:
        return
    with ticket.owner._lock:
        previous = ticket.record["provider_response_model"]
        if ticket.record.get("response_model_conflict"):
            return  # Already ambiguous: never choose a winner.
        if previous is not None and previous != model:
            ticket.record["response_model_conflict"] = True
            ticket.record["provider_response_model"] = None
        else:
            ticket.record["provider_response_model"] = model


class _ObservedSdkStream:
    """Metadata-only view of actual SDK events; event objects are unchanged."""

    def __init__(self, inner, ticket):
        self.inner, self.ticket = inner, ticket
        self._iterator = self._aiterator = None

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def __iter__(self):
        return self

    def __next__(self):
        if self._iterator is None:
            self._iterator = iter(self.inner)
        event = next(self._iterator)
        _note_sdk_model(self.ticket, event)
        return event

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._aiterator is None:
            self._aiterator = self.inner.__aiter__()
        event = await self._aiterator.__anext__()
        _note_sdk_model(self.ticket, event)
        return event

    def __enter__(self):
        self.inner.__enter__()
        return self

    def __exit__(self, *args):
        return self.inner.__exit__(*args)

    async def __aenter__(self):
        await self.inner.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self.inner.__aexit__(*args)


def _observe_sdk_response_model(response):
    tickets = _sdk_capture_tickets.get()
    if tickets:
        ticket = tickets[-1]  # Earlier SDK retry attempts have no returned object.
        _note_sdk_model(ticket, response)
        if isinstance(getattr(response, "response", None), httpx.Response):
            return _ObservedSdkStream(response, ticket)
    return response


class _CaptureTicket:
    def __init__(self, owner, record):
        self.owner, self.record = owner, record

    def settle(self, *, outcome, response=None, error=None):
        from copy import deepcopy

        with self.owner._lock:
            if self.record["outcome"] is not None:
                self.record["settlement_conflict"] = True
                return
            self.record["outcome"] = outcome
            # The physical owner supplies digest/status metadata only, never a
            # provider message or body. Do not broaden this to SDK responses.
            self.record["response"] = deepcopy(response) if isinstance(response, dict) else None
            self.record["error_type"] = type(error).__name__ if error is not None else None
            self.record["error_code"] = error.code if isinstance(error, EgressDispatchDenied) else None


@contextlib.contextmanager
def capture_physical_dispatch():
    """Observe buffered inference sends in this context; never authorize one.

    A prepared entry with no outcome is unsettled, not proof of transmission.
    Deferred response bodies keep their original capture owner after exit.
    """
    capture = PhysicalDispatchCapture()
    token = _capture.set(capture)
    try:
        yield capture
    finally:
        _capture.reset(token)


_sdk_active = contextvars.ContextVar("ares_inference_sdk_request", default=False)
_unsupported_bridge = contextvars.ContextVar(
    "ares_unqualified_thread_bridge", default=None
)


@contextlib.contextmanager
def unsupported_transport_scope(transport):
    """Observe an explicitly unsupported route; a lease can never admit it.

    Native enforce consumers must reject unsupported_transport before invoking
    opaque Rust or threaded adapters. Independently reject any supplied lease,
    including a lease supplied later at an orphanable worker's physical seam.
    """
    tickets = begin_llm_dispatch(
        {
            "schema": "ares.unsupported-send/v1",
            "transport": transport,
            "qualification_error": "UNSUPPORTED_TRANSPORT",
        },
        dispatch_phase="unsupported_transport",
    )
    if any(getattr(ticket, "transport_lease", None) is not None for ticket in tickets):
        settle_llm_dispatch(tickets, outcome="denied_by_peer")
        raise EgressDispatchDenied(
            "UNSUPPORTED_TRANSPORT", "route has no bounded cancellation owner"
        )
    token = _unsupported_bridge.set(transport)
    try:
        yield
    except BaseException as error:
        outcome, cause = _dispatch_error_settlement(error)
        settle_llm_dispatch(tickets, outcome=outcome, error=cause)
        raise
    else:
        settle_llm_dispatch(tickets, outcome="ok")
    finally:
        _unsupported_bridge.reset(token)


async def legacy_thread_bridge(callback, kwargs, *, transport):
    """Unbounded legacy observe/off only; no enforcing lease is admitted."""
    import asyncio

    with unsupported_transport_scope(transport):
        return await asyncio.to_thread(callback, **kwargs)


class _GovernanceEscape(BaseException):
    """Internal SDK retry fence; unwrapped by the instance request owner.

    OpenAI/Anthropic catch Exception around httpx and retry even typed denials.
    This signal crosses only that SDK stack, never a process/global patch.
    """

    def __init__(self, original):
        self.original = original


def _raise_governance(error) -> NoReturn:
    if _sdk_active.get():
        raise _GovernanceEscape(error) from error
    raise error


@contextlib.contextmanager
def inference_call_scope(agent, payload, *, purpose="conversation", route=None):
    """Bind one logical materialization across all physical SDK sends.

    The managed-call owner may return an explicit qualification failure. Observe
    consumers may record that failure; enforce consumers must reject it.
    """
    from ares_runtime.managed_calls import produce_dispatch_materialization

    basis = produce_dispatch_materialization(
        agent,
        payload,
        purpose=purpose,
        route=route,
        execution_id=getattr(agent, "_current_dispatch_attempt_id", None),
    )
    token = _call.set(basis)
    try:
        yield _call.get()
    finally:
        _call.reset(token)


@contextlib.contextmanager
def _restore_call(basis):
    token = _call.set(basis)
    try:
        yield
    finally:
        _call.reset(token)


class _CallBoundIterator:
    """Scope each iterator operation, never a suspended outward yield."""

    def __init__(self, iterator, basis):
        self._iterator, self._basis = iterator, basis

    def __iter__(self):
        return self

    def __next__(self):
        with _restore_call(self._basis):
            return next(self._iterator)

    def __getattr__(self, name):
        value = getattr(self._iterator, name)
        if name not in {"send", "throw", "close"} or not callable(value):
            return value

        def invoke(*args, **kwargs):
            with _restore_call(self._basis):
                return value(*args, **kwargs)

        return invoke


class _CallBoundAsyncIterator:
    def __init__(self, iterator, basis):
        self._iterator, self._basis = iterator, basis

    def __aiter__(self):
        return self

    async def __anext__(self):
        with _restore_call(self._basis):
            return await self._iterator.__anext__()

    def __getattr__(self, name):
        value = getattr(self._iterator, name)
        if name not in {"asend", "athrow", "aclose"} or not callable(value):
            return value

        async def invoke(*args, **kwargs):
            with _restore_call(self._basis):
                return await value(*args, **kwargs)

        return invoke


class _CallBoundStream:
    """Carry the owner projection into lazy SDK manager entry/iteration."""

    def __init__(self, stream, basis):
        self._stream, self._basis = stream, basis

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def __iter__(self):
        with _restore_call(self._basis):
            iterator = iter(self._stream)
        yield from _CallBoundIterator(iterator, self._basis)

    def __next__(self):
        with _restore_call(self._basis):
            return next(self._stream)

    def __aiter__(self):
        with _restore_call(self._basis):
            iterator = self._stream.__aiter__()
        return _CallBoundAsyncIterator(iterator, self._basis)

    async def __aenter__(self):
        with _restore_call(self._basis):
            return await self._stream.__aenter__()

    async def __aexit__(self, *args):
        with _restore_call(self._basis):
            return await self._stream.__aexit__(*args)

    async def aclose(self):
        with _restore_call(self._basis):
            return await self._stream.aclose()

    def __enter__(self):
        with _restore_call(self._basis):
            return self._stream.__enter__()

    def __exit__(self, *args):
        with _restore_call(self._basis):
            return self._stream.__exit__(*args)

    def close(self):
        with _restore_call(self._basis):
            close = getattr(self._stream, 'close', None)
            if callable(close):
                return close()
        return None


def bind_deferred_result(result, basis):
    # A stream manager can defer networking until __enter__. Do not wrap plain
    # response objects or alter off-mode response identities unnecessarily.
    if (
        hasattr(result, "__next__")
        or hasattr(result, "__anext__")
        or (
            (hasattr(result, "__enter__") or hasattr(result, "__aenter__"))
            and not isinstance(result, httpx.Response)
        )
    ):
        return _CallBoundStream(result, basis)
    return result


def _begin(request):
    try:
        return _begin_buffered(request)
    except (EgressDispatchDenied, EgressOutcomeUnrecorded) as error:
        _raise_governance(error)


def _begin_buffered(request):
    from blake3 import blake3
    from ares_runtime.dispatch_route import request_metadata
    from ares_runtime.transport_lease import admit_leases

    body_error = None
    try:
        body = request.content
    except httpx.RequestNotRead:
        # HTTPX rebuilds redirect requests from the same immutable ByteStream
        # without copying its cached content attribute. Admit only this exact
        # in-memory owner type; never drain an arbitrary streaming iterator.
        if type(getattr(request, "stream", None)) is httpx.ByteStream:
            body = request.read()
        else:
            body = None
            body_error = "UNSUPPORTED_TRANSPORT"
    if type(body) is not bytes or not body:
        body_error = "UNSUPPORTED_TRANSPORT"
    basis = _call.get()
    observation = {
        "schema": "ares.physical-send/v1",
        "dispatch_intent_ref": "ares-dispatch-intent:" + uuid.uuid4().hex,
        "execution_id": None if basis is None else basis.get("execution_id"),
        "materialization": None if basis is None else basis["materialization"],
        "qualification_error": body_error or (
            "MATERIALIZATION_UNAVAILABLE" if basis is None else basis["qualification_error"]
        ),
        "provider_request_digest": blake3(body).hexdigest() if type(body) is bytes else None,
        "provider_request_sha256": (
            "sha256:" + hashlib.sha256(body).hexdigest() if type(body) is bytes else None
        ),
        "provider_request_bytes": len(body) if type(body) is bytes else None,
        "request_body_schema": "ares.immutable-provider-body/v1+blake3",
        "started_monotonic": time.monotonic(),
    }
    route = str(getattr(request, "url", ""))
    if type(body) is bytes and body:
        observation.update(request_metadata(body, route=route))
    else:
        observation.update({"resolved_route": None, "route_qualification_error": "UNSUPPORTED_TRANSPORT",
                            "output_token_ceiling": None, "output_bound_error": "UNSUPPORTED_TRANSPORT"})
    # Observers see honest missing facts; enforce governors may refuse them.
    # Off/observe must not be blocked before the governor sees qualification.
    explicit = None if basis is None else basis.get("explicit_reasoning_request")
    capture = _capture.get()
    capture_ticket = capture.begin({**observation, "explicit_reasoning_request": explicit}) if capture is not None else None
    try:
        if _explicit_reasoning.get() is not None and basis is None:
            raise EgressDispatchDenied("EXPLICIT_REASONING_UNBOUND", "inference send lacks a logical request owner")
        if explicit is not None:
            from ares_runtime.dispatch_route import validate_explicit_reasoning

            validate_explicit_reasoning(
                explicit, observation.get("native_controls"),
                protocol=(observation.get("resolved_route") or {}).get("wire_protocol"))
        tickets = begin_llm_dispatch(observation, dispatch_phase="physical_send")
    except BaseException as error:
        if capture_ticket is not None:
            outcome, cause = _dispatch_error_settlement(error)
            if isinstance(error, EgressDispatchDenied):
                outcome = "denied_by_peer"
            capture_ticket.settle(outcome=outcome, error=cause)
        raise
    if capture_ticket is not None:
        tickets.append(capture_ticket)
    owned = _DispatchTickets(tickets)
    owned.started = time.monotonic()
    try:
        leased = any(getattr(t, "transport_lease", None) is not None for t in tickets)
        if leased and (body_error is not None or _unsupported_bridge.get() is not None):
            raise EgressDispatchDenied("UNSUPPORTED_TRANSPORT", "transport has no qualified buffered/bounded owner")
        owned.budget = admit_leases(tickets, body, route=route) if leased else None
    except EgressDispatchDenied:
        settle_llm_dispatch(tickets, outcome="denied_by_peer")
        raise
    return owned


def _lease_failure(error, budget):
    if budget is not None and isinstance(error, httpx.TransportError):
        try:
            budget.remaining()
        except EgressOutcomeUnrecorded as expired:
            return expired
        unknown = EgressOutcomeUnrecorded(
            "TRANSPORT_IO_UNCONFIRMED",
            "leased transport did not confirm a complete outcome",
        )
        unknown.__cause__ = error
        return unknown
    return error


class _Observation:
    def __init__(self, tickets, status):
        self.tickets = tickets
        self.budget = getattr(tickets, "budget", None)
        self.status = status
        self.digest = hashlib.sha256()
        self.length = 0
        self.started = getattr(tickets, "started", time.monotonic())
        self.finished = False

    def update(self, data):
        self.digest.update(data)
        self.length += len(data)
        if self.budget is not None:
            self.budget.remaining()

    def finish(self, outcome, error=None):
        if self.finished:
            return
        expired = None
        if outcome == "ok" and self.budget is not None:
            try:
                self.budget.remaining()
            except EgressOutcomeUnrecorded as failure:
                expired = error = failure
                outcome = "provider_error"
        self.finished = True
        tickets, self.tickets = self.tickets, []
        response = {
            "schema": "ares.transport-response-body/v1",
            "digest_algorithm": "sha256",
            "response_digest": "sha256:" + self.digest.hexdigest()
            if self.length
            else None,
            "response_bytes": self.length,
            "response_complete": outcome == "ok",
            "http_status": self.status,
            "duration_ms": (time.monotonic() - self.started) * 1000,
            "evidence": "executor_observed",
            "replay": "unavailable",
        }
        try:
            settle_llm_dispatch(
                tickets, outcome=outcome, response=response, error=error
            )
        except (EgressDispatchDenied, EgressOutcomeUnrecorded) as failure:
            _raise_governance(failure)
        if expired is not None:
            _raise_governance(expired)


class _SyncBody(httpx.SyncByteStream):
    def __init__(self, inner, observation, owner=None):
        self.inner, self.observation = inner, observation
        self.owner = owner
        self.closed = False

    def __iter__(self):
        try:
            for data in self.inner:
                self.observation.update(data)
                yield data
        except BaseException as error:
            error = _lease_failure(error, self.observation.budget)
            self.close_with(*_dispatch_error_settlement(error))
            if isinstance(error, (EgressDispatchDenied, EgressOutcomeUnrecorded)):
                _raise_governance(error)
            raise
        else:
            self.close_with("ok")

    def close_with(self, outcome, error=None):
        if self.closed:
            return
        self.closed = True
        try:
            self.inner.close()
        finally:
            try:
                if self.owner is not None:
                    self.owner[0].close()
                    self.owner[1].close()
            finally:
                self.observation.finish(outcome, error)

    def close(self):
        self.close_with("abandoned")

    def __del__(self):
        if not self.closed:
            self.close()


class _AsyncBody(httpx.AsyncByteStream):
    def __init__(self, inner, observation, owner=None):
        self.inner, self.observation = inner, observation
        self.owner = owner
        self.closed = False

    async def __aiter__(self):
        try:
            async for data in self.inner:
                self.observation.update(data)
                yield data
        except BaseException as error:
            error = _lease_failure(error, self.observation.budget)
            await self.close_with(*_dispatch_error_settlement(error))
            if isinstance(error, (EgressDispatchDenied, EgressOutcomeUnrecorded)):
                _raise_governance(error)
            raise
        else:
            await self.close_with("ok")

    async def close_with(self, outcome, error=None):
        if self.closed:
            return
        self.closed = True
        try:
            with __import__("anyio").CancelScope(shield=True):
                await self.inner.aclose()
                if self.owner is not None:
                    await self.owner[0].aclose()
                    self.owner[1].close()
        finally:
            self.observation.finish(outcome, error)

    async def aclose(self):
        await self.close_with("abandoned")

    def __del__(self):
        if not self.closed:
            self.closed = True
            try:
                if self.owner is not None:
                    self.owner[1].abort()
            finally:
                self.observation.finish("abandoned")


class _SyncTransport(httpx.BaseTransport):
    def __init__(self, inner):
        self.inner = inner

    def handle_request(self, request):
        from ares_runtime.transport_lease import bounded_transport
        from ares_runtime.dispatch_route import is_noncompletion_request

        if is_noncompletion_request(request):
            return self.inner.handle_request(request)
        tickets = _begin(request)
        owner = None
        transport = self.inner
        try:
            if tickets.budget is not None:
                transport, backend = bounded_transport(self.inner, tickets.budget)
                owner = (transport, backend)
        except EgressDispatchDenied as error:
            settle_llm_dispatch(tickets, outcome="denied_by_peer", error=error)
            _raise_governance(error)
        try:
            response = transport.handle_request(request)
        except BaseException as error:
            error = _lease_failure(error, tickets.budget)
            if owner is not None:
                owner[0].close()
                owner[1].close()
            _Observation(tickets, None).finish(*_dispatch_error_settlement(error))
            if isinstance(error, (EgressDispatchDenied, EgressOutcomeUnrecorded)):
                _raise_governance(error)
            raise
        response.stream = _SyncBody(
            response.stream, _Observation(tickets, response.status_code), owner
        )
        return response

    def close(self):
        self.inner.close()


class _AsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner):
        self.inner = inner

    async def handle_async_request(self, request):
        from ares_runtime.transport_lease import bounded_transport, _bounded
        from ares_runtime.dispatch_route import is_noncompletion_request

        if is_noncompletion_request(request):
            return await self.inner.handle_async_request(request)
        tickets = _begin(request)
        owner = None
        transport = self.inner
        try:
            if tickets.budget is not None:
                transport, backend = bounded_transport(
                    self.inner, tickets.budget, asynchronous=True
                )
                owner = (transport, backend)
        except EgressDispatchDenied as error:
            settle_llm_dispatch(tickets, outcome="denied_by_peer", error=error)
            _raise_governance(error)
        try:
            operation = lambda: transport.handle_async_request(request)
            response = (
                await operation()
                if owner is None
                else await _bounded(tickets.budget, operation)
            )
        except BaseException as error:
            error = _lease_failure(error, tickets.budget)
            if owner is not None:
                with __import__("anyio").CancelScope(shield=True):
                    await owner[0].aclose()
                    owner[1].close()
            _Observation(tickets, None).finish(*_dispatch_error_settlement(error))
            if isinstance(error, (EgressDispatchDenied, EgressOutcomeUnrecorded)):
                _raise_governance(error)
            raise
        response.stream = _AsyncBody(
            response.stream, _Observation(tickets, response.status_code), owner
        )
        return response

    async def aclose(self):
        await self.inner.aclose()


class _BotocoreBody:
    """Observe urllib3 raw streaming chunks without decoding or retaining them."""

    def __init__(self, inner, observation, owner=None):
        self.inner, self.observation = inner, observation
        self.owner = owner
        self.closed = False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def stream(self, *args, **kwargs):
        try:
            for chunk in self.inner.stream(*args, **kwargs):
                self.observation.update(chunk)
                yield chunk
        except BaseException as error:
            error = _lease_failure(error, self.observation.budget)
            self.close_with(*_dispatch_error_settlement(error))
            if isinstance(error, (EgressDispatchDenied, EgressOutcomeUnrecorded)):
                _raise_governance(error)
            raise
        else:
            self.close_with("ok")

    def read(self, *args, **kwargs):
        try:
            chunk = self.inner.read(*args, **kwargs)
        except BaseException as error:
            error = _lease_failure(error, self.observation.budget)
            self.close_with(*_dispatch_error_settlement(error))
            if isinstance(error, (EgressDispatchDenied, EgressOutcomeUnrecorded)):
                _raise_governance(error)
            raise
        self.observation.update(chunk)
        if not chunk:
            self.close_with("ok")
        return chunk

    def close_with(self, outcome, error=None):
        if self.closed:
            return
        self.closed = True
        try:
            self.inner.close()
        finally:
            try:
                if self.owner is not None:
                    self.owner[0].close()
                    self.owner[1].close()
            finally:
                self.observation.finish(outcome, error)

    def close(self):
        self.close_with("abandoned")

    def __del__(self):
        if not self.closed:
            self.close()


def bind_bedrock_client(client):
    """Bind this botocore inference client's actual prepared-body send owner.

    URLLib3Session disables urllib3 retries; botocore retries re-enter this
    instance's send and require a fresh intent. No control-plane client binding.
    """
    if client in _bound_sdk:
        return client
    session = client._endpoint.http_session
    send = session.send

    def send_once(request):
        # AWSPreparedRequest.body is the same immutable bytes passed to urlopen.
        tickets = _begin(types.SimpleNamespace(content=request.body, url=request.url))
        try:
            if tickets.budget is None:
                response = send(request)
            else:
                from ares_runtime.bedrock_lease import send_bounded

                response = send_bounded(session, request, tickets.budget)
        except BaseException as error:
            error = _lease_failure(error, tickets.budget)
            try:
                settle_llm_dispatch(
                    tickets,
                    outcome="denied_by_peer"
                    if isinstance(error, EgressDispatchDenied)
                    else _dispatch_error_outcome(error),
                    error=error,
                )
            except (EgressDispatchDenied, EgressOutcomeUnrecorded) as failure:
                _raise_governance(failure)
            if isinstance(error, (EgressDispatchDenied, EgressOutcomeUnrecorded)):
                _raise_governance(error)
            raise
        observation = _Observation(tickets, response.status_code)
        if request.stream_output or tickets.budget is not None:
            response.raw = _BotocoreBody(response.raw, observation)
            if not request.stream_output:
                response.content
        else:
            observation.update(response.content)
            observation.finish("ok")
        return response

    session.send = send_once
    original = client._make_api_call

    def invoke(self, *args, **kwargs):
        token = _sdk_active.set(True)
        try:
            return original(*args, **kwargs)
        except _GovernanceEscape as escape:
            raise escape.original from None
        finally:
            _sdk_active.reset(token)

    client._make_api_call = types.MethodType(invoke, client)
    _bound_sdk.add(client)
    return client


def bind_http_client(client):
    """Wrap this inference client's existing transports, preserving their config.

    httpx 0.28.1 transport/mount attributes are a pinned integration boundary.
    Unrelated clients are untouched. Authentication and request hooks still run
    normally. The same Request and immutable content reach the original pool.
    """
    if client in _bound_http:
        return client
    if not isinstance(client, (httpx.Client, httpx.AsyncClient)):
        raise TypeError("physical inference boundary requires an httpx client")
    wrapper = (
        _AsyncTransport if isinstance(client, httpx.AsyncClient) else _SyncTransport
    )
    wrapped = {}

    def wrap(transport):
        if transport is None:
            return None
        if id(transport) not in wrapped:
            wrapped[id(transport)] = wrapper(transport)
        return wrapped[id(transport)]

    client._transport = wrap(client._transport)
    client._mounts = {key: wrap(value) for key, value in client._mounts.items()}
    _bound_http.add(client)
    return client


def bind_inference_client(client):
    """Bind an actual installed OpenAI/Anthropic instance, not SDK globals.

    Request copies share the bound http client; each SDK request invocation owns
    its retry fence. A typed original is restored before Ares fallback logic.
    """
    if client in _bound_sdk:
        return client
    bind_http_client(client._client)
    original = client.request
    if inspect.iscoroutinefunction(original):

        async def request(self, *args, **kwargs):
            token = _sdk_active.set(True)
            capture_token = _sdk_capture_tickets.set([] if _capture.get() is not None else None)
            try:
                return _observe_sdk_response_model(await original(*args, **kwargs))
            except _GovernanceEscape as escape:
                raise escape.original from None
            finally:
                _sdk_capture_tickets.reset(capture_token)
                _sdk_active.reset(token)

    else:

        def request(self, *args, **kwargs):
            token = _sdk_active.set(True)
            capture_token = _sdk_capture_tickets.set([] if _capture.get() is not None else None)
            try:
                return _observe_sdk_response_model(original(*args, **kwargs))
            except _GovernanceEscape as escape:
                raise escape.original from None
            finally:
                _sdk_capture_tickets.reset(capture_token)
                _sdk_active.reset(token)

    client.request = types.MethodType(request, client)
    original_copy = client.copy

    def copy_client(self, *args, **kwargs):
        return bind_inference_client(original_copy(*args, **kwargs))

    client.copy = types.MethodType(copy_client, client)
    client.with_options = client.copy
    _bound_sdk.add(client)
    return client
