"""Ordinary opaque route references remain valid without native enrollment.

A receipt's route is an identifier, not evidence of a native grant. This
positive control prevents a blanket rejection of previously persisted opaque
routes while the enrolled-root refusal is qualified separately.
"""

import json
from types import SimpleNamespace

import pytest

from ares_runtime.continuity.runtime import ContextDispatchError, admit_final_context_dispatch
from hermes_state import SessionDB
from tests.ares_runtime.test_context_authority_binding import db, enroll, native  # noqa: F401


def test_ordinary_opaque_dispatch_route_survives_reopen_without_enrollment(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    try:
        db.create_session("s", source="cli")
        db.append_message("s", "user", "ordinary question")
        assert db.try_acquire_session_turn_lease("s", "holder", ttl_seconds=300)
        assert db.read_native_context_authority("s") is None
        snapshot = db.read_context_rebase_snapshot("s")
        route = "opaque:provider-switch:v1"
        admitted = db.admit_context_dispatch(
            "s", turn_lease_holder="holder", attempt_id="ordinary-1",
            expected_snapshot_digest=snapshot.digest,
            payload_digest="sha256:" + "a" * 64, route_ref=route,
        )
        assert admitted["route_ref"] == route
        db.settle_context_dispatch_response("ordinary-1", turn_lease_holder="holder")
    finally:
        db.close()

    reopened = SessionDB(path)
    try:
        assert reopened.read_native_context_authority("s") is None
        receipt = reopened.get_meta("context-dispatch:ordinary-1")
        settlement = reopened.get_meta("context-dispatch-result:ordinary-1")
        assert receipt is not None and settlement is not None
        assert json.loads(receipt)["route_ref"] == route
        assert json.loads(settlement)["disposition"] == "response_received"
    finally:
        reopened.close()


@pytest.mark.linux_only
def test_enrolled_root_refuses_opaque_provider_before_dispatch_mutation(db, native):
    """The ordinary opaque receipt above is not a native dispatch grant."""
    registration = enroll(db)
    snapshot = db.read_context_rebase_snapshot("s")
    agent = SimpleNamespace(
        session_id="s", _session_db=db, _active_session_turn_lease_holder="holder",
        provider="acp", api_mode="acp", model="opaque-model", base_url="acp://local",
        max_tokens=100, context_compressor=SimpleNamespace(context_length=10000),
    )
    assert db.get_meta("context-dispatch:unsafe-1") is None
    with pytest.raises(ContextDispatchError, match="PROVIDER_CONTEXT_RESET_UNQUALIFIED"):
        admit_final_context_dispatch(
            agent, snapshot,
            {
                "model": "opaque-model",
                "messages": [{"role": "user", "content": "Do the bounded work"}],
                "max_tokens": 100,
            },
            attempt_id="unsafe-1",
        )
    assert db.read_native_context_authority("s") == registration
    assert db.read_context_rebase_snapshot("s").digest == snapshot.digest
    assert db.get_meta("context-dispatch:unsafe-1") is None
    assert db.get_meta("context-dispatch-result:unsafe-1") is None
