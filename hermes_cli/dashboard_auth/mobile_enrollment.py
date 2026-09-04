"""Canonical challenge-bound proof helpers for mobile enrollment.

The host owns the challenge record and the device owns the non-exportable
private key.  Only the public key and a signature over this exact canonical
message cross the enrollment boundary; bearer credentials are minted only
after the server verifies the proof and consumes the one-use challenge.
"""
from __future__ import annotations

import hashlib
import json
from typing import Iterable


ENROLLMENT_DOMAIN = "ares-mobile-enrollment-v1"


def normalize_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    """Return a bounded, deterministic, duplicate-free scope tuple."""
    normalized = sorted({str(scope).strip() for scope in scopes if str(scope).strip()})
    if len(normalized) > 16 or any(len(scope) > 96 for scope in normalized):
        raise ValueError("mobile enrollment scope set is too large")
    return tuple(normalized)


def canonical_enrollment_message(
    *,
    challenge_id: str,
    challenge: str,
    host_id: str,
    app_instance_id: str,
    requested_scopes: Iterable[str],
    expires_at: float,
) -> bytes:
    """Encode the exact bytes signed by the Android Keystore key."""
    fields = {
        "app_instance_id": str(app_instance_id),
        "challenge": str(challenge),
        "challenge_id": str(challenge_id),
        "expires_at": format(float(expires_at), ".6f"),
        "host_id": str(host_id),
        "requested_scopes": list(normalize_scopes(requested_scopes)),
    }
    return (
        ENROLLMENT_DOMAIN.encode("ascii")
        + b"\0"
        + json.dumps(fields, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def public_key_fingerprint(public_key_der: bytes) -> str:
    """Return the stable SHA-256 fingerprint of a DER public key."""
    if not public_key_der:
        raise ValueError("public key is required")
    return hashlib.sha256(public_key_der).hexdigest()


def verify_enrollment_signature(
    *,
    public_key_der: bytes,
    signature: bytes,
    message: bytes,
) -> str:
    """Verify an EC/SHA-256 signature and return the public-key fingerprint."""
    if not signature:
        raise ValueError("enrollment signature is required")
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        public_key = serialization.load_der_public_key(public_key_der)
        if not isinstance(public_key, ec.EllipticCurvePublicKey):
            raise ValueError("mobile enrollment requires an EC public key")
        public_key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("invalid mobile enrollment signature") from exc
    return public_key_fingerprint(public_key_der)