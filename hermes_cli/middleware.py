"""Hermes middleware contract helpers.

Observer hooks report what happened. Middleware can change what happens by
rewriting a request or wrapping the actual execution callback. Keep the small
contract helpers here so agent-loop call sites and plugins share one vocabulary.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)

OBSERVER_SCHEMA_VERSION = "hermes.observer.v1"
MIDDLEWARE_SCHEMA_VERSION = "hermes.middleware.v1"

TOOL_REQUEST_MIDDLEWARE = "tool_request"
TOOL_EXECUTION_MIDDLEWARE = "tool_execution"
LLM_REQUEST_MIDDLEWARE = "llm_request"
LLM_EXECUTION_MIDDLEWARE = "llm_execution"
LLM_DISPATCH_MIDDLEWARE = "llm_dispatch"

# Back-compat aliases for older PoC branches that used API terminology.
API_REQUEST_MIDDLEWARE = LLM_REQUEST_MIDDLEWARE
API_EXECUTION_MIDDLEWARE = LLM_EXECUTION_MIDDLEWARE

VALID_MIDDLEWARE: set[str] = {
    TOOL_REQUEST_MIDDLEWARE,
    TOOL_EXECUTION_MIDDLEWARE,
    LLM_REQUEST_MIDDLEWARE,
    LLM_EXECUTION_MIDDLEWARE,
    LLM_DISPATCH_MIDDLEWARE,
}


class EgressDispatchDenied(RuntimeError):
    """No provider callback began; never classify as a retryable API error."""

    retryable = False
    provider_attempted = False

    def __init__(self, code: str, message: str = "model dispatch denied") -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class EgressOutcomeUnrecorded(RuntimeError):
    """A provider callback began but settlement is unproven; never resend."""

    retryable = False
    provider_attempted = True

    def __init__(self, code: str = "DISPATCH_OUTCOME_UNRECORDED",
                 message: str = "model dispatch outcome could not be recorded") -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


_LLM_DISPATCH_OUTCOMES = frozenset({
    "ok", "provider_error", "interrupted", "discarded", "abandoned", "denied_by_peer",
})


def begin_llm_dispatch(payload: Any, **context: Any) -> list[Any]:
    """Strict governor preflight, independent of generic fail-open middleware.

    This callback-level seam is not proof of a network-attempt boundary. A
    transport must independently establish that boundary before enforce use.
    No callback can rewrite the live request through its inspection copy.
    """
    context.setdefault("dispatch_phase", "sdk_invocation")
    tickets: list[Any] = []
    try:
        for callback in _get_middleware_callbacks(LLM_DISPATCH_MIDDLEWARE):
            # The old _safe_copy intentionally shares nested members on failure.
            # Governance cannot use that permissive request-rewrite fallback.
            inspection = deepcopy(payload)
            ticket = callback(payload=inspection, **middleware_payload(**context))
            if ticket is not None:
                if not callable(getattr(ticket, "settle", None)):
                    raise TypeError("dispatch governor returned a malformed ticket")
                tickets.append(ticket)
    except BaseException as error:
        cleanup_errors = []
        for ticket in tickets:
            try:
                ticket.settle(outcome="denied_by_peer", response=None, error=None)
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if not isinstance(error, Exception):
            raise
        denied = error if isinstance(error, EgressDispatchDenied) else EgressDispatchDenied(
            "DISPATCH_GOVERNOR_ERROR", "dispatch governor preflight failed"
        )
        if cleanup_errors:
            denied.add_note(f"{len(cleanup_errors)} prior governor settlement(s) unconfirmed; provider not attempted")
        if denied is error:
            raise
        raise denied from error
    return tickets


def settle_llm_dispatch(tickets: list[Any], *, outcome: str,
                        response: Any = None, error: BaseException | None = None) -> None:
    """Attempt every ticket's settlement; any failure withholds completion.

    Observe-mode tickets own their explicitly nonblocking degradation behavior.
    The core never silently turns an enforce settlement error into a response.
    """
    if outcome not in _LLM_DISPATCH_OUTCOMES:
        raise ValueError(f"invalid llm dispatch outcome: {outcome!r}")
    failures = []
    for ticket in tickets:
        try:
            ticket.settle(outcome=outcome, response=response, error=error)
        except BaseException as failure:
            failures.append(failure)
    if failures:
        unknown = failures[0] if isinstance(failures[0], EgressOutcomeUnrecorded) else EgressOutcomeUnrecorded()
        unknown.add_note(f"{len(failures)} governor settlement(s) unconfirmed")
        if unknown is failures[0]:
            raise unknown
        raise unknown from failures[0]


def _dispatch_error_outcome(error: BaseException) -> str:
    """Classify why a governed call or stream ended before completing.

    GeneratorExit means the consumer stopped reading: it closed, or dropped the
    last reference to, a generator chain suspended inside a body wrapper. The
    provider did not fail, so this is the same terminal as an explicit close().
    """
    import asyncio
    if isinstance(error, GeneratorExit):
        return "abandoned"
    if isinstance(error, (InterruptedError, KeyboardInterrupt, asyncio.CancelledError)):
        return "interrupted"
    return "provider_error"


def _dispatch_error_settlement(error: BaseException) -> tuple[str, BaseException | None]:
    """Outcome plus the error to record; abandonment records no error.

    An explicit close() settles ``abandoned`` with ``error=None``. A consumer
    stop observed as GeneratorExit must settle identically, so governors never
    see a control-flow signal presented as a failure cause.
    """
    outcome = _dispatch_error_outcome(error)
    return outcome, (None if outcome == "abandoned" else error)


def run_llm_dispatch(payload: Any, callback: Callable, *, stream: bool = False, _dispatch_agent=None, **context: Any) -> Any:
    """Govern one callback without allowing a governor to rewrite its bytes."""
    tickets = begin_llm_dispatch(payload, **context)
    from ares_runtime.physical_dispatch import inference_call_scope, bind_deferred_result
    try:
        with inference_call_scope(_dispatch_agent, payload,
                                  purpose=context.get("purpose", "conversation"),
                                  route={key: context[key] for key in ("provider", "api_mode") if context.get(key)}) as basis:
            response = callback(payload)
            if stream and _get_middleware_callbacks(LLM_DISPATCH_MIDDLEWARE):
                response = bind_deferred_result(response, basis)
    except BaseException as error:
        outcome, cause = _dispatch_error_settlement(error)
        settle_llm_dispatch(tickets, outcome=outcome, error=cause)
        raise
    if tickets and stream:
        return GovernedStreamProxy(response, tickets)
    settle_llm_dispatch(tickets, outcome="ok", response=response)
    return response


async def async_run_llm_dispatch(payload: Any, callback: Callable, **context: Any) -> Any:
    """Keep blocking governance IPC off the event loop; never send on cancel.

    Cancellation during preflight may leave a charged, pending native attempt
    when its IPC worker returns late. It does not resume the provider callback;
    native expiry/reconciliation owns that missing outcome, never a fake refund.
    """
    import asyncio
    tickets = await asyncio.to_thread(begin_llm_dispatch, payload, **context)
    from ares_runtime.physical_dispatch import inference_call_scope, bind_deferred_result
    try:
        with inference_call_scope(None, payload, purpose=context.get("purpose", "conversation"),
                                  route={key: context[key] for key in ("provider", "api_mode") if context.get(key)}) as basis:
            response = await callback(payload)
            if _get_middleware_callbacks(LLM_DISPATCH_MIDDLEWARE):
                response = bind_deferred_result(response, basis)
    except BaseException as error:
        outcome, cause = _dispatch_error_settlement(error)
        await asyncio.to_thread(settle_llm_dispatch, tickets, outcome=outcome, error=cause)
        raise
    await asyncio.to_thread(settle_llm_dispatch, tickets, outcome="ok", response=response)
    return response


class GovernedStreamProxy:
    """Keep one ticket batch pending until explicit stream exhaustion or close.

    Response-body evidence is still owned by the transport, not this wrapper.
    The provider object itself is never re-requested by this proxy.
    """

    def __init__(self, stream: Any, tickets: list[Any]) -> None:
        from threading import RLock
        self._stream = stream
        self._manager = stream
        self._iterator = None
        self._tickets = tickets
        self._lock = RLock()
        self._closed = False
        self._exit_called = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    def _settle(self, outcome: str, error: BaseException | None = None) -> None:
        with self._lock:
            tickets, self._tickets = self._tickets, None
        if tickets is not None:
            settle_llm_dispatch(tickets, outcome=outcome, response=self._stream, error=error)

    def __iter__(self):
        return self

    def __next__(self):
        with self._lock:
            if self._closed or self._tickets is None:
                raise StopIteration
        try:
            if self._iterator is None:
                self._iterator = iter(self._stream)
            value = next(self._iterator)
        except StopIteration:
            self._settle("ok")
            raise
        except BaseException as error:
            self._settle(*_dispatch_error_settlement(error))
            raise
        return value

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            close = getattr(self._stream, "close", None)
            if close is not None:
                close()
        except BaseException as error:
            self._settle("provider_error", error)
            raise
        else:
            self._settle("abandoned")

    def __del__(self):
        if not self._closed:
            self.close()

    def __enter__(self):
        try:
            enter = getattr(self._manager, "__enter__", None)
            if enter is not None:
                self._stream = enter()
        except BaseException as error:
            self._settle("provider_error", error)
            raise
        return self

    def __exit__(self, exc_type, error, traceback):
        with self._lock:
            if self._exit_called:
                return False
            self._exit_called = True
            self._closed = True
        try:
            exit_stream = getattr(self._manager, "__exit__", None)
            if exit_stream is not None:
                exit_stream(exc_type, error, traceback)
        except BaseException as close_error:
            self._settle("provider_error", close_error)
            raise
        finally:
            # A relaying generator closed mid-stream exits here with GeneratorExit.
            self._settle(*(("abandoned", None) if error is None else _dispatch_error_settlement(error)))
        # Never swallow a provider or governance exception.
        return False


@dataclass
class RequestMiddlewareResult:
    """Result of applying request middleware to a mutable payload."""

    payload: Any
    original_payload: Any
    changed: bool = False
    trace: List[Dict[str, Any]] = field(default_factory=list)


def observer_payload(**kwargs: Any) -> Dict[str, Any]:
    kwargs.setdefault("telemetry_schema_version", OBSERVER_SCHEMA_VERSION)
    return kwargs


def middleware_payload(**kwargs: Any) -> Dict[str, Any]:
    kwargs.setdefault("telemetry_schema_version", OBSERVER_SCHEMA_VERSION)
    kwargs.setdefault("middleware_schema_version", MIDDLEWARE_SCHEMA_VERSION)
    return kwargs


def _safe_copy(payload: Any) -> Any:
    """Deep-copy a request payload, tolerating non-deepcopyable members.

    Request payloads are normally plain JSON-shaped dicts, but an LLM request
    can occasionally carry non-deepcopyable objects (clients, callbacks, file
    handles). A hard ``deepcopy`` failure there would otherwise abort the whole
    request-middleware pass. Fall back to a shallow ``dict`` copy so middleware
    still runs and the original nested objects are shared by reference rather
    than corrupting the live payload.
    """
    try:
        return deepcopy(payload)
    except Exception as exc:  # pragma: no cover - exercised via fallback test
        logger.debug("deepcopy failed for request payload (%s); using shallow copy", exc)
        if isinstance(payload, dict):
            return dict(payload)
        return payload


def apply_llm_request_middleware(
    request: Dict[str, Any],
    **context: Any,
) -> RequestMiddlewareResult:
    """Apply registered LLM request middleware.

    Middleware may return ``{"request": {...}}`` to replace the effective
    provider kwargs before Hermes sends them.
    """
    if not _has_middleware(LLM_REQUEST_MIDDLEWARE):
        return RequestMiddlewareResult(
            payload=request,
            original_payload=request,
            changed=False,
            trace=[],
        )

    original_request = _safe_copy(request)
    current_request = _safe_copy(original_request)
    trace: List[Dict[str, Any]] = []

    for result in _invoke_middleware(
        LLM_REQUEST_MIDDLEWARE,
        request=current_request,
        original_request=original_request,
        **context,
    ):
        if not isinstance(result, dict):
            continue
        next_request = result.get("request")
        if not isinstance(next_request, dict):
            continue
        current_request = _safe_copy(next_request)
        trace.append(_trace_entry(result))

    return RequestMiddlewareResult(
        payload=current_request,
        original_payload=original_request,
        changed=bool(trace),
        trace=trace,
    )


def apply_tool_request_middleware(
    tool_name: str,
    args: Dict[str, Any],
    **context: Any,
) -> RequestMiddlewareResult:
    """Apply registered tool request middleware.

    Middleware may return ``{"args": {...}}`` to replace the effective tool
    arguments before hooks, guardrails, approvals, and execution see them.
    """
    original_args = _safe_copy(args)
    current_args = _safe_copy(original_args)
    trace: List[Dict[str, Any]] = []

    session_id = str(context.get("session_id") or "")
    skip_relay = bool(context.pop("skip_relay", False))
    if session_id and not skip_relay:
        from agent import relay_runtime

        relay_args = relay_runtime.apply_tool_request_intercepts(
            session_id=session_id,
            tool_name=tool_name,
            args=current_args,
        )
        if relay_args != current_args:
            current_args = _safe_copy(relay_args)
            trace.append({"source": "nemo_relay"})

    if not _has_middleware(TOOL_REQUEST_MIDDLEWARE):
        return RequestMiddlewareResult(
            payload=args if not trace else current_args,
            original_payload=args,
            changed=bool(trace),
            trace=trace,
        )

    for result in _invoke_middleware(
        TOOL_REQUEST_MIDDLEWARE,
        tool_name=tool_name,
        args=current_args,
        original_args=original_args,
        **context,
    ):
        if not isinstance(result, dict):
            continue
        next_args = result.get("args")
        if not isinstance(next_args, dict):
            continue
        current_args = _safe_copy(next_args)
        trace.append(_trace_entry(result))

    return RequestMiddlewareResult(
        payload=current_args,
        original_payload=original_args,
        changed=bool(trace),
        trace=trace,
    )


def apply_api_request_middleware(
    request: Dict[str, Any],
    **context: Any,
) -> RequestMiddlewareResult:
    """Compatibility wrapper for older ``api_request`` naming."""
    return apply_llm_request_middleware(request, **context)


def run_llm_execution_middleware(
    request: Dict[str, Any],
    next_call: Callable[[Dict[str, Any]], Any],
    **context: Any,
) -> Any:
    """Run provider execution through registered LLM execution middleware."""
    callbacks = _get_middleware_callbacks(LLM_EXECUTION_MIDDLEWARE)
    if not callbacks:
        return next_call(request)
    return _run_execution_chain(
        LLM_EXECUTION_MIDDLEWARE,
        callbacks,
        next_call,
        request=request,
        original_request=context.pop("original_request", request),
        **context,
    )


def run_tool_execution_middleware(
    tool_name: str,
    args: Dict[str, Any],
    next_call: Callable[[Dict[str, Any]], Any],
    **context: Any,
) -> Any:
    """Run tool execution through registered tool execution middleware."""
    callbacks = _get_middleware_callbacks(TOOL_EXECUTION_MIDDLEWARE)
    if not callbacks:
        return next_call(args)
    return _run_execution_chain(
        TOOL_EXECUTION_MIDDLEWARE,
        callbacks,
        next_call,
        tool_name=tool_name,
        args=args,
        original_args=context.pop("original_args", args),
        **context,
    )


def run_api_execution_middleware(
    request: Dict[str, Any],
    next_call: Callable[[Dict[str, Any]], Any],
    **context: Any,
) -> Any:
    """Compatibility wrapper for older ``api_execution`` naming."""
    return run_llm_execution_middleware(request, next_call, **context)


def _invoke_middleware(kind: str, **kwargs: Any) -> List[Any]:
    from hermes_cli.plugins import invoke_middleware

    return invoke_middleware(kind, **middleware_payload(**kwargs))


def _has_middleware(kind: str) -> bool:
    from hermes_cli.plugins import has_middleware

    return has_middleware(kind)


def _get_middleware_callbacks(kind: str) -> List[Callable]:
    from hermes_cli.plugins import get_plugin_manager

    return list(get_plugin_manager()._middleware.get(kind, []))


def _run_execution_chain(
    kind: str,
    callbacks: List[Callable],
    terminal_call: Callable[[Any], Any],
    **kwargs: Any,
) -> Any:
    payload_key = "request" if "request" in kwargs else "args"

    class _DownstreamExecutionError(Exception):
        def __init__(self, original: BaseException) -> None:
            super().__init__(str(original))
            self.original = original

    def call_at(index: int, payload: Any) -> Any:
        if index >= len(callbacks):
            return terminal_call(payload)

        callback = callbacks[index]
        next_called = False
        next_succeeded = False
        next_result: Any = None

        def next_call(next_payload: Any = None) -> Any:
            nonlocal next_called, next_succeeded, next_result
            # ``next_call`` is single-use per middleware frame. Calling it more
            # than once would re-run the downstream provider/tool, so a second
            # invocation is a contract violation rather than a retry. Surface it
            # instead of silently executing the terminal call twice.
            if next_called:
                raise RuntimeError(
                    f"Middleware '{kind}' callback "
                    f"{getattr(callback, '__name__', repr(callback))} called "
                    "next_call() more than once; downstream execution is single-use"
                )
            next_called = True
            try:
                next_result = call_at(index + 1, payload if next_payload is None else next_payload)
                next_succeeded = True
                return next_result
            except Exception as exc:
                raise _DownstreamExecutionError(exc) from exc

        call_kwargs = middleware_payload(**kwargs)
        call_kwargs[payload_key] = payload
        call_kwargs["next_call"] = next_call
        try:
            return callback(**call_kwargs)
        except _DownstreamExecutionError as exc:
            raise exc.original
        except Exception as exc:
            logger.warning(
                "Middleware '%s' callback %s raised: %s",
                kind,
                getattr(callback, "__name__", repr(callback)),
                exc,
            )
            if next_succeeded:
                return next_result
            if next_called:
                raise
            return call_at(index + 1, payload)

    return call_at(0, kwargs[payload_key])


def _trace_entry(result: Dict[str, Any]) -> Dict[str, Any]:
    entry: Dict[str, Any] = {}
    for key in ("source", "reason", "name"):
        value = result.get(key)
        if isinstance(value, str) and value:
            entry[key] = value
    if not entry:
        entry["source"] = "plugin"
    return entry
