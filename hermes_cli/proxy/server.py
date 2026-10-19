"""HTTP server that forwards OpenAI-compatible requests to a configured upstream.

Listens on ``http://<host>:<port>/v1/<path>`` and forwards each request to
``<upstream-base-url>/<path>`` with the client's ``Authorization`` header
replaced by a freshly-resolved bearer from the configured adapter. The
response is streamed back unmodified, preserving SSE.

The server is intentionally minimal: it does NOT mediate, log, transform,
or rewrite request/response bodies. It's a credential-attaching forwarder.
"""

from __future__ import annotations

import asyncio
import logging
import hmac
import ipaddress
from urllib.parse import urlsplit
import signal
from typing import Optional

try:
    import aiohttp
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None  # type: ignore[assignment]
    web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential

logger = logging.getLogger(__name__)

# Headers we strip when forwarding to the upstream. ``host``/``content-length``
# are recomputed by aiohttp; ``authorization`` is replaced with our bearer.
# Everything else (content-type, accept, user-agent, x-* headers) passes through.
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "authorization",  # we replace this one
    }
)

DEFAULT_PORT = 8645
DEFAULT_HOST = "127.0.0.1"
# Body cap for forwarded requests. Chat-completion payloads with long agent
# conversations can be large; mirror api_server's MAX_REQUEST_BYTES (10 MB).
# client_max_size bounds every read path, including chunked bodies.
MAX_REQUEST_BYTES = 10_000_000


def _json_error(status: int, message: str, code: str = "proxy_error") -> "web.Response":
    """Return an OpenAI-style error JSON response."""
    body = {"error": {"message": message, "type": code, "code": code}}
    return web.json_response(body, status=status)


def _filter_request_headers(headers: "aiohttp.typedefs.LooseHeaders") -> dict:
    """Strip hop-by-hop + auth headers from the inbound request."""
    out = {}
    for key, value in headers.items():
        if key.lower() in _HOP_BY_HOP_HEADERS:
            continue
        out[key] = value
    return out


def _filter_response_headers(headers) -> dict:
    """Strip hop-by-hop headers from the upstream response."""
    out = {}
    for key, value in headers.items():
        if key.lower() in _HOP_BY_HOP_HEADERS:
            continue
        # aiohttp recomputes Content-Encoding/Content-Length on stream — let it.
        if key.lower() in {"content-encoding", "content-length"}:
            continue
        out[key] = value
    return out


def validate_bind(host: str, client_token: str | None) -> None:
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if client_token is not None and len(client_token) < 32:
        raise ValueError("Proxy client credential must contain at least 32 characters")
    if not loopback and not client_token:
        raise ValueError("Nonloopback proxy binding requires a distinct client credential")


