"""Closed, nonauthorizing bounds supplied by dispatch ticket owners.

Native expiry stays absolute. The local monotonic deadline is anchored once at
admission and never renewed by a network operation, chunk, retry or redirect.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
import sys
import time
from types import MappingProxyType
from typing import Any, Mapping

import anyio
import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend
from hermes_cli.middleware import EgressDispatchDenied, EgressOutcomeUnrecorded


@dataclass(frozen=True, slots=True)
class TransportLease:
    schema_version: str
    expires_at_unix_ms: int
    send_timeout_ms: int
    request_bytes_limit: int
    output_tokens_limit: int

    @classmethod
    def parse(cls, value):
        if isinstance(value, cls):
            value = {name: getattr(value, name) for name in cls.__dataclass_fields__}
        fields = set(cls.__dataclass_fields__)
        if type(value) is not dict or set(value) != fields:
            raise EgressDispatchDenied(
                "INVALID_TRANSPORT_LEASE", "closed transport lease fields required"
            )
        if value["schema_version"] != "ares.transport-lease/v1":
            raise EgressDispatchDenied(
                "INVALID_TRANSPORT_LEASE", "unknown lease schema"
            )
        if any(
            type(value[k]) is not int or not 0 < value[k] <= 2**63 - 1
            for k in fields - {"schema_version"}
        ):
            raise EgressDispatchDenied(
                "INVALID_TRANSPORT_LEASE",
                "bounds must be positive signed-64-bit integers",
            )
        return cls(**value)

    def to_dict(self):
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class SendBudget:
    leases: tuple[TransportLease, ...]
    deadline_monotonic: float

    def remaining(self):
        # A forward wall-clock adjustment may shorten, but cannot extend, a send.
        wall = min((v.expires_at_unix_ms / 1000 - time.time()) for v in self.leases)
        remaining = min(self.deadline_monotonic - time.monotonic(), wall)
        if remaining <= 0:
            raise EgressOutcomeUnrecorded(
                "TRANSPORT_DEADLINE_EXCEEDED", "total local send deadline elapsed"
            )
        return remaining


def admit_leases(tickets, body: bytes, *, route: str) -> SendBudget | None:
    leases = tuple(
        TransportLease.parse(v)
        for t in tickets
        if (v := getattr(t, "transport_lease", None)) is not None
    )
    if not leases:
        return None
    wall, mono = time.time(), time.monotonic()
    for lease in leases:
        if lease.expires_at_unix_ms <= wall * 1000:
            raise EgressDispatchDenied("TRANSPORT_LEASE_EXPIRED")
        if len(body) > lease.request_bytes_limit:
            raise EgressDispatchDenied("REQUEST_BYTES_EXCEEDED")
    from ares_runtime.dispatch_route import request_output_ceiling
    ceiling = request_output_ceiling(body, route=route)
    if any(ceiling > lease.output_tokens_limit for lease in leases):
        raise EgressDispatchDenied("OUTPUT_TOKENS_EXCEEDED")
    seconds = min(
        min(v.send_timeout_ms / 1000, v.expires_at_unix_ms / 1000 - wall)
        for v in leases
    )
    return SendBudget(leases, mono + seconds)


async def _bounded(budget, operation):
    try:
        with anyio.fail_after(budget.remaining()):
            result = await operation()
            budget.remaining()
            return result
    except TimeoutError as exc:
        raise EgressOutcomeUnrecorded(
            "TRANSPORT_DEADLINE_EXCEEDED", "total local send deadline elapsed"
        ) from exc
    except (httpcore.NetworkError, httpcore.TimeoutException) as exc:
        budget.remaining()
        # Preserve the distinction: a shorter idle timeout is not proof that
        # the total budget elapsed, and cannot authorize an unknown resend.
        raise EgressOutcomeUnrecorded(
            "TRANSPORT_IO_UNCONFIRMED", "network operation did not confirm an outcome"
        ) from exc


class _AsyncStream(httpcore.AsyncNetworkStream):
    def __init__(self, inner, budget):
        self.inner, self.budget = inner, budget

    async def read(self, max_bytes, timeout=None):
        return await _bounded(
            self.budget, lambda: self.inner.read(max_bytes, timeout=timeout)
        )

    async def write(self, buffer, timeout=None):
        return await _bounded(
            self.budget, lambda: self.inner.write(buffer, timeout=timeout)
        )

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        result = await _bounded(
            self.budget,
            lambda: self.inner.start_tls(ssl_context, server_hostname, timeout),
        )
        return _AsyncStream(result, self.budget)

    async def aclose(self):
        # Network backend close performs no response draining or TLS unwrap.
        with anyio.CancelScope(shield=True):
            await self.inner.aclose()

    def get_extra_info(self, info):
        return self.inner.get_extra_info(info)


class _AsyncBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, budget):
        self.budget, self.inner = budget, AnyIOBackend()
        self.sockets = set()
        self.transports = set()
        import threading

        self._lock = threading.Lock()
        self.expired = False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._timer = None
        else:
            self._timer = loop.call_later(
                max(0, budget.deadline_monotonic - time.monotonic()), self.abort
            )

    def close(self):
        if self._timer is not None:
            self._timer.cancel()

    def shutdown_sockets(self):
        # Sync owner's joined watchdog only shuts down owned sockets. It never
        # runs a provider callback, SDK operation, policy IPC or event loop.
        import socket
        import contextlib

        with self._lock:
            self.expired = True
            for sock in self.sockets:
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)

    def remember(self, stream):
        sock = stream.get_extra_info("socket")
        with self._lock:
            if sock is not None:
                self.sockets.add(sock)
                if self.expired:
                    import socket

                    sock.shutdown(socket.SHUT_RDWR)
            self.transports.add(stream._stream._transport)
        return stream

    def abort(self):
        # Synchronous GC cannot await async cleanup. Shut down owned fds now;
        # no task/thread is scheduled and no response draining can occur.
        self.close()
        self.shutdown_sockets()
        for transport in self.transports:
            transport.abort()
        self.sockets.clear()
        self.transports.clear()

    async def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        from ares_runtime.bounded_dns import resolve

        async def connect():
            addresses = await resolve(host, port)
            last = None
            for address in addresses:
                try:
                    return await self.inner.connect_tcp(
                        address,
                        port,
                        timeout=timeout,
                        local_address=local_address,
                        socket_options=socket_options,
                    )
                except httpcore.ConnectError as error:
                    last = error
            raise last or httpcore.ConnectError("no resolved address")

        return _AsyncStream(
            self.remember(await _bounded(self.budget, connect)), self.budget
        )

    async def connect_unix_socket(self, *args, **kwargs):
        stream = await _bounded(
            self.budget, lambda: self.inner.connect_unix_socket(*args, **kwargs)
        )
        return _AsyncStream(self.remember(stream), self.budget)

    async def sleep(self, seconds):
        await _bounded(self.budget, lambda: anyio.sleep(seconds))


class _SyncStream(httpcore.NetworkStream):
    def __init__(self, inner, owner):
        self.inner, self.owner = inner, owner

    def read(self, max_bytes, timeout=None):
        return self.owner.run(lambda: self.inner.read(max_bytes, timeout))

    def write(self, buffer, timeout=None):
        return self.owner.run(lambda: self.inner.write(buffer, timeout))

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return _SyncStream(
            self.owner.run(
                lambda: self.inner.start_tls(ssl_context, server_hostname, timeout)
            ),
            self.owner,
        )

    def close(self):
        self.owner.run(self.inner.aclose, closing=True)

    def get_extra_info(self, info):
        return self.inner.get_extra_info(info)


class _SyncBackend(httpcore.NetworkBackend):
    """Same-thread owned event loop, not a background provider worker.

    Each leased send gets its own backend and pool. There is no cross-send
    connection or HTTP/2 multiplexing and no inherited pool wait to outlive the
    deadline. TLS/proxy configuration stays on the copied original pool.
    """

    def __init__(self, budget):
        self.budget = budget
        self.inner = _AsyncBackend(budget)
        self.runner = asyncio.Runner()
        self.closed = False
        import threading

        self._watchdog = threading.Timer(
            max(0, budget.deadline_monotonic - time.monotonic()),
            self.inner.shutdown_sockets,
        )
        self._watchdog.daemon = True
        self._watchdog.start()

    def run(self, operation, *, closing=False):
        if self.closed:
            return None
        if not closing:
            self.budget.remaining()
        # Explicit context: asyncio.Runner otherwise caches its first Context.
        import contextvars

        return self.runner.run(operation(), context=contextvars.copy_context())

    def connect_tcp(self, *args, **kwargs):
        return _SyncStream(
            self.run(lambda: self.inner.connect_tcp(*args, **kwargs)), self
        )

    def connect_unix_socket(self, *args, **kwargs):
        return _SyncStream(
            self.run(lambda: self.inner.connect_unix_socket(*args, **kwargs)), self
        )

    def sleep(self, seconds):
        self.run(lambda: self.inner.sleep(seconds))

    def close(self):
        if not self.closed:
            self.closed = True
            self._watchdog.cancel()
            self._watchdog.join()
            self.inner.close()
            # Provider tasks have completed/cancelled on the same thread.
            self.runner.close()


# bounded_transport reaches into private pool, backend and cancel-scope
# internals of exactly these releases. pyproject.toml pins each one to the same
# version, so any install built from this release's dependency set is
# qualified; tests/ares_runtime/test_qualified_transport_backend.py holds the
# two in lockstep. A mismatch means the environment did not come from that set.
QUALIFIED_TRANSPORT_BACKEND: Mapping[str, str] = MappingProxyType({
    "httpx": "0.28.1",
    "httpcore": "1.0.9",
    "anyio": "4.12.1",
})
QUALIFIED_TRANSPORT_PLATFORM = "linux"


def _imported_version(package: str) -> str:
    """Version of the code this interpreter actually imports for ``package``.

    A module's own ``__version__`` wins over distribution metadata, because a
    shadowing copy on sys.path can disagree with the installed record. anyio
    publishes no ``__version__``, so it falls back to metadata.
    """
    import importlib
    from importlib.metadata import PackageNotFoundError, version

    try:
        module = importlib.import_module(package)
    except ImportError:
        return "missing"
    imported = getattr(module, "__version__", None)
    if isinstance(imported, str):
        return imported
    try:
        return version(package)
    except PackageNotFoundError:
        return "missing"


def transport_backend_mismatches() -> tuple[str, ...]:
    """Why this interpreter cannot run a leased transport; empty when qualified.

    Callable from a plain interpreter, so ``ares doctor`` can ask the selected
    runtime before any governed call is refused.
    """
    problems = []
    for package, qualified in QUALIFIED_TRANSPORT_BACKEND.items():
        installed = _imported_version(package)
        if installed != qualified:
            problems.append(f"{package} {installed} (qualified {qualified})")
    if sys.platform != QUALIFIED_TRANSPORT_PLATFORM:
        problems.append(
            f"platform {sys.platform} (qualified {QUALIFIED_TRANSPORT_PLATFORM})"
        )
    return tuple(problems)


def bounded_transport(
    inner: Any, budget: SendBudget, *, asynchronous: bool = False
) -> tuple[Any, Any]:
    """Clone configuration, not live connections; only pinned standard httpx pools.

    Preserve SSLContext, proxy URL/auth/TLS context, socket options, UDS and
    HTTP settings. Custom transports/backends must supply their own admission
    contract; their behavior cannot be inferred from a duck-typed attribute.
    """
    mismatches = transport_backend_mismatches()
    if mismatches:
        raise EgressDispatchDenied(
            "UNSUPPORTED_TRANSPORT",
            "network backend version/platform is not qualified: " + "; ".join(mismatches),
        )
    expected = httpx.AsyncHTTPTransport if asynchronous else httpx.HTTPTransport
    if type(inner) is not expected or not hasattr(inner, "_pool"):
        raise EgressDispatchDenied(
            "UNSUPPORTED_TRANSPORT", "lease requires a standard httpx transport"
        )
    pools = (
        (httpcore.AsyncConnectionPool, httpcore.AsyncHTTPProxy)
        if asynchronous
        else (httpcore.ConnectionPool, httpcore.HTTPProxy)
    )
    if type(inner._pool) not in pools:
        raise EgressDispatchDenied(
            "UNSUPPORTED_TRANSPORT", "custom/SOCKS pool is not qualified"
        )
    from httpcore._backends.auto import AutoBackend
    from httpcore._backends.sync import SyncBackend

    if type(inner._pool._network_backend) not in (
        AutoBackend,
        SyncBackend,
        AnyIOBackend,
    ):
        raise EgressDispatchDenied(
            "UNSUPPORTED_TRANSPORT", "custom network backend is not qualified"
        )
    if inner._pool._local_address is not None:
        import ipaddress

        try:
            ipaddress.ip_address(inner._pool._local_address)
        except ValueError:
            raise EgressDispatchDenied(
                "UNSUPPORTED_TRANSPORT", "local_address must be an IP literal"
            ) from None
    if asynchronous:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            raise EgressDispatchDenied(
                "UNSUPPORTED_TRANSPORT", "async lease requires asyncio"
            ) from None
    if not asynchronous:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise EgressDispatchDenied(
                "UNSUPPORTED_TRANSPORT", "synchronous lease inside a running event loop"
            )
    transport = copy.copy(inner)
    pool = copy.copy(inner._pool)
    pool._connections, pool._requests = [], []
    from httpcore._synchronization import ThreadLock, AsyncThreadLock

    pool._optional_thread_lock = AsyncThreadLock() if asynchronous else ThreadLock()
    # New pool and backend are owned by this send and closed with its body.
    backend = _AsyncBackend(budget) if asynchronous else _SyncBackend(budget)
    pool._network_backend = backend
    transport._pool = pool
    return transport, backend
