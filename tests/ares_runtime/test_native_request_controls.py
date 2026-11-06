"""Final-body controls are observations, never config attestations or authority."""
import hashlib
import json

import httpx
import pytest

from ares_runtime.dispatch_route import request_metadata
from ares_runtime.physical_dispatch import bind_http_client
from hermes_cli import middleware


def encode(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


@pytest.mark.parametrize("endpoint,controls", [
    ("responses", {"reasoning": {"effort": "high", "summary": "auto"}}),
    ("chat/completions", {"reasoning_effort": "xhigh"}),
    ("messages", {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}),
])
def test_final_body_controls_are_visible_without_content(endpoint, controls):
    body = {"model": "fixture-model", "messages": [{"role": "user", "content": "private-canary"}],
            "max_tokens": 20, "tools": [{"type": "function", "function": {"name": "private-tool"}}], **controls}
    original = encode(body)
    metadata = request_metadata(original, route=f"https://fixture.invalid/v1/{endpoint}")
    observed = metadata["native_controls"]
    assert observed["schema"] == "ares.native-request-controls/v1"
    for key in ("reasoning", "reasoning_effort", "thinking", "output_config"):
        if key in controls:
            if isinstance(controls[key], dict):
                assert observed[key] == {k: v for k, v in controls[key].items()
                                         if k in {"effort", "type", "budget_tokens"}}
            else:
                assert observed[key] == controls[key]
    assert observed["tools_count"] == 1
    assert observed["context_sha256"].startswith("sha256:")
    assert metadata["controls_qualification_error"] is None
    assert "private-canary" not in json.dumps(metadata)
    assert "private-tool" not in json.dumps(metadata)
    assert original == encode(body)


def test_omitted_reasoning_remains_omitted_not_defaulted():
    meta = request_metadata(encode({"model": "fixture", "messages": []}), route="https://fixture.invalid/v1/responses")
    assert "reasoning" not in meta["native_controls"]
    assert meta["native_controls"]["tools_count"] == 0


@pytest.mark.parametrize("bad", [b'{"model":"x","reasoning":{"effort":"high","effort":"low"}}',
                                  b'[]', b'{"model":"x","tools":null}',
                                  b'{"model":"x","reasoning":"high"}',
                                  b'{"model":"x","output_config":{"effort":true}}'])
def test_ambiguous_controls_are_unqualified_not_silent_empty(bad):
    meta = request_metadata(bad, route="https://fixture.invalid/v1/responses")
    assert meta["native_controls"] is None
    assert meta["controls_qualification_error"] == "REQUEST_CONTROLS_UNAVAILABLE"


def test_context_digest_distinguishes_injected_private_marker_without_copying_it():
    def projection(content):
        return request_metadata(encode({"model": "x", "messages": [{"role": "user", "content": content}]}),
                                route="https://fixture.invalid/v1/responses")["native_controls"]
    plain = projection("query")
    injected = projection("query\nprivate-fixture-memory-canary")
    assert plain["context_sha256"] != injected["context_sha256"]
    assert "private-fixture" not in json.dumps(injected)


def test_send_boundary_rejects_receipt_high_when_wire_effort_is_medium(monkeypatch):
    sends = []
    claims = {"effort": "high"}  # Independent upstream assertion, deliberately wrong.
    observed = []
    def govern(*, payload, dispatch_phase, **context):
        assert dispatch_phase == "physical_send"
        observed.append(payload)
        native = payload["native_controls"]
        if native["reasoning"]["effort"] != claims["effort"]:
            raise middleware.EgressDispatchDenied("REQUEST_BINDING_MISMATCH")
    monkeypatch.setattr(middleware, "_get_middleware_callbacks", lambda _: [govern])
    def transport(request):
        sends.append(request.content)
        return httpx.Response(200, json={"ok": True})
    with bind_http_client(httpx.Client(transport=httpx.MockTransport(transport))) as client:
        with pytest.raises(middleware.EgressDispatchDenied) as caught:
            client.post("https://fixture.invalid/v1/responses", json={"model": "fixture", "reasoning": {"effort": "medium"}, "max_output_tokens": 1})
    assert caught.value.code == "REQUEST_BINDING_MISMATCH"
    assert len(observed) == 1
    assert sends == []


def test_observation_agrees_with_independent_transport_body_and_is_byte_transparent(monkeypatch):
    observations, bodies = [], []
    monkeypatch.setattr(middleware, "_get_middleware_callbacks", lambda _: [lambda **kw: observations.append(kw["payload"])])
    def transport(request):
        bodies.append(request.content)
        return httpx.Response(200, json={"ok": True})
    body = encode({"model": "fixture", "reasoning": {"effort": "high"}, "tools": [], "messages": [], "max_output_tokens": 1})
    with bind_http_client(httpx.Client(transport=httpx.MockTransport(transport))) as client:
        client.post("https://fixture.invalid/v1/responses", content=body)
    assert bodies == [body]
    assert json.loads(bodies[0])["reasoning"] == observations[0]["native_controls"]["reasoning"]
    assert observations[0]["provider_request_sha256"] == "sha256:" + hashlib.sha256(body).hexdigest()
