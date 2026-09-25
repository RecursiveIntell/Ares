"""Ares owner integration; the paired native job owns cryptographic protocol proof."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ares_runtime.collaboration import ContractError, DaemonPermitReceiptAdapter
from ares_runtime.continuity.runtime import context_tool_control_scope
from tests.ares_runtime.test_context_authority_binding import (  # noqa: F401
    db, native, enroll, admit,
)

pytestmark = pytest.mark.linux_only


@pytest.fixture
def peer(db, native, monkeypatch):
    state, calls, original = native
    permits, transitions = {}, {}
    faults = set()

    def request(adapter, kind, **fields):
        assert not db._conn.in_transaction
        if kind not in {"scoped_permit_issue", "scoped_permit_consume", "scoped_permit_outcome_record",
                        "scoped_permit_readback", "context_transition_prepare",
                        "context_authority_transition", "context_transition_readback"}:
            return original(adapter, kind, **fields)
        calls.append((kind, deepcopy(fields)))
        if kind == "context_transition_prepare":
            material = {"authority": fields["authority"], "action": fields["action"],
                        "transition": ("8" if fields["action"]["action"] == "retire" else "9") * 64,
                        "signature": []}
            payload = ["recursive-agent.context-transition/v1", fields["authority"],
                       material["transition"], fields["action"]]
            return {"material": material, "signing_bytes": list(json.dumps(payload).encode())}
        if kind == "context_transition_readback":
            return {"receipt": deepcopy(transitions.get(fields["transition"]["transition"]))}
        if kind == "context_authority_transition":
            transition = fields["transition"]
            key = transition["transition"]
            if key not in transitions:
                assert transition["authority"] == state["authority"]
                action = transition["action"]
                old = state["authority"]["head"]
                successor = ({"context": action["successor_context"], "generation": old["generation"] + 1,
                              "mode": "sealed"} if action["action"] == "retire" else {**old, "mode": "active"})
                receipt = {"request": transition, "successor": successor, "consumed": deepcopy(state["consumed"]),
                           "recorded_at": "2026-09-25T00:00:00Z", "receipt_digest": key}
                transitions[key] = deepcopy(receipt)
                state["authority"]["head"] = successor
                state["transitions_charged"] += 1
            if "transition_ack" in faults:
                faults.remove("transition_ack")
                raise ContractError("CONTEXT_NATIVE_ACK_UNKNOWN")
            return {"receipt": deepcopy(transitions[key])}
        if kind == "scoped_permit_issue":
            permit = {"permit_id": "permit:test:one", "effect": {}, "context": fields["context"],
                      "approval_verifier": state["approval_verifier"]}
            permits[permit["permit_id"]] = {"permit": deepcopy(permit), "preflight": None, "outcome": None}
            return {"permit": permit}
        record = permits[fields["permit"]["permit_id"]]
        if kind == "scoped_permit_consume":
            assert record["preflight"] is None
            record["preflight"] = {"permit": deepcopy(fields["permit"]),
                "recorded_at": "2026-09-25T00:00:00Z", "receipt_digest": "a" * 64}
            if "consume_ack" in faults:
                raise ContractError("CONTEXT_NATIVE_ACK_UNKNOWN")
            return {"preflight": deepcopy(record["preflight"])}
        if kind == "scoped_permit_outcome_record":
            record["outcome"] = {"permit_id": fields["permit"]["permit_id"],
                "preflight_receipt_digest": fields["preflight_receipt_digest"], "reported": fields["reported"],
                "recorded_at": "2026-09-25T00:00:01Z", "receipt_digest": "b" * 64}
            if "outcome_ack" in faults:
                raise ContractError("CONTEXT_NATIVE_ACK_UNKNOWN")
            return {"outcome": deepcopy(record["outcome"])}
        return {"record": deepcopy(record)}

    monkeypatch.setattr(DaemonPermitReceiptAdapter, "context_request", request)
    return state, calls, permits, faults


def bound_agent(db):
    registration = enroll(db)
    admit(db)
    return SimpleNamespace(session_id="s", _session_db=db, context_rebase_enabled=False,
        _context_response_admission={"attempt_id": "a"}, _active_session_turn_lease_holder="holder",
        _context_response_native_authority=registration)


@pytest.mark.parametrize("fault", [None, "consume_ack", "outcome_ack"])
def test_actual_registry_scoped_permit_uses_final_arguments_and_exact_outcome(db, peer, fault):
    import model_tools
    _, calls, permits, faults = peer
    agent = bound_agent(db)
    if fault:
        faults.add(fault)
    def middleware(name, args, execute, **kwargs):
        return execute({**args, "content": "final"})
    with (context_tool_control_scope(agent),
          patch("agent.context_input.validate_turn_input_authority"),
          patch("ares_runtime.collaboration.GatewayProductionApprovalWitnessProvider.issue_witness",
                return_value={"opaque_witness": "exact-approved-call"}),
          patch("acp_adapter.edit_approval.maybe_require_edit_approval", return_value=None),
          patch("hermes_cli.middleware.run_tool_execution_middleware", side_effect=middleware),
          patch.object(model_tools.registry, "dispatch", return_value='{"ok":true}') as effect):
        result = model_tools.handle_function_call("write_file", {"path": "/tmp/test-worktree/out", "content": "initial"},
                                                  session_id="s", task_id="mission")
    record = permits["permit:test:one"]
    if fault == "consume_ack":
        effect.assert_not_called()
        assert "CONTEXT_NATIVE_ACK_UNKNOWN" in result
        assert record["outcome"] is None
    else:
        effect.assert_called_once()
        assert effect.call_args.args[1]["content"] == "final"
        assert record["outcome"]["reported"]["state"] == "succeeded"
    consume = next(fields for kind, fields in calls if kind == "scoped_permit_consume")
    assert consume["call"]["args"]["content"] == "final"


def publish(db):
    value = db.read_context_rebase_snapshot("s")
    return db.publish_context_rebase_child(transition_id="transition", parent_session_id="s", child_session_id="child",
        continuation_digest="sha256:" + "c" * 64, expected_snapshot_digest=value.digest,
        control_revision=value.control_revision, input_watermark=value.input_watermark,
        turn_lease_holder="holder", source="cli", system_prompt="trusted", profile_name="p",
        messages=[{"role": "assistant", "content": "continuation", "display_kind": "hidden"},
                  {"role": "user", "content": "Do the bounded work"}])


@pytest.mark.parametrize("ack_lost", [False, True])
def test_retirement_precedes_publication_and_activation_resolves_exact_ack(db, peer, ack_lost):
    state, _, _, faults = peer
    enroll(db)
    if ack_lost:
        faults.add("transition_ack")
    transition = publish(db)
    assert transition.state == "committed_pending_activation"
    assert state["authority"]["head"] == {"context": "child", "generation": 2, "mode": "sealed"}
    reservation = db.begin_context_rebase_recovery("transition", turn_lease_holder="holder")
    assert reservation["attempts"] == 2
    if ack_lost:
        faults.add("transition_ack")
    db.activate_native_context_rebase("transition", session_id="child", turn_lease_holder="holder",
        reservation={"transition_id": "transition", "attempt": reservation["attempts"],
                     "control_digest": reservation["action_control_digest"]})
    db.mark_context_rebase_ready("transition", expected_continuation_digest=transition.continuation_digest,
        expected_child_session_id="child", turn_lease_holder="holder", recovery_attempt=reservation["attempts"],
        expected_control_digest=reservation["action_control_digest"])
    assert db.assert_context_rebase_ready_for_turn("child").state == "ready"
    assert state["transitions_charged"] == 2


def test_consumed_reported_success_is_preserved_and_blocks_automatic_activation(db, peer):
    state, _, _, _ = peer
    enroll(db)
    obligation = {"permit_id": "permit:test:old", "preflight_receipt_digest": "a" * 64,
                  "outcome_receipt_digest": "b" * 64, "reported_state": "succeeded"}
    state["consumed"] = [obligation]
    publish(db)
    reservation = db.begin_context_rebase_recovery("transition", turn_lease_holder="holder")
    with pytest.raises(Exception, match="CONTEXT_NATIVE_EFFECT_RECONCILIATION_REQUIRED"):
        db.activate_native_context_rebase("transition", session_id="child", turn_lease_holder="holder",
            reservation={"transition_id": "transition", "attempt": reservation["attempts"],
                         "control_digest": reservation["action_control_digest"]})
    assert db.read_native_context_rebase("child")["retire_receipt"]["consumed"] == [obligation]
    assert state["authority"]["head"]["mode"] == "sealed"


def test_rebased_child_keeps_captured_gateway_approval_route(db, peer, tmp_path):
    import model_tools
    import tools.approval as approval
    state, _, _, _ = peer
    state["grant"]["write_root"] = str(tmp_path)
    enroll(db)
    transition = publish(db)
    reservation = db.begin_context_rebase_recovery("transition", turn_lease_holder="holder")
    db.activate_native_context_rebase("transition", session_id="child", turn_lease_holder="holder",
        reservation={"transition_id": "transition", "attempt": reservation["attempts"],
                     "control_digest": reservation["action_control_digest"]})
    db.mark_context_rebase_ready("transition", expected_continuation_digest=transition.continuation_digest,
        expected_child_session_id="child", turn_lease_holder="holder", recovery_attempt=reservation["attempts"],
        expected_control_digest=reservation["action_control_digest"])
    snapshot = db.read_context_rebase_snapshot("child")
    db.admit_context_dispatch("child", turn_lease_holder="holder", attempt_id="child-response",
        expected_snapshot_digest=snapshot.digest, payload_digest="sha256:" + "7" * 64, route_ref="test")
    db.settle_context_dispatch_response("child-response", turn_lease_holder="holder")
    agent = SimpleNamespace(session_id="child", _session_db=db, context_rebase_enabled=False,
        _context_response_admission={"attempt_id": "child-response"}, _active_session_turn_lease_holder="holder",
        _context_response_native_authority=db.read_native_context_authority("child"))
    route = "gateway:original-turn"
    notified = []
    approval.register_gateway_notify(route, notified.append)
    token = approval.set_current_session_key(route)
    def decision(session_key, notify, request, **kwargs):
        assert session_key == route
        notify(request)
        return {"resolved": True, "choice": "once", "witness": {"opaque_witness": "exact-approved-call"}}
    try:
        with (context_tool_control_scope(agent), patch("agent.context_input.validate_turn_input_authority"),
              patch.object(approval, "_await_gateway_decision", side_effect=decision),
              patch("acp_adapter.edit_approval.maybe_require_edit_approval", return_value=None),
              patch.object(model_tools.registry, "dispatch", return_value='{"ok":true}') as effect):
            changed = approval.set_current_session_key("unrelated-later-route")
            try:
                result = model_tools.handle_function_call("write_file", {"path": str(tmp_path / "out"), "content": "approved"},
                                                          task_id="mission", session_id="child")
            finally:
                approval.reset_current_session_key(changed)
        assert json.loads(result).get("ok") is True, result
        effect.assert_called_once()
        assert len(notified) == 1
    finally:
        approval.reset_current_session_key(token)
        approval.unregister_gateway_notify(route)


@pytest.mark.parametrize("generation", [True, 1.0])
def test_prepared_signing_bytes_refuse_type_coercion(db, native, generation):
    from ares_runtime.continuity.authority import signing_material
    registration = enroll(db)
    authority = registration["authority"]
    wrong = deepcopy(authority)
    wrong["head"]["generation"] = generation
    payload = ["recursive-agent.context-call/v1", wrong, "6" * 64]
    with pytest.raises(Exception, match="CONTEXT_AUTHORITY_SIGNING_MATERIAL_INVALID"):
        signing_material({"material": {"authority": authority, "witness_digest": "6" * 64, "signature": []},
                          "signing_bytes": list(json.dumps(payload).encode())}, authority=authority)
