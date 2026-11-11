"""Task 2.1 contract: preflight never consumes a permit; final dispatch consumes once.

The boundary's two-phase contract: preflight calls with ``consume_permit=False`` and
must not spend a single-use permit; the final execution call with
``consume_permit=True`` consumes exactly one. The bridge adapter is replaced at the
boundary's own seam, so this tests the owner's control flow, not permit semantics.
"""

import pytest

from ares_runtime import collaboration as c


class CountingAdapter:
    def __init__(self):
        self.consumed = 0

    def validate_and_consume_call(self, *, mission_ref, tool_name, args, target_ref):
        self.consumed += 1
        return {"permit_id": f"p{self.consumed}", "tool": tool_name}

    def validate_and_consume(self, *, mission_ref, tool_name, args_digest, target_ref):
        self.consumed += 1
        return {"permit_id": f"p{self.consumed}", "tool": tool_name}

    def record_receipt(self, receipt):
        pass


@pytest.fixture
def permit_path(monkeypatch):
    adapter = CountingAdapter()
    monkeypatch.setattr(
        c.DaemonPermitReceiptAdapter, "from_ares_config", classmethod(lambda cls, cfg: adapter)
    )
    monkeypatch.setattr(
        c, "_production_permit_canary_config", lambda session_id=None: {"enabled": True, "socket_path": "x"}
    )
    return adapter


def test_preflight_does_not_consume_and_final_consumes_exactly_once(permit_path):
    args = {"action": "list"}
    ok, code, _ = c.dispatcher_boundary(
        "cronjob", args, mission_ref="m1", session_id="s", schema=None,
        authorize_permit=True, consume_permit=False,
    )
    assert ok and code is None
    assert permit_path.consumed == 0, "preflight must not spend a single-use permit"

    ok, code, _ = c.dispatcher_boundary(
        "cronjob", args, mission_ref="m1", session_id="s", schema=None,
        authorize_permit=True, consume_permit=True,
    )
    assert ok and code is None
    assert permit_path.consumed == 1, "final dispatch must consume exactly one permit"
