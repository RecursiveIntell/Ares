"""Exact browser origin contract shared by HTTP, cookies and WebSockets.

Forwarded headers are interpreted only by uvicorn's explicit trusted-proxy
resolver. This module never reads X-Forwarded-* as an authority.
"""
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware


def exact_origin(value: str):
    """Parse a serialized HTTP origin; reject opaque or ambiguous values."""
    if not value or value != value.strip() or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    try:
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"} or not url.hostname
                or url.username is not None or url.password is not None
                or url.path or url.query or url.fragment or any(c in value for c in "\\?#")):
            return None
        port = url.port if url.port is not None else (443 if url.scheme == "https" else 80)
        if not 0 < port <= 65535 or url.netloc.endswith(":"):
            return None
        return url.scheme, url.hostname.lower(), port
    except ValueError:
        return None


def target_origin(scope):
    scheme = {"ws": "http", "wss": "https"}.get(scope["scheme"], scope["scheme"])
    return exact_origin(f"{scheme}://{Headers(scope=scope).get('host', '')}")


def origin_allowed(origin: str, scope) -> bool:
    parsed = exact_origin(origin)
    return parsed is not None and parsed == target_origin(scope)


class ExactOriginCORS:
    """Use Starlette's real preflight handling with a request-exact origin.

    The target Host remains subject to the server's Host admission middleware.
    Native clients do not need browser CORS and receive no opaque-origin grant.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        origin = Headers(scope=scope).get("origin", "") if scope["type"] == "http" else ""
        cors = CORSMiddleware(
            self.app, allow_origins=[origin] if origin_allowed(origin, scope) else [],
            allow_methods=["*"], allow_headers=["*"],
        ) if scope["type"] == "http" else self.app
        await cors(scope, receive, send)
