"""The physical response witness keeps the dispatched endpoint through restore."""
from unittest.mock import patch

import pytest

from tests.run_agent.test_verification_continuation_budget import agent, _response  # noqa: F401


@pytest.mark.parametrize("case", ["physical", "restored_endpoint", "restored_mode", "synthetic", "substituted"])
def test_physical_transport_witness_precedes_call_and_survives_restoration(agent, case):
    agent.max_iterations = 3
    agent._disable_streaming = True
    agent._route_turn_token = object()
    endpoint, mode = agent.base_url, agent.api_mode
    other_endpoint, other_mode = "http://other.invalid/v1", "codex_responses"
    agent._interruptible_api_call = lambda kwargs: _response()

    def execution(kwargs, callback, **_):
        if case == "synthetic":
            return _response()
        if case == "restored_endpoint":
            agent.base_url = other_endpoint
        if case == "restored_mode":
            agent.api_mode = other_mode
        try:
            # Keep real transport preflight active with its required payload.
            wire_kwargs = ({"model": kwargs["model"], "input": [{"role": "user", "content": [{"type": "input_text", "text": "offline input"}]}],
                            "instructions": "inert test instructions"}
                           if case == "restored_mode" else kwargs)
            result = callback(wire_kwargs)
        finally:
            agent.base_url, agent.api_mode = endpoint, mode
        return _response() if case == "substituted" else result

    with patch("hermes_cli.middleware.run_llm_execution_middleware", side_effect=execution):
        result = agent.run_conversation("offline input")
    assert result["completed"] is True
    route = result.get("accepted_response_route")
    if case in ("synthetic", "substituted"):
        assert route is None
    else:
        assert route.base_url == (other_endpoint if case == "restored_endpoint" else endpoint)
        assert route.api_mode == (other_mode if case == "restored_mode" else mode)
        assert route.turn_token is agent._route_turn_token
        assert (route.model, route.served_model) == ("test/model", "test/model")
    assert (agent.base_url, agent.api_mode) == (endpoint, mode)
