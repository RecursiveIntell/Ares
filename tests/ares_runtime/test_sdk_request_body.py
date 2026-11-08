"""SDK transport options never become JSON body evidence or hide body errors."""
import json

import httpx
import openai
import pytest

from ares_runtime.collaboration import ContractError
from ares_runtime.managed_calls import logical_request_bytes, openai_sdk_request_body


def test_transport_controls_are_projected_without_mutating_sdk_arguments():
    timeout = httpx.Timeout(5)
    payload = {"model": "local", "messages": [{"role": "user", "content": "hello"}],
               "stream": True, "timeout": timeout, "extra_headers": {"x-key": "private"},
               "headers": {"x-other": "private"}, "extra_query": {"api-version": "v1"},
               "extra_body": {"provider_extension": {"enabled": True}}}
    body = openai_sdk_request_body(payload)
    assert body == {"model": "local", "messages": payload["messages"], "stream": True,
                    "provider_extension": {"enabled": True}}
    assert json.loads(logical_request_bytes(body)) == body
    assert payload["timeout"] is timeout
    assert payload["extra_body"] == {"provider_extension": {"enabled": True}}


def test_extra_body_uses_sdk_override_and_omit_semantics():
    payload = {"model": "local", "temperature": 1, "seed": 7,
               "extra_body": {"temperature": 0.25, "seed": openai.omit}}
    assert openai_sdk_request_body(payload) == {"model": "local", "temperature": 0.25}


@pytest.mark.parametrize("extra", [[], "text", 7])
def test_invalid_extra_body_is_not_silently_dropped(extra):
    with pytest.raises(ContractError, match="WIRE_OVERRIDE_UNQUALIFIED"):
        openai_sdk_request_body({"model": "local", "extra_body": extra})


@pytest.mark.parametrize("bad_body", [
    {"provider_extension": object()},
    {"messages": [{"role": "user", "content": object()}]},
    {"extra_body": {"provider_extension": object()}},
])
def test_non_json_body_members_still_fail_closed(bad_body):
    with pytest.raises(TypeError):
        logical_request_bytes(openai_sdk_request_body({"model": "local", **bad_body}))
