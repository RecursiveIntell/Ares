from __future__ import annotations

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from hermes_cli.dashboard_auth.mobile_enrollment import canonical_enrollment_message
from hermes_state import SessionDB


def _enroll(db: SessionDB) -> dict:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    challenge = db.mobile_enrollment_create_challenge(
        user_id="user",
        provider="stub",
        label="phone",
        host_id="a" * 64,
        requested_scopes=["session:control", "session:read"],
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


def test_mobile_device_token_is_hashed_and_revocation_survives_reopen(tmp_path) -> None:
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    try:
        enrolled = _enroll(db)
        token = enrolled["device_token"]
        assert db.mobile_device_verify_token(token)["device_id"] == enrolled["device_id"]
        assert token not in str(db._conn.execute("SELECT token_digest FROM mobile_devices").fetchone()[0])
        assert db.mobile_device_revoke(device_id=enrolled["device_id"], user_id="user", provider="stub") is True
    finally:
        db.close()
    reopened = SessionDB(db_path=path)
    try:
        assert reopened.mobile_device_verify_token(token) is None
    finally:
        reopened.close()


def test_mobile_enrollment_challenge_is_one_use_and_wrong_proof_does_not_consume(tmp_path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        private_key = ec.generate_private_key(ec.SECP256R1())
        public_key_der = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        challenge = db.mobile_enrollment_create_challenge(
            user_id="user",
            provider="stub",
            label="phone",
            host_id="b" * 64,
            requested_scopes=["session:read"],
        )
        wrong_signature = private_key.sign(b"wrong", ec.ECDSA(hashes.SHA256()))
        try:
            db.mobile_device_complete_enrollment(
                challenge_id=challenge["challenge_id"],
                challenge=challenge["challenge"],
                host_id=challenge["host_id"],
                app_instance_id="test-app",
                public_key_der=public_key_der,
                signature=wrong_signature,
            )
        except ValueError as exc:
            assert "signature" in str(exc)
        else:
            raise AssertionError("invalid signature was accepted")

        valid_signature = private_key.sign(
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
        enrolled = db.mobile_device_complete_enrollment(
            challenge_id=challenge["challenge_id"],
            challenge=challenge["challenge"],
            host_id=challenge["host_id"],
            app_instance_id="test-app",
            public_key_der=public_key_der,
            signature=valid_signature,
        )
        assert enrolled["scopes"] == ["session:read"]
        try:
            db.mobile_device_complete_enrollment(
                challenge_id=challenge["challenge_id"],
                challenge=challenge["challenge"],
                host_id=challenge["host_id"],
                app_instance_id="test-app",
                public_key_der=public_key_der,
                signature=valid_signature,
            )
        except ValueError as exc:
            assert "consumed" in str(exc)
        else:
            raise AssertionError("challenge replay was accepted")
    finally:
        db.close()


def test_mobile_refresh_rotates_and_reuse_revokes_device(tmp_path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        enrolled = _enroll(db)
        rotated = db.mobile_device_refresh(enrolled["refresh_token"])
        assert rotated is not None
        assert rotated["access_token"] != enrolled["access_token"]
        assert db.mobile_device_verify_token(enrolled["access_token"]) is None
        active = db.mobile_device_verify_token(rotated["access_token"])
        assert active is not None
        assert active["device_id"] == enrolled["device_id"]

        # Reusing the previous refresh credential is a token-theft signal, not
        # a normal authentication miss: revoke the device principal.
        assert db.mobile_device_refresh(enrolled["refresh_token"]) is None
        assert db.mobile_device_verify_token(rotated["access_token"]) is None
    finally:
        db.close()
