"""Actual native route and bounded replay-control witnesses, synthetic state."""
import hashlib
import json
import secrets
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hermes_cli import web_server as ws
from hermes_cli.dashboard_auth.native_bootstrap import NativeReplayWindow, native_mac

KEY = "synthetic-owned-child-secret-32-bytes-minimum"
IDENTITY = "synthetic-child"
ENDPOINT = "http://127.0.0.1:9119/auth/desktop-bootstrap"


def headers(*, endpoint=ENDPOINT, identity=IDENTITY, nonce=None, issued=None, direction="request"):
    nonce = nonce or secrets.token_hex(32)
    issued = int(time.time()) if issued is None else issued
    return {
        "X-Hermes-Native-Identity": identity, "X-Hermes-Native-Nonce": nonce,
        "X-Hermes-Native-Issued": str(issued),
        "X-Hermes-Native-Proof": native_mac(KEY, ["hermes-native-v1", direction, endpoint, identity, nonce, issued]),
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(ws.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(ws.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(ws.app.state, "trusted_public_hosts", frozenset(), raising=False)
    monkeypatch.setattr(ws, "_DESKTOP_BOOTSTRAP_KEY", KEY)
    monkeypatch.setattr(ws, "_DESKTOP_BOOTSTRAP_ID", IDENTITY)
    monkeypatch.setattr(ws, "_NATIVE_BOOTSTRAP_NONCES", NativeReplayWindow())
    with TestClient(ws.app, base_url="http://127.0.0.1:9119") as value:
        yield value


def test_native_response_is_token_free_bound_and_not_replayable(client):
    request = headers()
    response = client.get("/auth/desktop-bootstrap", headers=request)
    assert response.status_code == 200
    body = response.json()
    digest = hashlib.sha256(ws._SESSION_TOKEN.encode()).hexdigest()
    nonce = request["X-Hermes-Native-Nonce"]
    issued = int(request["X-Hermes-Native-Issued"])
    assert body == {
        "protocol": "hermes-native-v1", "identity": IDENTITY, "nonce": nonce,
        "issued_at": issued, "session_digest": digest,
        "proof": native_mac(KEY, ["hermes-native-v1", "response", ENDPOINT, IDENTITY, nonce, issued, digest]),
    }
    assert ws._SESSION_TOKEN not in response.text and KEY not in response.text
    assert client.get("/auth/desktop-bootstrap", headers=request).status_code == 403


@pytest.mark.parametrize("kind", ["missing", "raw-secret", "wrong-endpoint", "foreign-child", "expired", "future", "reflection", "origin", "malformed"])
def test_invalid_possession_proofs_are_denied(client, kind):
    request = headers()
    if kind == "missing": request = {}
    elif kind == "raw-secret": request = {"X-Hermes-Native-Bootstrap": KEY, "X-Hermes-Native-Identity": IDENTITY}
    elif kind == "wrong-endpoint": request = headers(endpoint="http://127.0.0.1:9220/auth/desktop-bootstrap")
    elif kind == "foreign-child": request = headers(identity="foreign-child")
    elif kind == "expired": request = headers(issued=int(time.time()) - 31)
    elif kind == "future": request = headers(issued=int(time.time()) + 6)
    elif kind == "reflection": request = headers(direction="response")
    elif kind == "origin": request["Origin"] = "http://127.0.0.1:9119"
    elif kind == "malformed": request["X-Hermes-Native-Nonce"] = "x" * 5000
    response = client.get("/auth/desktop-bootstrap", headers=request)
    assert response.status_code == 403
    assert ws._SESSION_TOKEN not in response.text
    assert not ws._NATIVE_BOOTSTRAP_NONCES._nonces


def test_relayed_host_cannot_change_owned_listener_binding(client):
    # Simulates forwarding the parent's valid proof for a different listener
    # while spoofing Host. The ASGI transport still belongs to port 9119.
    request = headers(endpoint="http://127.0.0.1:9220/auth/desktop-bootstrap")
    request["Host"] = "127.0.0.1:9220"
    response = client.get("/auth/desktop-bootstrap", headers=request)
    assert response.status_code == 403
    assert not ws._NATIVE_BOOTSTRAP_NONCES._nonces


def test_replay_capacity_never_evicts_live_nonces():
    now = [0.0]
    replay = NativeReplayWindow(capacity=1, clock=lambda: now[0])
    assert replay.claim("a", "one")
    assert not replay.claim("a", "one")
    assert not replay.claim("a", "two")
    now[0] = 60.0
    assert replay.claim("a", "two")


def test_cross_language_golden_vectors():
    fixture = Path(__file__).resolve().parents[2] / "tests/fixtures/p2-native-bootstrap-v1.json"
    row = json.loads(fixture.read_text(encoding="utf-8"))
    request = [row["protocol"], "request", row["endpoint"], row["identity"], row["nonce"], row["issued_at"]]
    assert native_mac(row["synthetic_key"], request) == row["request_mac"]
    assert native_mac(row["synthetic_key"], [row["protocol"], "response", *request[2:], row["session_digest"]]) == row["response_mac"]
