"""Bounded native array-type projection; unsupported constraints fail before HTTP."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from agent.gemini_native_adapter import (
    AsyncGeminiNativeClient,
    GeminiAPIError,
    GeminiNativeClient,
    _translate_tools_to_gemini,
)
from agent.gemini_schema import GeminiSchemaProjectionError, sanitize_gemini_schema


def _parameters(node, location):
    if location == "root":
        return node
    if location == "properties":
        return {"type": "object", "properties": {"private-property": node}}
    if location == "items":
        return {"type": "array", "items": node}
    return {"anyOf": [node]}


def _project(node, location):
    params = _parameters(node, location)
    original = copy.deepcopy(params)
    tools = [{"type": "function", "function": {"name": "probe", "parameters": params}}]
    result = _translate_tools_to_gemini(tools)[0]["functionDeclarations"][0]["parameters"]
    assert params == original
    if location == "properties":
        return result["properties"]["private-property"]
    return result["items"] if location == "items" else result["anyOf"][0]


@pytest.mark.parametrize("types", [
    ["string", "null"], ["integer", "null"], ["number", "boolean"],
    ["array", "object", "string", "null"], ["string", "string", "null"], ["object"],
])
@pytest.mark.parametrize("location", ["properties", "items", "anyOf"])
@pytest.mark.parametrize("nullable_first", [False, True])
def test_array_projection_preserves_alternatives_and_constraints(types, location, nullable_first):
    node = {"nullable": False, "type": types} if nullable_first else {"type": types, "nullable": False}
    node.update(description="Keep this guidance", anyOf=[{"description": "Keep conjunction"}])
    if "array" in types:
        node.update(items={"type": "integer"}, minItems=1, maxItems=4)
    if "object" in types:
        node.update(properties={"name": {"type": "string"}}, required=["name", "undefined"],
                    minProperties=1, maxProperties=2, propertyOrdering=["name"])
    out = _project(node, location)
    branches = out["anyOf"] if "type" not in out else [out]
    assert [branch["type"] for branch in branches] == list(dict.fromkeys(t for t in types if t != "null"))
    assert out["description"] == node["description"]
    assert out["nullable"] is ("null" in types)
    for branch in branches:
        assert branch["anyOf"] == node["anyOf"]
        if branch["type"] == "array":
            assert branch["items"] == {"type": "integer"}
            assert (branch["minItems"], branch["maxItems"]) == (1, 4)
        elif len(branches) > 1:
            assert "items" not in branch
        if branch["type"] == "object":
            assert branch["properties"] == node["properties"]
            assert branch["required"] == ["name"]
            assert (branch["minProperties"], branch["maxProperties"]) == (1, 2)
            assert branch["propertyOrdering"] == ["name"]
        elif len(branches) > 1:
            assert "properties" not in branch and "required" not in branch


@pytest.mark.parametrize("type_name,values,expected", [
    ("string", ["one", "two"], ["one", "two"]),
    ("integer", [1, -2, 1], ["1", "-2"]),
    ("number", [1, 2.5, -3.0], ["1", "2.5", "-3.0"]),
    ("boolean", [True, False, True], ["true", "false"]),
])
@pytest.mark.parametrize("location", ["properties", "items", "anyOf"])
def test_single_type_array_enums_have_exact_supported_scalar_metadata(type_name, values, expected, location):
    out = _project({"type": [type_name, type_name, "null"], "enum": values}, location)
    assert out == {"type": type_name, "nullable": True, "enum": expected}


REFUSED = [
    {"type": []}, {"type": ["null"]}, {"type": ["unrecognized-private-type"]},
    {"type": ["string", 7]}, {"type": ["string", {}]},
    {"type": ["string", "integer"], "enum": ["one", 2]},
    {"type": ["string", "integer"], "enum": [2]},
    {"type": ["object"], "enum": [{"private-value": 1}]},
    {"type": ["array"], "enum": [[1]]}, {"type": ["string"], "enum": [None]},
    {"type": ["integer"], "enum": [True]}, {"type": ["number"], "enum": [True]},
    {"type": ["boolean"], "enum": [1]}, {"type": ["string"], "enum": [1]},
    {"type": ["number"], "enum": [float("inf")]},
    {"type": ["number"], "enum": [float("nan")]},
    {"type": ["string"], "enum": []}, {"type": ["string"], "enum": "private-value"},
]


@pytest.fixture(scope="module")
def native_capture():
    captured = []

    class Capture(BaseHTTPRequestHandler):
        def do_POST(self):
            captured.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(418)
            self.end_headers()
            self.wfile.write(b"Offline request capture only")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Capture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1beta", captured
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def _invoke(client, asynchronous, **kwargs):
    if asynchronous:
        return asyncio.run(AsyncGeminiNativeClient(client).chat.completions.create(**kwargs))
    return client.chat.completions.create(**kwargs)


@pytest.mark.parametrize("node", REFUSED)
@pytest.mark.parametrize("location", ["root", "properties", "items", "anyOf"])
@pytest.mark.parametrize("asynchronous,stream", [(False, False), (False, True), (True, False), (True, True)])
def test_refusal_reaches_real_native_entrypoints_before_any_http(node, location, asynchronous, stream, native_capture):
    base_url, captured = native_capture
    before = len(captured)
    params = _parameters(node, location)
    original = copy.deepcopy(params)
    tools = [
        {"type": "function", "function": {"name": "valid", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "private-tool", "parameters": params}},
    ]
    # The real httpx transport targets only this disposable loopback capture.
    with GeminiNativeClient(api_key="offline-placeholder", base_url=base_url,
                            http_client=httpx.Client(trust_env=False, timeout=2)) as client:
        with pytest.raises(GeminiSchemaProjectionError) as error:
            _invoke(client, asynchronous, model="offline-probe", messages=[{"role": "user", "content": "probe"}],
                    tools=tools, stream=stream)
    assert len(captured) == before
    assert params == original
    message = str(error.value)
    assert "private" not in message and "unrecognized-private-type" not in message
    assert len(message) < 300 and "$" in message
    assert location == "root" or location in message


@pytest.mark.parametrize("asynchronous", [False, True])
def test_real_native_wire_preserves_supported_projection(asynchronous, native_capture):
    base_url, captured = native_capture
    node = {"type": ["integer", "null"], "enum": [1, 2], "nullable": False}
    params = _parameters(node, "properties")
    original = copy.deepcopy(params)
    tools = [{"type": "function", "function": {"name": "probe", "parameters": params}}]
    before = len(captured)
    with GeminiNativeClient(api_key="offline-placeholder", base_url=base_url,
                            http_client=httpx.Client(trust_env=False, timeout=2)) as client:
        with pytest.raises(GeminiAPIError) as error:
            _invoke(client, asynchronous, model="offline-probe", messages=[{"role": "user", "content": "probe"}], tools=tools)
    assert error.value.status_code == 418
    assert len(captured) == before + 1
    declaration = captured[-1]["tools"][0]["functionDeclarations"][0]
    assert declaration["parameters"]["properties"]["private-property"] == {
        "type": "integer", "enum": ["1", "2"], "nullable": True,
    }
    assert params == original


def test_error_path_is_bounded_without_property_names():
    node = {"type": []}
    for _ in range(50):
        node = {"properties": {"private-property": node}}
    with pytest.raises(GeminiSchemaProjectionError) as error:
        sanitize_gemini_schema(node)
    assert "private-property" not in str(error.value)
    assert len(str(error.value)) < 300


def test_optional_google_sdk_accepts_supported_declarations():
    types = pytest.importorskip("google.genai.types", reason="Optional Google SDK not installed; no remote provider claim")
    for node in [
        {"type": ["integer", "null"], "enum": [1, 2]},
        {"type": ["array", "object"], "items": {"type": "string"},
         "properties": {"name": {"type": "string"}}, "required": ["name"]},
    ]:
        tools = [{"type": "function", "function": {"name": "probe", "parameters": _parameters(node, "properties")}}]
        declaration = _translate_tools_to_gemini(tools)[0]["functionDeclarations"][0]
        types.FunctionDeclaration.model_validate(json.loads(json.dumps(declaration)))
