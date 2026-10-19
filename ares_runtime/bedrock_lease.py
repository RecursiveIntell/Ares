"""Bounded botocore prepared-send executor; never reserializes/signs a body.

This is a local network backend selection, not a provider endpoint/proxy. The
prepared URL, auth headers and bytes remain owned by botocore. Unsupported proxy
forwarding/experimental header semantics are denied rather than approximated.
"""

import os
import ssl

import httpx
from botocore.awsrequest import AWSResponse
from botocore.httpsession import get_cert_path
from hermes_cli.middleware import EgressDispatchDenied
from ares_runtime.transport_lease import bounded_transport


class _Raw:
    def __init__(self, response, transport, backend):
        self.response, self.transport, self.backend = response, transport, backend
        self.iterator = iter(response.stream)
        self.buffer = b""
        self.closed = False

    def stream(self, amt=65536, decode_content=False):
        # HTTPX transport stream is raw (no Response.iter_bytes decoding).
        try:
            if self.buffer:
                yield self.buffer
                self.buffer = b""
            yield from self.iterator
        finally:
            self.close()

    def read(self, amt=None):
        if amt is None:
            return b"".join(self.stream())
        while len(self.buffer) < amt and not self.closed:
            try:
                self.buffer += next(self.iterator)
            except StopIteration:
                self.close()
        result, self.buffer = self.buffer[:amt], self.buffer[amt:]
        return result

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.response.close()
            finally:
                try:
                    self.transport.close()
                finally:
                    self.backend.close()


def send_bounded(session, prepared, budget):
    if session._proxy_config.settings.get("proxy_use_forwarding_for_https"):
        raise EgressDispatchDenied(
            "UNSUPPORTED_TRANSPORT", "Bedrock HTTPS proxy forwarding is not qualified"
        )
    if os.environ.get("BOTO_EXPERIMENTAL__ADD_PROXY_HOST_HEADER", "").lower() in (
        "1",
        "true",
    ):
        raise EgressDispatchDenied(
            "UNSUPPORTED_TRANSPORT",
            "experimental Bedrock proxy Host rewrite is not qualified",
        )
    context = session._get_ssl_context()
    if session._verify:
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.load_verify_locations(cafile=get_cert_path(session._verify))
    else:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    if session._cert_file:
        context.load_cert_chain(session._cert_file, session._key_file)
    proxy_url = session._proxy_config.proxy_url_for(prepared.url)
    proxy = None
    if proxy_url:
        proxy = httpx.Proxy(
            proxy_url,
            ssl_context=session._setup_proxy_ssl_context(proxy_url),
            headers=session._proxy_config.proxy_headers_for(proxy_url),
        )
    original = httpx.HTTPTransport(
        verify=context, proxy=proxy, socket_options=session._socket_options
    )
    try:
        transport, backend = bounded_transport(original, budget)
    finally:
        original.close()
    timeout = session._timeout
    if isinstance(timeout, (float, int)):
        connect = read = timeout
    else:
        connect, read = timeout.connect_timeout, timeout.read_timeout
    request = httpx.Request(
        prepared.method,
        prepared.url,
        headers=dict(prepared.headers.items()),
        content=prepared.body,
        extensions={
            "timeout": {
                "connect": connect,
                "read": read,
                "write": read,
                "pool": connect,
            }
        },
    )
    try:
        response = transport.handle_request(request)
    except BaseException:
        try:
            transport.close()
        finally:
            backend.close()
        raise
    return AWSResponse(
        prepared.url,
        response.status_code,
        response.headers,
        _Raw(response, transport, backend),
    )
