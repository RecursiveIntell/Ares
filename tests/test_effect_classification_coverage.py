"""Task 2.1 RED: effectful tools must be classified as effectful by the owner.

``_EFFECTFUL_TOOLS`` in ``ares_runtime.collaboration`` decides which model-reachable
tools the dispatcher boundary admits. A tool absent from it is treated as
non-effectful. The audit closure plan names these tools as effects that must be
present; this test checks that claim against the live set rather than a snapshot
count.
"""

import pytest

from ares_runtime import collaboration

# Effects named by the Task 2.1 RED. Each one executes a side effect, so each must
# be classified effectful by the single boundary owner.
REQUIRED_EFFECTFUL = {
    "cronjob",
    "process",
    "skill_manage",
    "setup_mcp",
    "delegate_task",
    "memory",
}


@pytest.mark.parametrize("tool", sorted(REQUIRED_EFFECTFUL))
def test_named_effect_is_classified_effectful(tool):
    assert tool in collaboration._EFFECTFUL_TOOLS, (
        f"{tool} executes an effect but is absent from _EFFECTFUL_TOOLS, "
        "so the boundary would treat it as non-effectful"
    )
