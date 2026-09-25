"""Ares wire-adapter reconciliation; native owner witnesses live in its crate."""
import copy
import json
import struct

import pytest

from ares_runtime.collaboration import (
    ConsumedPermitSettlement, ContractError, DaemonPermitReceiptAdapter,
)


PERMIT = "permit:fixture"
BINDING = {"native_binding": "opaque"}
DIGEST = "a" * 64
REPORTED = {"state": "succeeded", "duration_ms": 7, "error_type": None}


def outcome(reported=None):
    return {"permit_id": PERMIT, "preflight_receipt_digest": DIGEST,
            "reported": REPORTED if reported is None else reported,
            "recorded_at": "2026-09-25T00:00:00Z", "receipt_digest": "b" * 64}


def history():
    return {"schema": "recursive-agent.external-permit-readback/v1",
            "permit_id": PERMIT, "binding": BINDING,
            "state": {"state": "consumed", "consumed_at": "2026-09-25T00:00:00Z"},
            "preflight": {"permit_id": PERMIT, "receipt_digest": DIGEST},
            "outcome": outcome()}


def receipt():
    return {"permit_ref": PERMIT, "preflight_receipt": {"receipt_digest": DIGEST},
            "permit_binding": BINDING, "state": "ok", "duration_ms": 7, "error_type": None}


class FramedTransport:
    """Only the transport is replaced. Adapter framing and parsing are real."""
    def __init__(self, responder, requests):
        self.responder, self.requests, self.data = responder, requests, b""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def sendall(self, frame):
        size = struct.unpack(">I", frame[:4])[0]
        assert size == len(frame) - 4
        request = json.loads(frame[4:])
        self.requests.append(request)
        if self.responder is None:
            return  # Daemon mutation happened, response was lost.
        response = self.responder(request)
        payload = json.dumps(response).encode()
        self.data = struct.pack(">I", len(payload)) + payload

    def recv(self, length):
        result, self.data = self.data[:length], self.data[length:]
        return result


def adapter_with(monkeypatch, *responders):
    adapter = DaemonPermitReceiptAdapter({"mode": "production_per_call"})
    requests = []
    pending = iter(responders)
    monkeypatch.setattr(adapter, "_connect", lambda: FramedTransport(next(pending), requests))
    return adapter, requests


def read_response(request, value=None):
    assert request["request"] == {"kind": "permit_readback", "permit_id": PERMIT,
                                  "binding": BINDING, "preflight_receipt_digest": DIGEST}
    return {"request_id": request["request_id"], "permit_id": PERMIT,
            "readback": history() if value is None else value}


def test_lost_outcome_ack_has_one_readback_and_no_effect_replay(monkeypatch):
    adapter, requests = adapter_with(monkeypatch, None, read_response)
    adapter.record_receipt(receipt())
    assert [r["request"]["kind"] for r in requests] == ["permit_outcome_record", "permit_readback"]
    assert requests[0]["request"]["reported"] == REPORTED


@pytest.mark.parametrize("mutation", ["missing", "changed_report", "wrong_binding", "wrong_digest", "wrong_permit", "issued", "unknown_field"])
def test_lost_ack_unconfirmed_or_conflicting_readback_stays_closed(monkeypatch, mutation):
    value = copy.deepcopy(history())
    if mutation == "missing":
        value["outcome"] = None
    elif mutation == "changed_report":
        value["outcome"]["reported"]["duration_ms"] += 1
    elif mutation == "wrong_binding":
        value["binding"] = {"other": "binding"}
    elif mutation == "wrong_digest":
        value["preflight"]["receipt_digest"] = "c" * 64
    elif mutation == "wrong_permit":
        value["outcome"]["permit_id"] = "permit:other"
    elif mutation == "issued":
        value["state"] = {"state": "issued"}
    else:
        value["execution_allowed"] = True
    adapter, requests = adapter_with(monkeypatch, None, lambda r: read_response(r, value))
    with pytest.raises(ContractError):
        adapter.record_receipt(receipt())
    assert len(requests) == 2


