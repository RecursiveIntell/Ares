from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from hermes_cli import web_server
from hermes_cli.dashboard_auth import middleware, routes
from hermes_cli.dashboard_auth.public_paths import MOBILE_PROOF_API_PATHS


class _DB:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def close(self) -> None:
        return None

    def mobile_enrollment_create_challenge(self, **kwargs):
        self.calls.append(("challenge", kwargs))
        return {"challenge_id": "challenge-1", "challenge": "secret", **kwargs}

    def mobile_device_complete_enrollment(self, **kwargs):
        self.calls.append(("complete", kwargs))
        return {"device_id": "device-1", "device_token": "opaque"}


def _request(*, user_id: str = "owner", provider: str = "native-pkce"):
    return SimpleNamespace(state=SimpleNamespace(session=SimpleNamespace(user_id=user_id, provider=provider)))


def test_only_proof_endpoints_bypass_dashboard_cookie_gate() -> None:
    assert MOBILE_PROOF_API_PATHS == {
        "/api/mobile/enrollment/complete",
        "/api/mobile/auth/refresh",
    }
    assert middleware._path_is_public("/api/mobile/enrollment/complete")
    assert middleware._path_is_public("/api/mobile/auth/refresh")
    assert not middleware._path_is_public("/api/mobile/enrollment/challenge")
    assert not middleware._path_is_public("/api/mobile/devices")


def test_challenge_requires_session_and_binds_authenticated_owner(monkeypatch) -> None:
    db = _DB()
    monkeypatch.setattr(routes, "SessionDB", lambda **_kwargs: db)
    monkeypatch.setattr(routes, "host_identity_digest", lambda: "a" * 64)

    result = asyncio.run(
        routes.create_mobile_enrollment_challenge(
            _request(),
            routes.MobileEnrollmentChallengeRequest(
                label="phone",
                requested_scopes=["session:read"],
            ),
        )
    )

    assert result["challenge_id"] == "challenge-1"
    assert db.calls == [
        (
            "challenge",
            {
                "user_id": "owner",
                "provider": "native-pkce",
                "label": "phone",
                "host_id": "a" * 64,
                "requested_scopes": ["session:read"],
            },
        )
    ]

    with pytest.raises(HTTPException, match="native bearer authentication"):
        asyncio.run(
            routes.create_mobile_enrollment_challenge(
                _request(user_id="", provider=""),
                routes.MobileEnrollmentChallengeRequest(),
            )
        )


def test_completion_rejects_invalid_encoding_and_forwards_only_decoded_proof(monkeypatch) -> None:
    db = _DB()
    monkeypatch.setattr(routes, "SessionDB", lambda **_kwargs: db)
    monkeypatch.setattr(routes, "host_identity_digest", lambda: "b" * 64)

    with pytest.raises(HTTPException, match="invalid enrollment proof encoding"):
        asyncio.run(
            routes.complete_mobile_enrollment(
                routes.MobileEnrollmentCompleteRequest(
                    challenge_id="challenge-1",
                    challenge="secret",
                    app_instance_id="install-1",
                    public_key_der_b64="*not-base64*",
                    signature_b64="*not-base64*",
                )
            )
        )
    assert db.calls == []

    result = asyncio.run(
        routes.complete_mobile_enrollment(
            routes.MobileEnrollmentCompleteRequest(
                challenge_id="challenge-1",
                challenge="secret",
                app_instance_id="install-1",
                public_key_der_b64=base64.b64encode(b"der").decode(),
                signature_b64=base64.b64encode(b"signature").decode(),
            )
        )
    )
    assert result["device_id"] == "device-1"
    assert db.calls == [
        (
            "complete",
            {
                "challenge_id": "challenge-1",
                "challenge": "secret",
                "host_id": "b" * 64,
                "app_instance_id": "install-1",
                "public_key_der": b"der",
                "signature": b"signature",
            },
        )
    ]


def test_routes_refuse_unbound_host_identity(monkeypatch) -> None:
    monkeypatch.setattr(routes, "host_identity_digest", lambda: "unbound")
    with pytest.raises(HTTPException, match="host identity is not configured"):
        asyncio.run(
            routes.create_mobile_enrollment_challenge(
                _request(), routes.MobileEnrollmentChallengeRequest()
            )
        )


def test_legacy_gate_honors_only_mobile_proof_paths(monkeypatch) -> None:
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(web_server, "_has_valid_session_token", lambda _request: False)
    monkeypatch.setattr(web_server, "_has_valid_query_token", lambda _request, _path: False)

    async def next_response(_request):
        return Response(status_code=204)

    def request(path: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": [],
                "scheme": "http",
                "server": ("test", 80),
                "client": ("127.0.0.1", 1234),
                "app": web_server.app,
            }
        )

    assert asyncio.run(
        web_server.auth_middleware(request("/api/mobile/enrollment/complete"), next_response)
    ).status_code == 204
    assert asyncio.run(
        web_server.auth_middleware(request("/api/mobile/auth/refresh"), next_response)
    ).status_code == 204
    assert asyncio.run(
        web_server.auth_middleware(request("/api/mobile/enrollment/challenge"), next_response)
    ).status_code == 401
