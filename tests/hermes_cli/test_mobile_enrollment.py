from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from hermes_cli.dashboard_auth.mobile_enrollment import (
    ENROLLMENT_DOMAIN,
    canonical_enrollment_message,
    normalize_scopes,
    public_key_fingerprint,
    verify_enrollment_signature,
)


def _message() -> bytes:
    return canonical_enrollment_message(
        challenge_id="challenge-1",
        challenge="one-use-challenge",
        host_id="a" * 64,
        app_instance_id="phone-install-1",
        requested_scopes=["session:read", "session:read", " session:control "],
        expires_at=1_760_000_000.25,
    )


def test_enrollment_message_is_stable_and_scope_normalized() -> None:
    assert normalize_scopes(["session:read", " session:control ", "session:read"]) == (
        "session:control",
        "session:read",
    )
    message = _message()
    assert message.startswith(f"{ENROLLMENT_DOMAIN}\0".encode("ascii"))
    assert b'"expires_at":"1760000000.250000"' in message
    assert b'"requested_scopes":["session:control","session:read"]' in message


def test_enrollment_scope_limits_fail_closed() -> None:
    with pytest.raises(ValueError, match="too large"):
        normalize_scopes([f"scope:{index}" for index in range(17)])
    with pytest.raises(ValueError, match="too large"):
        normalize_scopes(["x" * 97])


def test_ecdsa_enrollment_proof_verifies_and_invalid_proof_fails() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    message = _message()
    signature = private_key.sign(message, ec.ECDSA(hashes.SHA256()))

    assert verify_enrollment_signature(
        public_key_der=public_der,
        signature=signature,
        message=message,
    ) == public_key_fingerprint(public_der)

    with pytest.raises(ValueError, match="invalid mobile enrollment signature"):
        verify_enrollment_signature(
            public_key_der=public_der,
            signature=private_key.sign(b"different", ec.ECDSA(hashes.SHA256())),
            message=message,
        )


def test_empty_public_key_or_signature_is_rejected() -> None:
    with pytest.raises(ValueError, match="public key"):
        public_key_fingerprint(b"")
    with pytest.raises(ValueError, match="signature"):
        verify_enrollment_signature(public_key_der=b"not-a-key", signature=b"", message=b"message")
