"""Positive and negative scope controls; no model, secret or network effects."""
from types import SimpleNamespace

import httpx
import pytest

from ares_runtime.physical_dispatch import (
    bind_deferred_result, bind_http_client, explicit_reasoning_request_scope, inference_call_scope,
)
from hermes_cli.middleware import EgressDispatchDenied


@pytest.mark.parametrize("bad", [{"enabled": 1, "effort": "high"}, {"enabled": True},
    {"enabled": True, "effort": "HIGH"}, {"enabled": True, "effort": "high", "unknown": True}])
def test_scope_requires_normalized_data_and_a_real_owner(bad):
    with pytest.raises(EgressDispatchDenied):
        with explicit_reasoning_request_scope(object(), bad):
            pytest.fail("invalid request became active")
    with pytest.raises(EgressDispatchDenied):
        with explicit_reasoning_request_scope(None, {"enabled": True, "effort": "high"}):
            pytest.fail("missing owner became active")


@pytest.mark.parametrize("effort,accepted", [("high", True), ("medium", False), (None, False)])
def test_deferred_callback_keeps_bound_intent_after_outer_scope_exit(effort, accepted):
    owner = SimpleNamespace(session_id="deferred-fixture")
    sent = []
    body = {"model": "fixture", "max_output_tokens": 1, "input": "synthetic"}
    if effort is not None:
        body["reasoning"] = {"effort": effort}
    with bind_http_client(httpx.Client(transport=httpx.MockTransport(
            lambda req: sent.append(bytes(req.content)) or httpx.Response(200, json={"ok": True})))) as client:
        def callback():
            client.post("https://fixture.invalid/v1/responses", json=body)
            yield "synthetic"
        with explicit_reasoning_request_scope(owner, {"enabled": True, "effort": "high"}):
            with inference_call_scope(owner, body) as basis:
                deferred = bind_deferred_result(callback(), basis)
        if accepted:
            assert list(deferred) == ["synthetic"] and len(sent) == 1
        else:
            with pytest.raises(EgressDispatchDenied):
                list(deferred)
            assert sent == []


def test_unowned_inference_send_is_refused_while_explicit_request_is_active():
    sent = []
    with bind_http_client(httpx.Client(transport=httpx.MockTransport(
            lambda request: sent.append(True) or httpx.Response(200, json={})))) as client:
        with explicit_reasoning_request_scope(object(), {"enabled": True, "effort": "high"}):
            with pytest.raises(EgressDispatchDenied) as error:
                client.post("https://fixture.invalid/v1/responses", json={"model": "fixture", "reasoning": {"effort": "high"}})
        assert error.value.code == "EXPLICIT_REASONING_UNBOUND" and sent == []


@pytest.mark.parametrize("native_effort,accepted", [("none", True), ("high", False), (None, False)])
def test_explicit_disable_needs_a_native_disabled_representation(native_effort, accepted):
    owner = SimpleNamespace(session_id="disable-fixture")
    sent = []
    body = {"model": "fixture", "max_output_tokens": 1}
    if native_effort is not None:
        body["reasoning"] = {"effort": native_effort}
    with bind_http_client(httpx.Client(transport=httpx.MockTransport(
            lambda request: sent.append(True) or httpx.Response(200, json={})))) as client:
        with explicit_reasoning_request_scope(owner, {"enabled": False}):
            with inference_call_scope(owner, body):
                if accepted:
                    assert client.post("https://fixture.invalid/v1/responses", json=body).status_code == 200
                else:
                    with pytest.raises(EgressDispatchDenied):
                        client.post("https://fixture.invalid/v1/responses", json=body)
    assert len(sent) == int(accepted)