def create_app(adapter: UpstreamAdapter, *, host: str = DEFAULT_HOST,
               port: int = 0, client_token: str | None = None) -> "web.Application":
    """Build the aiohttp application bound to a specific upstream adapter."""
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError(
            "aiohttp is required for `hermes proxy`. Run `hermes setup` to install it."
        )

    validate_bind(host, client_token)

    @web.middleware
    async def client_boundary(request, handler):
        # Host/Origin and client auth precede health adapters, credentials,
        # body reads and upstream I/O. No browser CORS grant is exposed.
        try:
            from hermes_cli.dashboard_auth.origins import exact_origin
            authority = request.headers.get("Host", "")
            if exact_origin(f"http://{authority}") is None:
                return _json_error(403, "Host denied")
            target = urlsplit(f"http://{authority}")
            name = target.hostname
            target_port = target.port if target.port is not None else 80
            if not name or target.username or target.password or target.path or target.query or target.fragment:
                return _json_error(403, "Host denied")
            try:
                local_bind = ipaddress.ip_address(host).is_loopback
                local_host = ipaddress.ip_address(name).is_loopback
            except ValueError:
                local_bind = host == "localhost" or host in {"127.0.0.1", "::1"}
                local_host = name in {"localhost", "127.0.0.1", "::1"}
            sock = request.transport.get_extra_info("sockname") if request.transport else None
            host_ok = local_host if local_bind else name == host
            if host in {"0.0.0.0", "::"}:
                host_ok = bool(sock) and name == sock[0]
            if not host_ok:
                return _json_error(403, "Host denied")
            effective_port = sock[1] if sock else port
            if effective_port and target_port != effective_port:
                return _json_error(403, "Host port denied")
        except ValueError:
            return _json_error(403, "Host denied")
        origin = request.headers.get("Origin")
        if origin is not None:
            from hermes_cli.dashboard_auth.origins import exact_origin
            if exact_origin(origin) != exact_origin(f"http://{request.headers.get('Host', '')}") or exact_origin(origin) is None:
                return _json_error(403, "Origin denied")
        if client_token:
            auth = request.headers.get("Authorization", "")
            if not hmac.compare_digest(auth.encode(), f"Bearer {client_token}".encode()):
                return _json_error(401, "Client authorization required")
        if request.method == "OPTIONS":
            return web.Response(status=204)
        return await handler(request)

    app = web.Application(client_max_size=MAX_REQUEST_BYTES, middlewares=[client_boundary])
    # AppKey ensures forward-compat with future aiohttp versions that strip
    # bare-string keys.
    _adapter_key = web.AppKey("adapter", UpstreamAdapter)
    app[_adapter_key] = adapter

    async def handle_health(request: "web.Request") -> "web.Response":
        return web.json_response(
            {
                "status": "ok",
                "upstream": adapter.display_name,
                "authenticated": adapter.is_authenticated(),
            }
        )

    async def handle_proxy(request: "web.Request") -> "web.StreamResponse":
        # Extract the path *after* /v1
        rel_path = request.match_info.get("tail", "")
        rel_path = "/" + rel_path.lstrip("/")

        if rel_path not in adapter.allowed_paths:
            allowed = ", ".join(sorted(adapter.allowed_paths))
            return _json_error(
                404,
                f"Path /v1{rel_path} is not forwarded by this proxy. "
                f"Allowed: {allowed}",
                code="path_not_allowed",
            )

        try:
            cred = adapter.get_credential()
        except Exception as exc:
            logger.warning("proxy: credential resolution failed: %s", exc)
            return _json_error(401, str(exc), code="upstream_auth_failed")

        if client_token and hmac.compare_digest(client_token.encode(), cred.bearer.encode()):
            return _json_error(403, "Client and upstream credentials must be distinct")

        # Forward body verbatim. Read into memory once — request bodies for
        # chat/completions/embeddings are small (<1MB typically). If we ever
        # need to forward large multipart uploads we'll switch to streaming
        # the request body too.
        body = await request.read()

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=300)

        async def _send_upstream(active_cred: UpstreamCredential):
            if client_token and hmac.compare_digest(client_token.encode(), active_cred.bearer.encode()):
                raise RuntimeError("Client and upstream credentials must be distinct")
            upstream_url = f"{active_cred.base_url.rstrip('/')}{rel_path}"
            # Preserve query string verbatim.
            if request.query_string:
                upstream_url = f"{upstream_url}?{request.query_string}"

            fwd_headers = _filter_request_headers(request.headers)
            fwd_headers["Authorization"] = f"{active_cred.token_type} {active_cred.bearer}"

            logger.debug(
                "proxy: forwarding %s %s -> %s (body=%d bytes)",
                request.method, rel_path, upstream_url, len(body),
            )

            try:
                session = aiohttp.ClientSession(timeout=timeout)
            except Exception as exc:  # pragma: no cover - aiohttp setup issue
                raise RuntimeError(f"proxy session init failed: {exc}") from exc

            try:
                from ares_runtime.physical_dispatch import unsupported_transport_scope
                with unsupported_transport_scope('standalone_aiohttp_provider_proxy'):
                    upstream_resp = await session.request(
                        request.method,
                        upstream_url,
                        data=body if body else None,
                        headers=fwd_headers,
                        allow_redirects=False,
                    )
            except Exception:
                await session.close()
                raise
            return session, upstream_resp

        async def _open_upstream(active_cred: UpstreamCredential):
            from hermes_cli.middleware import EgressDispatchDenied, EgressOutcomeUnrecorded
            try:
                return await _send_upstream(active_cred)
            except EgressDispatchDenied as exc:
                return _json_error(403, str(exc), code=exc.code), None
            except EgressOutcomeUnrecorded as exc:
                return _json_error(422, str(exc), code=exc.code), None
            except RuntimeError as exc:
                return _json_error(500, str(exc)), None
            except aiohttp.ClientError as exc:
                logger.warning("proxy: upstream connection failed: %s", exc)
                return (
                    _json_error(
                        502,
                        f"upstream connection failed: {exc}",
                        code="upstream_unreachable",
                    ),
                    None,
                )
            except asyncio.TimeoutError:
                return (
                    _json_error(
                        504,
                        "upstream request timed out",
                        code="upstream_timeout",
                    ),
                    None,
                )

        session_or_response, upstream_resp = await _open_upstream(cred)
        if upstream_resp is None:
            return session_or_response
        session = session_or_response

        if upstream_resp.status in {401, 429}:
            try:
                retry_cred = adapter.get_retry_credential(
                    failed_credential=cred,
                    status_code=upstream_resp.status,
                )
            except Exception as exc:
                logger.warning("proxy: retry credential resolution failed: %s", exc)
                retry_cred = None

            if retry_cred is not None:
                upstream_resp.release()
                await session.close()
                session_or_response, upstream_resp = await _open_upstream(retry_cred)
                if upstream_resp is None:
                    return session_or_response
                session = session_or_response

        if 300 <= upstream_resp.status < 400:
            upstream_resp.release()
            await session.close()
            return _json_error(502, "Upstream redirect refused")

        # Stream response back. Headers first, then chunked body.
        resp = web.StreamResponse(
            status=upstream_resp.status,
            headers=_filter_response_headers(upstream_resp.headers),
        )
        await resp.prepare(request)

        try:
            async for chunk in upstream_resp.content.iter_any():
                if chunk:
                    await resp.write(chunk)
        except (aiohttp.ClientError, asyncio.CancelledError) as exc:
            logger.warning("proxy: streaming interrupted: %s", exc)
        finally:
            upstream_resp.release()
            await session.close()

        await resp.write_eof()
        return resp

    # /health doesn't go through the upstream
    app.router.add_get("/health", handle_health)
    # Catch-all under /v1 — forwards if the path is allowed.
    app.router.add_route("*", "/v1/{tail:.*}", handle_proxy)

    return app


