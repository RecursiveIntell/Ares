"""Local owner admission with a recording native protocol peer.

Native cryptographic/permit semantics have their separate paired-source gate.
This suite proves SQLite, key and captured-response constraints in Ares.
"""
from copy import deepcopy
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import pytest

from ares_runtime.collaboration import DaemonPermitReceiptAdapter
from ares_runtime.continuity.credentials import ControllerCredentialError
from hermes_state_continuity import ContextContinuationError
from tests.ares_runtime.test_context_controller_credentials import db  # noqa: F401


pytestmark = pytest.mark.linux_only
TRANSPORT = {"socket_path": "/tmp/test-context.sock", "timeout_seconds": 1}
INCARNATION = "1" * 64


@pytest.fixture
def native(db, monkeypatch):
    db.create_session("s", source="cli", profile_name="p")
    db.append_message("s", "user", "Do the bounded work")
    assert db.try_acquire_session_turn_lease("s", "holder", ttl_seconds=300)
    public = db.initialize_context_controller()
    scope = {"store": "2" * 64, "profile": "p", "root": "s"}
    head = {"context": "s", "generation": 1, "mode": "active"}
    authority = {"incarnation": INCARNATION, "scope": scope, "grant_digest": "3" * 64,
                 "policy_digest": "4" * 64, "head": head}
    state = {"authority": authority, "approval_verifier": "5" * 64,
             "configuration_revision": 1, "enrolled": True, "effects_charged": 0,
             "transitions_charged": 0, "consumed": [], "grant": {
                 "scope": scope, "controller_public_key": public["public_key"], "actor": {},
                 "policy_version": "test", "policy_digest": "4" * 64,
                 "write_root": "/tmp/test-worktree", "initial_head": head,
                 "not_before": "2026-01-01T00:00:00Z", "expires_at": "2099-01-01T00:00:00Z",
                 "max_effects": 20, "max_transitions": 20, "previous_grant": None}}
    calls = []

    def request(_adapter, kind, **fields):
        assert not db._conn.in_transaction, "Native RPC must not hold SQLite write transaction"
        calls.append((kind, deepcopy(fields)))
        if kind == "context_store_identity":
            assert fields == {"nonce": public["store_nonce"]}
            return {"store": scope["store"]}
        if kind == "context_authority_readback":
            return {"snapshot": deepcopy(state)}
        if kind == "context_call_prepare":
            assert fields["witness"] == {"opaque_witness": "exact-approved-call"}
            material = {"authority": fields["authority"], "witness_digest": "6" * 64, "signature": []}
            payload = ["recursive-agent.context-call/v1", fields["authority"], "6" * 64]
            return {"material": material, "signing_bytes": list(json.dumps(payload, separators=(",", ":")).encode())}
        raise AssertionError(kind)

    monkeypatch.setattr(DaemonPermitReceiptAdapter, "context_request", request)
    return state, calls, request


def enroll(db):
    return db.enroll_native_context_authority("s", turn_lease_holder="holder", transport=TRANSPORT,
                                              incarnation=INCARNATION)


def admit(db):
    snapshot = db.read_context_rebase_snapshot("s")
    db.admit_context_dispatch("s", turn_lease_holder="holder", attempt_id="a",
                              expected_snapshot_digest=snapshot.digest,
                              payload_digest="sha256:" + "7" * 64, route_ref="test")
    db.settle_context_dispatch_response("a", turn_lease_holder="holder")


def sign(db, registration):
    return db.sign_native_context_call(session_id="s", turn_lease_holder="holder", attempt_id="a",
                                       registration=registration, witness={"opaque_witness": "exact-approved-call"})


def test_enrollment_binds_actual_store_profile_root_and_survives_disabled_flags(db, native):
    value = enroll(db)
    assert value == enroll(db)
    assert value["authority"]["scope"] == {"store": "2" * 64, "profile": "p", "root": "s"}
    assert db.context_dispatch_required_for_session("s")
    assert db.read_native_context_authority("s") == value


@pytest.mark.parametrize("mutation", ["key", "scope", "incarnation", "generation", "used", "revoked"])
def test_first_enrollment_refuses_foreign_or_previously_used_native_authority(db, native, mutation):
    state, _, _ = native
    if mutation == "key":
        state["grant"]["controller_public_key"] = [0] * 32
    elif mutation == "scope":
        state["authority"]["scope"] = {**state["authority"]["scope"], "profile": "other"}
    elif mutation == "incarnation":
        state["authority"]["incarnation"] = "f" * 64
    elif mutation == "generation":
        state["authority"]["head"] = {"context": "s", "generation": 2, "mode": "active"}
    elif mutation == "used":
        state["effects_charged"] = 1
    else:
        state["enrolled"] = False
    with pytest.raises((ContextContinuationError, ControllerCredentialError)):
        enroll(db)
    assert db.read_native_context_authority("s") is None


def test_signature_is_for_the_exact_native_message_after_local_response_admission(db, native):
    registration = enroll(db)
    admit(db)
    signed = sign(db, registration)
    payload = ["recursive-agent.context-call/v1", registration["authority"], "6" * 64]
    public = Ed25519PublicKey.from_public_bytes(bytes(db.read_context_controller_identity()["public_key"]))
    public.verify(bytes(signed["signature"]), json.dumps(payload, separators=(",", ":")).encode())


@pytest.mark.parametrize("mutation", ["no_response", "input", "stop", "lease", "native_head", "approval_verifier"])
def test_signer_refuses_unsettled_or_stale_captured_response(db, native, mutation):
    registration = enroll(db)
    if mutation != "no_response":
        admit(db)
    if mutation == "input":
        db.append_message("s", "user", "New correction")
    elif mutation == "stop":
        db.record_context_stop("s")
    elif mutation == "lease":
        db.release_session_turn_lease("s", "holder")
        assert db.try_acquire_session_turn_lease("s", "new-holder", ttl_seconds=300)
    elif mutation == "native_head":
        native[0]["authority"]["head"] = {"context": "next", "generation": 2, "mode": "active"}
    elif mutation == "approval_verifier":
        native[0]["approval_verifier"] = "f" * 64
    with pytest.raises((ContextContinuationError, ControllerCredentialError)):
        sign(db, registration)


def test_signer_refuses_native_domain_substitution(db, native, monkeypatch):
    registration = enroll(db)
    admit(db)
    original = native[2]

    def corrupt(adapter, kind, **fields):
        result = original(adapter, kind, **fields)
        if kind == "context_call_prepare":
            result["signing_bytes"] = list(b'["arbitrary-domain","payload"]')
        return result

    monkeypatch.setattr(DaemonPermitReceiptAdapter, "context_request", corrupt)
    with pytest.raises(ControllerCredentialError, match="SIGNING_MATERIAL_INVALID"):
        sign(db, registration)
