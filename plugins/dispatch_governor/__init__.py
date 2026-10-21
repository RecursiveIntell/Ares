"""Durable dispatch governor for the managed model-call execution record.

The governor is a plugin, not a core special case.  It receives the
``physical_send`` observation from ``physical_dispatch`` and commits the
``send_started`` transition *before* the transport sends bytes.  In enforce
mode a failed commit raises ``EgressDispatchDenied`` so the send never starts.
Observe mode records what it can and never denies.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

from hermes_cli.middleware import EgressDispatchDenied, EgressOutcomeUnrecorded
from hermes_state_execution import ExecutionRecordStoreError

logger = logging.getLogger(__name__)

DEFAULT_MODE = "observe"
_VALID_MODES = frozenset({"observe", "enforce"})
_STORE: Any = None


def _default_store():
    """Resolve the canonical SessionDB lazily; never at plugin import time."""
    global _STORE
    if _STORE is None:
        from hermes_state import SessionDB

        _STORE = SessionDB()
    return _STORE


def _configured_mode() -> str:
    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
        section = config.get("dispatch_governor") or {}
        mode = (
            section.get("mode", DEFAULT_MODE)
            if isinstance(section, Mapping)
            else DEFAULT_MODE
        )
    except Exception:
        mode = DEFAULT_MODE
    return mode if mode in _VALID_MODES else DEFAULT_MODE


def _wire_digest(observation: Mapping[str, Any]) -> str | None:
    sha256 = observation.get("provider_request_sha256")
    if isinstance(sha256, str) and sha256.startswith("sha256:"):
        return sha256
    digest = observation.get("provider_request_digest")
    if isinstance(digest, str) and digest:
        return "blake3:" + digest
    return None


def _route_reference(route: Any) -> str | None:
    if isinstance(route, Mapping):
        wire = route.get("wire_protocol")
        model = route.get("model_ref")
        if isinstance(wire, str) and wire and isinstance(model, str) and model:
            return f"{wire}:{model}"
    if isinstance(route, str) and route:
        return route
    return None


class _GovernorTicket:
    def __init__(self, store, execution_id: str, mode: str) -> None:
        self.store = store
        self.execution_id = execution_id
        self.mode = mode

    def settle(
        self, *, outcome: str, response: Any = None, error: BaseException | None = None
    ) -> None:
        if self.store is None:
            return
        try:
            current = self.store.read_execution_record(self.execution_id)
        except ExecutionRecordStoreError as exc:
            if exc.code == "EXECUTION_NOT_FOUND":
                if self.mode == "enforce":
                    raise EgressOutcomeUnrecorded(
                        "DISPATCH_RECORD_UNAVAILABLE",
                        "execution record disappeared before settlement",
                    ) from exc
                return
            raise
        if current.is_terminal:
            return

        response_digest = None
        http_status = None
        if isinstance(response, Mapping):
            value = response.get("response_digest")
            response_digest = (
                value
                if isinstance(value, str) and value.startswith(("sha256:", "blake3:"))
                else None
            )
            status = response.get("http_status")
            http_status = (
                status if type(status) is int and 100 <= status <= 599 else None
            )
        provider_error_code = None
        if error is not None:
            provider_error_code = str(getattr(error, "code", type(error).__name__))[
                :128
            ]
            status = getattr(error, "status_code", None)
            if http_status is None and type(status) is int and 100 <= status <= 599:
                http_status = status

        try:
            if outcome == "denied_by_peer":
                target = (
                    "denied"
                    if current.state in {"admitted", "materialized"}
                    else "indeterminate"
                )
                self.store.transition_execution_record(
                    self.execution_id,
                    target,
                    terminal_outcome="denied" if target == "denied" else "unknown",
                )
            elif outcome in {"ok", "provider_error", "interrupted", "discarded"}:
                self.store.transition_execution_record(
                    self.execution_id,
                    "settled",
                    terminal_outcome=outcome,
                    response_digest=response_digest,
                    http_status=http_status,
                    provider_error_code=provider_error_code,
                )
            else:
                self.store.transition_execution_record(
                    self.execution_id,
                    "indeterminate",
                    terminal_outcome="unknown",
                    provider_error_code=provider_error_code,
                )
        except ExecutionRecordStoreError as exc:
            if exc.code in {"ALREADY_TERMINAL", "ALREADY_TRANSITIONED"}:
                return
            if self.mode == "enforce":
                raise EgressOutcomeUnrecorded(
                    "DISPATCH_SETTLEMENT_UNAVAILABLE",
                    "execution record could not be settled",
                ) from exc
            logger.warning("dispatch governor settlement unavailable: %s", exc)


class DispatchGovernor:
    """One implementation used by unit tests and the production plugin."""

    def __init__(
        self,
        *,
        store_factory: Callable[[], Any] | None = None,
        mode: str = DEFAULT_MODE,
    ):
        if mode not in _VALID_MODES:
            raise ValueError("invalid dispatch governor mode")
        self._store_factory = store_factory or _default_store
        self.mode = mode
        self._store = None

    def _get_store(self):
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    def __call__(
        self, *, payload: Any, dispatch_phase: str = "sdk_invocation", **context: Any
    ):
        if dispatch_phase != "physical_send":
            return None
        if not isinstance(payload, Mapping):
            return None
        execution_id = payload.get("execution_id")
        if not execution_id:
            materialization = payload.get("materialization")
            if isinstance(materialization, Mapping):
                execution_id = materialization.get("execution_id")
        if not execution_id:
            execution_id = context.get("attempt_id")
        if not execution_id:
            return None

        store = self._get_store()
        intent = payload.get("dispatch_intent_ref")
        if not isinstance(intent, str) or not intent:
            intent = "ares-dispatch-intent:unknown"
        wire = _wire_digest(payload)
        wire_bytes = payload.get("provider_request_bytes")
        route = _route_reference(payload.get("resolved_route"))
        transport = payload.get("request_body_schema")

        try:
            current = store.read_execution_record(execution_id)
        except ExecutionRecordStoreError as exc:
            if exc.code == "EXECUTION_NOT_FOUND":
                if self.mode == "enforce":
                    raise EgressDispatchDenied(
                        "DISPATCH_RECORD_UNAVAILABLE",
                        "no execution record exists for the physical send",
                    ) from exc
                return None
            raise

        if current.is_terminal:
            return None
        try:
            if current.state in {"admitted", "materialized"}:
                if wire is None or type(wire_bytes) is not int:
                    if self.mode == "enforce":
                        raise EgressDispatchDenied(
                            "DISPATCH_WIRE_UNQUALIFIED",
                            "physical send has no qualified wire digest",
                        )
                    return _GovernorTicket(store, execution_id, self.mode)
                store.transition_execution_record(
                    execution_id,
                    "send_started",
                    wire_request_digest=wire,
                    wire_bytes=wire_bytes,
                    transport_version=str(transport) if transport else None,
                    route_ref=str(route) if route else None,
                    dispatch_intent_refs=[intent],
                    governor_qualified=True,
                    governor_mode=self.mode,
                )
            elif current.state == "send_started":
                store.append_dispatch_intent(execution_id, intent)
        except ExecutionRecordStoreError as exc:
            if exc.code == "ALREADY_TRANSITIONED":
                current = store.read_execution_record(execution_id)
                if current.state == "send_started":
                    store.append_dispatch_intent(execution_id, intent)
                    return _GovernorTicket(store, execution_id, self.mode)
                if current.is_terminal:
                    return None
            if exc.code == "ALREADY_TERMINAL":
                return None
            if self.mode == "enforce":
                raise EgressDispatchDenied(
                    "DISPATCH_RECORD_UNAVAILABLE",
                    f"pre-send marker could not be committed: {exc.code}",
                ) from exc
            logger.warning("dispatch governor observe failure: %s", exc)
        return _GovernorTicket(store, execution_id, self.mode)


def register(ctx) -> None:
    mode = _configured_mode()
    ctx.register_middleware("llm_dispatch", DispatchGovernor(mode=mode))


__all__ = ["DispatchGovernor", "register"]