def test_missing_binding_or_readback_transport_never_retries_mutation(monkeypatch):
    adapter, requests = adapter_with(monkeypatch, None)
    unbound = receipt()
    del unbound["permit_binding"]
    with pytest.raises(ContractError, match="PERMIT_BRIDGE_UNAVAILABLE"):
        adapter.record_receipt(unbound)
    assert len(requests) == 1
    adapter, requests = adapter_with(monkeypatch, None, None)
    with pytest.raises(ContractError, match="PERMIT_BRIDGE_UNAVAILABLE"):
        adapter.record_receipt(receipt())
    assert len(requests) == 2


def test_native_ambiguous_report_shape_is_accepted_without_invented_state(monkeypatch):
    selected = receipt()
    selected["state"] = "ambiguous"
    reported = {**REPORTED, "state": "outcome_ambiguous"}
    adapter, requests = adapter_with(monkeypatch, lambda r: {
        "request_id": r["request_id"], "permit_id": PERMIT, "outcome_artifact": outcome(reported),
    })
    adapter.record_receipt(selected)
    assert len(requests) == 1


def test_settlement_pins_binding_and_consume_time_adapter(monkeypatch):
    adapter, requests = adapter_with(monkeypatch, None, read_response)
    settlement = ConsumedPermitSettlement({"canonical_permit_ref": PERMIT,
        "receipt_artifact": {"receipt_digest": DIGEST}, "issued_binding": BINDING}, adapter)
    selected = receipt()
    selected["permit_binding"] = {"caller": "cannot replace consume binding"}
    settlement.record_receipt(selected)
    assert requests[1]["request"]["binding"] == BINDING
    selected["permit_ref"] = "permit:foreign"
    with pytest.raises(ContractError, match="PERMIT_SETTLEMENT_IDENTITY_MISMATCH"):
        settlement.record_receipt(selected)
    assert len(requests) == 2


def test_actual_dispatcher_calls_tool_once_when_outcome_ack_is_lost(monkeypatch, tmp_path):
    import ares_runtime.collaboration as collaboration
    import model_tools

    persisted = {}

    def issued(request):
        assert request["request"]["kind"] == "permit_issue_production"
        return {"request_id": request["request_id"], "permit_id": PERMIT, "binding": BINDING}

    def consumed(request):
        assert request["request"]["kind"] == "permit_consume"
        return {"request_id": request["request_id"], "permit_id": PERMIT, "evidence": {},
                "preflight_artifact": {}, "receipt_artifact": {"receipt_digest": DIGEST}}

    def lost(request):
        assert request["request"]["kind"] == "permit_outcome_record"
        persisted["reported"] = request["request"]["reported"]
        raise OSError("response lost after native persistence")

    def recovered(request):
        value = history()
        value["outcome"] = outcome(persisted["reported"])
        return read_response(request, value)

    adapter, requests = adapter_with(monkeypatch, issued, consumed, lost, recovered)
    monkeypatch.setattr(adapter, "_production_approval_witness", lambda **_: {"signed": "native-verifies"})
    monkeypatch.setattr(DaemonPermitReceiptAdapter, "from_ares_config", lambda _: adapter)
    monkeypatch.setattr(collaboration, "production_permit_canary_context", lambda **_: collaboration.ProductionPermitCanaryContext(
        "session:test", str(tmp_path / "daemon.sock"), str(tmp_path), 1.0))
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda _: False)
    monkeypatch.setattr("hermes_cli.observability.handles_hook", lambda _: False)
    monkeypatch.setattr("acp_adapter.edit_approval.maybe_require_edit_approval", lambda *_args, **_kwargs: None)
    effects = []
    def dispatch(name, args, **_kwargs):
        effects.append((name, args))
        return json.dumps({"bytes_written": len(args["content"])})
    monkeypatch.setattr(model_tools.registry, "dispatch", dispatch)
    result = model_tools.handle_function_call("write_file", {"path": str(tmp_path / "result"), "content": "exact"},
        task_id="task:test", session_id="session:test", skip_pre_tool_call_hook=True,
        skip_tool_request_middleware=True, skip_tool_execution_middleware=True)
    assert json.loads(result) == {"bytes_written": 5}
    assert len(effects) == 1
    assert [r["request"]["kind"] for r in requests] == [
        "permit_issue_production", "permit_consume", "permit_outcome_record", "permit_readback"]
