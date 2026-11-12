"""Task 2.1 route coverage: every probed effectful tool enters the boundary exactly once.

Two model-reachable routes exist:
- the registry route, ``model_tools.handle_function_call``;
- the agent-local route, ``agent.agent_runtime_helpers.invoke_tool``, which dispatches
  ``memory`` and ``delegate_task`` through its own branches.

Each route is driven with the boundary observed. A tool that reaches the boundary
zero times on either route is an unguarded effect (audit H2).
"""

import pytest

import model_tools as mt
import agent.agent_runtime_helpers as helpers
from ares_runtime import collaboration as c

REGISTRY_TOOLS = ["cronjob", "process", "skill_manage", "setup_mcp", "delegate_task", "memory", "write_file"]
AGENT_LOCAL_TOOLS = ["memory", "delegate_task"]


class _Agent:
    _memory_store = None
    _memory_manager = None
    valid_tool_names = set()
    session_id = "s"

    def _dispatch_delegate_task(self, args):
        return "delegated-stub"


@pytest.fixture
def observed(monkeypatch):
    counts = {}

    def boundary(tool_name, *args, **kwargs):
        counts[tool_name] = counts.get(tool_name, 0) + 1
        return (True, None, None)

    # model_tools imports dispatcher_boundary lazily from the owner module, so the
    # owner is the single seam to observe. Patching it intercepts both routes.
    monkeypatch.setattr(c, "dispatcher_boundary", boundary)
    return counts


@pytest.mark.parametrize("tool", REGISTRY_TOOLS)
def test_registry_route_enters_boundary_exactly_once(tool, observed):
    mt.handle_function_call(tool, {"action": "list"}, task_id="t", session_id="s", enabled_tools=REGISTRY_TOOLS)
    assert observed.get(tool, 0) == 1, f"{tool} reached the registry boundary {observed.get(tool, 0)} times"


@pytest.mark.parametrize("tool", AGENT_LOCAL_TOOLS)
def test_agent_local_route_enters_boundary_exactly_once(tool, observed):
    helpers.invoke_tool(_Agent(), tool, {"action": "list", "target": "memory"}, "task-1",
                        tool_call_id="tc1", messages=[])
    assert observed.get(tool, 0) == 1, (
        f"{tool} reached the agent-local boundary {observed.get(tool, 0)} times; "
        "agent-local branches must enter the owner exactly once"
    )
