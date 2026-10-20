"""Versioned, token-free Desktop bootstrap possession proof.

Fixed-position JSON arrays are ASCII, compact encoded and direction-separated.
The boot secret never travels over HTTP. State is bounded and process-local.
"""
import hashlib
import hmac
import ipaddress
import json
import re
import threading
import time
from hermes_cli.dashboard_auth.origins import exact_origin

PROTOCOL = "hermes-native-v1"


def native_listener_endpoint(scope, host_header):
    """Bind to trusted ASGI listener metadata, never a caller's Host alone."""
    try:
        host, port = scope["server"]
        address = ipaddress.ip_address(host)
        if (scope.get("scheme") != "http" or not address.is_loopback
                or not isinstance(port, int) or isinstance(port, bool) or not 0 < port <= 65535):
            return None
        if exact_origin("http://" + host_header) != ("http", str(address), port):
            return None
        authority = f"[{address}]" if address.version == 6 else str(address)
        suffix = "" if port == 80 else f":{port}"
        return f"http://{authority}{suffix}/auth/desktop-bootstrap"
    except (KeyError, TypeError, ValueError):
        return None


def native_mac(key, parts):
    encoded = json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hmac.new(key.encode("ascii"), encoded, hashlib.sha256).hexdigest()


class NativeReplayWindow:
    def __init__(self, *, capacity=1024, clock=time.monotonic):
        self._capacity = capacity
        self._clock = clock
        self._nonces = {}
        self._lock = threading.Lock()

    def claim(self, identity, nonce):
        with self._lock:
            now = self._clock()
            for key in list(self._nonces):
                if self._nonces[key] <= now:
                    del self._nonces[key]
            key = (identity, nonce)
            if key in self._nonces or len(self._nonces) >= self._capacity:
                return False
            self._nonces[key] = now + 60
            return True


def verify_native_request(headers, endpoint, key, identity, replay, *, now=None):
    if not isinstance(key, str) or not re.fullmatch(r"[!-~]{32,256}", key):
        return None
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
        return None
    if headers.get("x-hermes-native-identity") != identity:
        return None
    nonce = headers.get("x-hermes-native-nonce", "")
    issued_text = headers.get("x-hermes-native-issued", "")
    proof = headers.get("x-hermes-native-proof", "")
    if not re.fullmatch(r"[0-9a-f]{64}", nonce) or not re.fullmatch(r"[0-9]{1,12}", issued_text) or not re.fullmatch(r"[0-9a-f]{64}", proof):
        return None
    issued = int(issued_text)
    observed = time.time() if now is None else now
    if issued < observed - 30 or issued > observed + 5 or not endpoint.isascii():
        return None
    expected = native_mac(key, [PROTOCOL, "request", endpoint, identity, nonce, issued])
    if not hmac.compare_digest(proof, expected) or not replay.claim(identity, nonce):
        return None
    return nonce, issued


def make_native_response(key, endpoint, identity, nonce, issued, session_token):
    digest = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
    return {
        "protocol": PROTOCOL, "identity": identity, "nonce": nonce,
        "issued_at": issued, "session_digest": digest,
        "proof": native_mac(key, [PROTOCOL, "response", endpoint, identity, nonce, issued, digest]),
    }