async def run_server(
    adapter: UpstreamAdapter,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    shutdown_event: Optional[asyncio.Event] = None,
    client_token: str | None = None,
) -> None:
    """Run the proxy in the current event loop until shutdown_event is set.

    If shutdown_event is None, runs until cancelled (Ctrl+C or SIGTERM).
    """
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError(
            "aiohttp is required for `hermes proxy`. Run `hermes setup` to install it."
        )

    # Preserve the localhost convenience alias without trusting hosts/DNS
    # resolution to keep a credential-free listener on loopback.
    if host == "localhost":
        host = "127.0.0.1"
    validate_bind(host, client_token)
    app = create_app(adapter, host=host, port=port, client_token=client_token)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()

    logger.info(
        "proxy: listening on http://%s:%d/v1 -> %s",
        host, port, adapter.display_name,
    )

    stop_event = shutdown_event or asyncio.Event()

    # Wire signal handlers when we own the loop's lifetime.
    if shutdown_event is None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)  # windows-footgun: ok
            except NotImplementedError:
                # Windows / restricted environments — Ctrl+C will still
                # raise KeyboardInterrupt and unwind us.
                pass

    try:
        await stop_event.wait()
    finally:
        logger.info("proxy: shutting down")
        await runner.cleanup()


__all__ = [
    "create_app",
    "run_server",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "AIOHTTP_AVAILABLE",
]
