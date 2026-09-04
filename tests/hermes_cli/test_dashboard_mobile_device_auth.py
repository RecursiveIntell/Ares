from __future__ import annotations

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from hermes_cli.dashboard_auth import (
    TokenPrincipal,
    assert_protocol_compliance,
    clear_providers,
    list_session_providers,
    list_token_providers,
)
from hermes_cli.dashboard_auth import token_auth
from hermes_cli.dashboard_auth.mobile_device import (
    MobileDeviceAuthProvider,
    register_mobile_device_provider,
)
from hermes_cli.dashboard_auth.mobile_enrollment import canonical_enrollment_message
from hermes_state import SessionDB


def _enroll(db: SessionDB) -> dict:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    challenge = db.mobile_enrollment_create_challenge(
        user_id="user-1",
        provider="native-pkce",
        label="phone",
        host_id="f" * 64,
        requested_scopes=["mobile:device"],
    )
    signature = private_key.sign(
        canonical_enrollment_message(
            challenge_id=challenge["challenge_id"],
            challenge=challenge["challenge"],
            host_id=challenge["host_id"],
            app_instance_id="test-app",
            requested_scopes=challenge["requested_scopes"],
            expires_at=challenge["expires_at"],
        ),
        ec.ECDSA(hashes.SHA256()),
    )
    return db.mobile_device_complete_enrollment(
        challenge_id=challenge["challenge_id"],
        challenge=challenge["challenge"],
        host_id=challenge["host_id"],
        app_instance_id="test-app",
        public_key_der=public_key_der,
        signature=signature,
    )


def test_provider_is_token_only_and_protocol_compliant() -> None:
    assert_protocol_compliance(MobileDeviceAuthProvider)
    provider = MobileDeviceAuthProvider()
    assert provider.supports_session is False
    assert provider.supports_token is True


def test_provider_resolves_active_device_and_rejects_revoked_or_unknown(tmp_path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        enrolled = _enroll(db)
        provider = MobileDeviceAuthProvider(
            db_factory=lambda: SessionDB(db_path=tmp_path / "state.db")
        )
        assert provider.verify_token(token=enrolled["device_token"]) == TokenPrincipal(
            principal=enrolled["device_id"],
            provider="mobile-device",
            scopes=("mobile:device",),
        )
        assert provider.verify_token(token="unknown") is None
        assert db.mobile_device_revoke(
            device_id=enrolled["device_id"],
            user_id="user-1",
            provider="native-pkce",
        )
        assert provider.verify_token(token=enrolled["device_token"]) is None
    finally:
        db.close()


def test_registration_uses_existing_registry_and_excludes_session_auth() -> None:
    clear_providers()
    try:
        provider = register_mobile_device_provider()
        assert list_token_providers() == [provider]
        assert provider not in list_session_providers()
        assert register_mobile_device_provider() is provider
    finally:
        clear_providers()


def test_provider_registers_no_generic_token_routes() -> None:
    token_auth.clear_token_routes()
    try:
        register_mobile_device_provider()
        assert not token_auth.is_token_route("/api/gateway/drain")
        assert not token_auth.is_token_route("/api/mobile")
    finally:
        clear_providers()
        token_auth.clear_token_routes()
