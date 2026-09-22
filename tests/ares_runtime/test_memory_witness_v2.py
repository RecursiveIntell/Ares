"""Consumer tests using captured actual Rust owner bytes, not live owner proof.

The fixture was emitted by semantic-memory 8602767985b30859322fcaf062273d45e2b8e1f9
with explicit MockEmbedder(768), a disposable database and a canonical append.
Private integration receipts separately bind the executable and fresh emissions.
"""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from ares_runtime.collaboration import ContractError
from ares_runtime.governed_context import MemoryRequirement, SemanticMemoryWitnessedPort

CASES = json.loads(
    (Path(__file__).parent / "fixtures/memory_witness_v2_owner.json").read_text(encoding="utf-8")
)["cases"]


def arguments(case, requirement):
    return dict(
        requirement=requirement,
        request_id=case["request_id"],
        query=case["query"],
        top_k=case["top_k"],
        requested_namespaces=[case["namespace"]],
        authorized_namespaces=[case["namespace"]],
        caller=case["caller"],
        subject=case["subject"],
        audiences=case["audiences"],
    )


def make_port(case, raw=None, *, prepare=None, call=None):
    return SemanticMemoryWitnessedPort(
        prepare_access_request=prepare or (lambda intent: copy.deepcopy(case["access_request"])),
        call_owner_tool=call or (lambda name, request: copy.deepcopy(raw or case["response"])),
        resolve_current_state=lambda: copy.deepcopy(case["state"]),
    )


