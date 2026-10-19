"""Pinned standard httpx configuration conversion, never live-pool copying.

An async facade cannot cancel a sync provider worker. Convert only the standard
transport contract while preserving TLS, proxy auth, mounts and HTTP settings.
Custom transports/hooks require the caller to supply an explicit AsyncClient.
"""

from __future__ import annotations
import httpcore
import httpx
from hermes_cli.middleware import EgressDispatchDenied


def async_client_from_sync(client: httpx.Client) -> httpx.AsyncClient:
    from ares_runtime.physical_dispatch import _SyncTransport

    if not isinstance(client, httpx.Client) or any(client.event_hooks.values()):
        raise EgressDispatchDenied(
            "UNSUPPORTED_TRANSPORT",
            "async conversion requires standard httpx client without sync hooks",
        )
    converted = {}

    def convert(transport):
        if transport is None:
            return None
        if id(transport) in converted:
            return converted[id(transport)]
        key = id(transport)
        if isinstance(transport, _SyncTransport):
            transport = transport.inner
        if type(transport) is not httpx.HTTPTransport:
            raise EgressDispatchDenied(
                "UNSUPPORTED_TRANSPORT",
                "custom sync transport cannot be made cancellable",
            )
        pool = transport._pool
        if type(pool) not in (httpcore.ConnectionPool, httpcore.HTTPProxy):
            raise EgressDispatchDenied(
                "UNSUPPORTED_TRANSPORT",
                "custom/SOCKS pool async conversion not qualified",
            )
        config = {
            name: getattr(pool, "_" + name)
            for name in (
                "ssl_context",
                "max_connections",
                "max_keepalive_connections",
                "keepalive_expiry",
                "http1",
                "http2",
                "retries",
                "local_address",
                "uds",
                "socket_options",
            )
        }
        if type(pool) is httpcore.HTTPProxy:
            config.update({
                name: getattr(pool, "_" + name)
                for name in ("proxy_url", "proxy_headers", "proxy_ssl_context")
            })
            # httpcore has already normalized proxy credentials into headers.
            new_pool = httpcore.AsyncHTTPProxy(**config)
        else:
            new_pool = httpcore.AsyncConnectionPool(**config)
        new = httpx.AsyncHTTPTransport()
        new._pool = new_pool
        converted[key] = new
        return new

    # Materialize all conversions before constructing an owning client.
    transport = convert(client._transport)
    mounts = {
        pattern.pattern: convert(value) for pattern, value in client._mounts.items()
    }
    return httpx.AsyncClient(
        transport=transport,
        mounts=mounts,
        trust_env=False,
        headers=client.headers,
        cookies=client.cookies,
        auth=client._auth,
        timeout=client.timeout,
        follow_redirects=client.follow_redirects,
        max_redirects=client.max_redirects,
        default_encoding=client._default_encoding,
    )
