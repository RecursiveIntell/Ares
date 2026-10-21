"""A stored empty tool name must not log the same warning on every API call.

`sanitize_api_messages` runs on the per-call API copy, so a persisted assistant
message with an empty `function.name` is re-repaired on every subsequent
request. The repair is intentional (and stored history is deliberately not
rewritten here), but logging each repeat flooded `errors.log`: one call id
produced 121 identical warnings over 50 minutes.

The outgoing copy must still be repaired every time — only the warning is
deduplicated, per call id, per process.
"""

import types

import run_agent

from agent import agent_runtime_helpers as helpers


class _FakeLogger:
    def __init__(self):
        self.warnings: list[str] = []

    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, message, *args):
        self.warnings.append(message % args if args else message)


def _messages(call_id: str):
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "", "arguments": "{}"},
                }
            ],
        }
    ]


def _install(monkeypatch):
    logger = _FakeLogger()
    ra = types.SimpleNamespace(logger=logger, AIAgent=run_agent.AIAgent)
    monkeypatch.setattr(helpers, "_ra", lambda: ra)
    helpers._EMPTY_NAME_REPAIR_WARNED.clear()

    return logger


def test_same_call_id_warns_once_but_is_repaired_every_time(monkeypatch):
    logger = _install(monkeypatch)

    first = helpers.sanitize_api_messages(_messages("call_dedupe_1"))
    second = helpers.sanitize_api_messages(_messages("call_dedupe_1"))

    assert len(logger.warnings) == 1
    assert "call_dedupe_1" in logger.warnings[0]
    # The outgoing copy is repaired on both passes.
    assert first[0]["tool_calls"][0]["function"]["name"] == "invalid_tool_call"
    assert second[0]["tool_calls"][0]["function"]["name"] == "invalid_tool_call"


def test_distinct_call_ids_each_warn(monkeypatch):
    logger = _install(monkeypatch)

    helpers.sanitize_api_messages(_messages("call_dedupe_a"))
    helpers.sanitize_api_messages(_messages("call_dedupe_b"))

    assert len(logger.warnings) == 2
