"""Final qualification counts schema data without accepting opaque transport input."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from ares_runtime.continuity.budget import BudgetError, CountMethod, final_request_upper_bound
from ares_runtime.continuity.runtime import (
    ContextDispatchError, admit_final_context_dispatch, context_dispatch_payload_digest,
    context_dispatch_route_identity,
)
from hermes_state import SessionDB


NAMES = ("conversation", "previous_response_id", "encrypted_content")
MODES = ("chat_completions", "codex_responses", "anthropic_messages", "bedrock_converse")


def schema(name):
    return {"type": "object", "properties": {name: {"type": "string"}}, "required": [name]}


def payload(mode, name="query", definition=None):
    definition = schema(name) if definition is None else definition
    fn = {"name": "search_history", "description": "Plain text lookup", "parameters": definition}
    if mode == "codex_responses":
        return {"model": "test-model", "max_output_tokens": 1000,
                "input": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", **fn}]}
    if mode == "anthropic_messages":
        return {"model": "test-model", "max_tokens": 1000,
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"name": fn["name"], "description": fn["description"],
                           "input_schema": definition}]}
    if mode == "bedrock_converse":
        return {"modelId": "test-model", "inferenceConfig": {"maxTokens": 1000},
                "messages": [{"role": "user", "content": [{"text": "hi"}]}],
                "toolConfig": {"tools": [{"toolSpec": {
                    "name": fn["name"], "description": fn["description"],
                    "inputSchema": {"json": definition}}}]}}
    return {"model": "test-model", "max_tokens": 1000,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": fn}]}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name", NAMES)
def test_valid_schema_parameter_names_count_every_byte(mode, name):
    body = payload(mode, name)
    result = final_request_upper_bound(route_ref="test:route", payload=body)
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode()
    assert result.tokens == len(raw) + 8192 + 64
    assert result.method is CountMethod.QUALIFIED_UPPER_BOUND
    assert result.payload_digest == "sha256:" + hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("mode", MODES)
def test_nested_schema_types_enums_defaults_and_examples_are_data(mode):
    definition = {"type": "object", "properties": {
        "type": {"type": ["string", "null"]},
        "nested": {"type": "object", "properties": {
            "conversation": {"type": "string", "description": "previous_response_id"},
            "previous_response_id": {"enum": [{"encrypted_content": "example"}]},
            "encrypted_content": {"default": {"type": "input_image", "image_url": "example"}},
        }},
    }, "examples": [{"type": "compaction", "encrypted_content": "example"}]}
    original = final_request_upper_bound(route_ref="r", payload=payload(mode, definition=definition))
    changed = deepcopy(definition)
    changed["description"] = "Every added schema byte remains counted."
    newer = final_request_upper_bound(route_ref="r", payload=payload(mode, definition=changed))
    assert newer.tokens > original.tokens
    assert newer.payload_digest != original.payload_digest


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("location", ["root", "extension", "tool_sibling", "schema_lookalike"])
def test_opaque_names_outside_genuine_schema_locations_still_refuse(name, location):
    body = payload("chat_completions")
    if location == "root":
        body[name] = "server-state"
    elif location == "extension":
        body["extra_body"] = {"vendor_extension": {name: "server-state"}}
    elif location == "tool_sibling":
        body["tools"][0]["function"][name] = "server-state"
    else:
        body["extension"] = {"tools": [{"type": "function", "function": {
            "name": "fake", "parameters": schema(name)}}]}
    with pytest.raises(BudgetError, match="FINAL_PAYLOAD_OPAQUE_ACCOUNTING_UNQUALIFIED"):
        final_request_upper_bound(route_ref="r", payload=body)


@pytest.mark.parametrize("mode", MODES)
def test_media_sibling_of_schema_still_refuses(mode):
    body = payload(mode)
    if mode == "bedrock_converse":
        body["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["opaque"] = {
            "type": "input_image"}
    else:
        body["tools"][0]["opaque"] = {"type": "input_image"}
    with pytest.raises(BudgetError, match="FINAL_PAYLOAD_OPAQUE_ACCOUNTING_UNQUALIFIED"):
        final_request_upper_bound(route_ref="r", payload=body)


@pytest.mark.parametrize("kind", [
    "image", "image_url", "input_image", "input_file", "file", "audio",
    "input_audio", "compaction", "computer_screenshot",
])
def test_actual_media_and_native_items_still_refuse(kind):
    body = payload("codex_responses")
    body["input"] = [{"type": kind}]
    with pytest.raises(BudgetError, match="FINAL_PAYLOAD_OPAQUE_ACCOUNTING_UNQUALIFIED"):
        final_request_upper_bound(route_ref="r", payload=body)


@pytest.mark.parametrize("malformed", [object(), float("nan")])
def test_schema_data_does_not_skip_serialization_validation(malformed):
    with pytest.raises(BudgetError, match="INVALID_FINAL_PAYLOAD"):
        final_request_upper_bound(route_ref="r",
                                  payload=payload("chat_completions", definition={"default": malformed}))


def test_schema_data_does_not_skip_depth_validation():
    definition = {}
    for _ in range(65):
        definition = {"properties": {"x": definition}}
    with pytest.raises(BudgetError, match="INVALID_FINAL_PAYLOAD"):
        final_request_upper_bound(route_ref="r", payload=payload("chat_completions", definition=definition))


def test_schema_lookalike_with_wrong_tool_kind_is_not_exempt():
    body = payload("codex_responses", "conversation")
    body["tools"][0]["type"] = "unknown-server-tool"
    with pytest.raises(BudgetError, match="FINAL_PAYLOAD_OPAQUE_ACCOUNTING_UNQUALIFIED"):
        final_request_upper_bound(route_ref="r", payload=body)


def test_schema_alias_at_a_content_location_is_still_opaque():
    shared = {"type": "object", "properties": {"conversation": {"type": "string"}}}
    body = payload("codex_responses", definition=shared)
    body["input"] = [shared]
    with pytest.raises(BudgetError, match="FINAL_PAYLOAD_OPAQUE_ACCOUNTING_UNQUALIFIED"):
        final_request_upper_bound(route_ref="r", payload=body)


@pytest.mark.parametrize("kind", [["input_image"], {"input_image": True}])
def test_malformed_transport_discriminator_refuses_with_typed_error(kind):
    body = payload("codex_responses")
    body["input"] = [{"type": kind}]
    with pytest.raises(BudgetError, match="INVALID_FINAL_PAYLOAD"):
        final_request_upper_bound(route_ref="r", payload=body)


@pytest.mark.parametrize("kind", [7, True])
def test_scalar_extension_type_metadata_retains_prior_counting(kind):
    body = payload("chat_completions")
    body["extra_body"] = {"vendor_metadata": {"type": kind}}
    assert final_request_upper_bound(route_ref="r", payload=body).tokens > 8192


@pytest.fixture
def bound_agent(tmp_path):
    db = SessionDB(db_path=tmp_path / "context.db")
    db.create_session("s", source="cli")
    db.append_message("s", "user", "hi")
    assert db.try_acquire_session_turn_lease("s", "holder", ttl_seconds=300)
    agent = SimpleNamespace(provider="test", model="test-model", api_mode="chat_completions",
        base_url="", client=object(), max_tokens=1000,
        context_compressor=SimpleNamespace(context_length=100_000),
        _session_db=db, session_id="s", _active_session_turn_lease_holder="holder")
    yield agent
    db.close()


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name", NAMES)
def test_real_final_admission_and_materialization_share_schema_count(bound_agent, mode, name):
    bound_agent.api_mode = mode
    body = payload(mode, name)
    snapshot = bound_agent._session_db.read_context_rebase_snapshot("s")
    digest = context_dispatch_payload_digest(body)
    admission = admit_final_context_dispatch(bound_agent, snapshot, body,
        attempt_id="attempt", materialization_digest=digest,
        route_identity=context_dispatch_route_identity(bound_agent))
    assert admission["payload_digest"] == digest


@pytest.mark.parametrize("changed,code", [
    ({"previous_response_id": "opaque"}, "FINAL_PAYLOAD_OPAQUE_ACCOUNTING_UNQUALIFIED"),
    ({"extra_body": {"tools": []}}, "CONTEXT_DISPATCH_WIRE_OVERRIDE_UNQUALIFIED"),
    ({"max_tokens": 0}, "CONTEXT_DISPATCH_OUTPUT_BUDGET_UNQUALIFIED"),
    ({"model": "other"}, "CONTEXT_DISPATCH_ROUTE_CHANGED"),
])
def test_public_admission_retains_negative_controls(bound_agent, changed, code):
    body = payload("chat_completions")
    body.update(changed)
    snapshot = bound_agent._session_db.read_context_rebase_snapshot("s")
    with pytest.raises(ContextDispatchError, match=code):
        admit_final_context_dispatch(bound_agent, snapshot, body, attempt_id="refused")


def test_public_admission_preserves_overflow_digest_and_no_snapshot_modes(bound_agent):
    body = payload("chat_completions")
    snapshot = bound_agent._session_db.read_context_rebase_snapshot("s")
    with pytest.raises(ContextDispatchError, match="CONTEXT_DISPATCH_MATERIALIZATION_CHANGED"):
        admit_final_context_dispatch(bound_agent, snapshot, body, attempt_id="digest",
                                     materialization_digest="sha256:" + "0" * 64)
    bound_agent.context_compressor.context_length = 9000
    with pytest.raises(ContextDispatchError, match="CONTEXT_DISPATCH_FINAL_PAYLOAD_TOO_LARGE"):
        admit_final_context_dispatch(bound_agent, snapshot, body, attempt_id="overflow")
    assert admit_final_context_dispatch(bound_agent, None, {"previous_response_id": "opaque"},
                                       attempt_id="ordinary") is None


def test_bedrock_native_default_reserve_is_admitted_with_unset_host_limit(bound_agent):
    from agent.chat_completion_helpers import build_api_kwargs
    from agent.transports.bedrock import BedrockTransport
    bound_agent.api_mode = "bedrock_converse"
    bound_agent.max_tokens = None
    bound_agent._get_transport = lambda: BedrockTransport()
    body = build_api_kwargs(bound_agent, [{"role": "user", "content": "hi"}], [])
    assert body["inferenceConfig"]["maxTokens"] == 4096
    snapshot = bound_agent._session_db.read_context_rebase_snapshot("s")
    admission = admit_final_context_dispatch(bound_agent, snapshot, body, attempt_id="native-default")
    assert admission["payload_digest"] == context_dispatch_payload_digest(body)


@pytest.mark.parametrize("inference", [None, {}, {"maxTokens": None}, {"maxTokens": True},
    {"maxTokens": "4096"}, {"maxTokens": 0}, {"maxTokens": -1}, []])
def test_bedrock_invalid_native_reserve_cannot_borrow_host_or_top_level_limit(bound_agent, inference):
    bound_agent.api_mode = "bedrock_converse"
    body = payload("bedrock_converse")
    body["inferenceConfig"] = inference
    body["max_tokens"] = 1000
    snapshot = bound_agent._session_db.read_context_rebase_snapshot("s")
    with pytest.raises(ContextDispatchError, match="CONTEXT_DISPATCH_OUTPUT_BUDGET_UNQUALIFIED"):
        admit_final_context_dispatch(bound_agent, snapshot, body, attempt_id="invalid-native")


def test_bedrock_native_reserve_controls_overflow_despite_smaller_alias(bound_agent):
    bound_agent.api_mode = "bedrock_converse"
    bound_agent.context_compressor.context_length = 20_000
    body = payload("bedrock_converse")
    body["inferenceConfig"]["maxTokens"] = 15_000
    body["max_tokens"] = 1000
    snapshot = bound_agent._session_db.read_context_rebase_snapshot("s")
    with pytest.raises(ContextDispatchError, match="CONTEXT_DISPATCH_FINAL_PAYLOAD_TOO_LARGE"):
        admit_final_context_dispatch(bound_agent, snapshot, body, attempt_id="native-overflow")