def replace_payload(raw, payload):
    raw["payload_json"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    raw["payload_sha256"] = "sha256:" + hashlib.sha256(raw["payload_json"].encode("utf-8")).hexdigest()
    return raw


@pytest.mark.parametrize("case", CASES, ids=["populated", "empty"])
@pytest.mark.parametrize("requirement", [MemoryRequirement.REQUIRED, MemoryRequirement.OPTIONAL])
def test_actual_owner_v2_payload_is_consumed(case, requirement):
    calls = []

    def call(name, request):
        calls.append((name, copy.deepcopy(request)))
        return copy.deepcopy(case["response"])

    observation = make_port(case, call=call).resolve(**arguments(case, requirement)).to_dict()
    assert observation["state"] == ("applied" if case["namespace"] == "general" else "no_match")
    assert observation["complete_replay_available"] is False
    assert len(calls) == 1
    assert calls[0][0] == "sm_search_governed_witnessed_v2"
    assert calls[0][1] == {
        key: case[key] for key in ("request_id", "query", "top_k", "access_request")
    }


REQUEST_MUTATIONS = [
    (("request_id",), "other"),
    (("query",), "other"),
    (("top_k",), 9),
    (("top_k",), True),
    (("access_request", "caller"), "other"),
    (("access_request", "subject"), "other"),
    (("access_request", "principal"), "other"),
    (("access_request", "audience"), "other"),
    (("access_request", "audiences"), ["other"]),
    (("access_request", "namespace"), "other"),
    (("access_request", "scope", "namespace"), "other"),
    (("access_request", "scope", "domain"), "other"),
    (("access_request", "scope", "workspace_id"), "other"),
    (("access_request", "scope", "repo_id"), "other"),
    (("access_request", "purpose"), "replay"),
    (("access_request", "policy_version"), "other"),
    (("access_request", "policy_digest"), "blake3:" + "0" * 64),
    (("access_request", "delegation_or_elevation"), {}),
]


@pytest.mark.parametrize("case", CASES, ids=["populated", "empty"])
@pytest.mark.parametrize("requirement", [MemoryRequirement.REQUIRED, MemoryRequirement.OPTIONAL])
@pytest.mark.parametrize("path,value", REQUEST_MUTATIONS)
def test_complete_original_request_is_compared_even_with_recomputed_hash(case, requirement, path, value):
    raw = copy.deepcopy(case["response"])
    payload = json.loads(raw["payload_json"])
    target = payload["request"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    replace_payload(raw, payload)
    with pytest.raises(ContractError):
        make_port(case, raw).resolve(**arguments(case, requirement))


@pytest.mark.parametrize("case", CASES, ids=["populated", "empty"])
@pytest.mark.parametrize("requirement", [MemoryRequirement.REQUIRED, MemoryRequirement.OPTIONAL])
def test_altered_exact_payload_bytes_fail_before_parsing(case, requirement, monkeypatch):
    raw = copy.deepcopy(case["response"])
    raw["payload_json"] = raw["payload_json"].replace("sentinel", "TAMPERED")
    called = []
    original = json.loads

    def loads(*args, **kwargs):
        called.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(json, "loads", loads)
    with pytest.raises(ContractError, match="MEMORY_OWNER_PAYLOAD_DIGEST_MISMATCH"):
        make_port(case, raw).resolve(**arguments(case, requirement))
    assert called == []


@pytest.mark.parametrize("kind", ["duplicate", "constant", "overflow", "extra_payload", "extra_outer", "surrogate", "oversize", "v1"])
@pytest.mark.parametrize("requirement", [MemoryRequirement.REQUIRED, MemoryRequirement.OPTIONAL])
def test_strict_v2_envelope_does_not_fall_back(kind, requirement):
    case = CASES[1]
    raw = copy.deepcopy(case["response"])
    payload = json.loads(raw["payload_json"])
    if kind == "duplicate":
        raw["payload_json"] = raw["payload_json"].replace('"top_k":10', '"top_k":10,"top_k":10')
    elif kind == "constant":
        raw["payload_json"] = raw["payload_json"].replace('"top_k":10', '"top_k":NaN')
    elif kind == "overflow":
        raw["payload_json"] = raw["payload_json"].replace('"top_k":10', '"top_k":1e400')
    elif kind == "extra_payload":
        payload["unknown"] = 1
        replace_payload(raw, payload)
    elif kind == "extra_outer":
        raw["unknown"] = 1
    elif kind == "surrogate":
        raw["payload_json"] = "\ud800"
    elif kind == "oversize":
        raw["payload_json"] = " " * 1_048_577
    elif kind == "v1":
        raw = {**payload["response"], "ok": True}
    if kind != "surrogate" and "payload_json" in raw:
        raw["payload_sha256"] = "sha256:" + hashlib.sha256(raw["payload_json"].encode("utf-8")).hexdigest()
    with pytest.raises(ContractError):
        make_port(case, raw).resolve(**arguments(case, requirement))


def test_owner_request_is_frozen_before_untrusted_callback_mutates_arguments():
    case = CASES[1]

    def call(name, request):
        request["access_request"]["scope"]["namespace"] = "other"
        raw = copy.deepcopy(case["response"])
        payload = json.loads(raw["payload_json"])
        payload["request"] = request
        return replace_payload(raw, payload)

    with pytest.raises(ContractError):
        make_port(case, call=call).resolve(**arguments(case, MemoryRequirement.REQUIRED))


def test_invalid_prepared_request_is_not_owner_unavailability():
    case = CASES[1]
    calls = []

    def prepare(intent):
        request = copy.deepcopy(case["access_request"])
        request["caller"] = "other"
        return request

    port = make_port(case, prepare=prepare, call=lambda *args: calls.append(args))
    with pytest.raises(ContractError):
        port.resolve(**arguments(case, MemoryRequirement.OPTIONAL))
    assert calls == []


@pytest.mark.parametrize("path", [
    ("response", "results", 0),
    ("response", "results", 0, "source", "fact"),
    ("response", "decisions", 0),
    ("response", "decisions", 0, "scope"),
])
@pytest.mark.parametrize("requirement", [MemoryRequirement.REQUIRED, MemoryRequirement.OPTIONAL])
def test_unknown_nested_owner_fields_are_not_admitted(path, requirement):
    case = CASES[0]
    raw = copy.deepcopy(case["response"])
    payload = json.loads(raw["payload_json"])
    obj = payload["response"]
    for key in path:
        obj = obj[key]
    obj["unknown_extension"] = True
    replace_payload(raw, payload)
    with pytest.raises(ContractError):
        make_port(case, raw).resolve(**arguments(case, requirement))
