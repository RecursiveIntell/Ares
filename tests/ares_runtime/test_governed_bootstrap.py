"""Consume-time policy-basis admission: the bounded Task 2.1 seam.

The fixture is the pinned profile-runtime owner output
(``fixtures/profile_runtime_v2_owner.json``). These tests assert behaviour
contracts: a current owner is admitted once per consume-time readback, and every
other shape is refused with a stable code. The resolver call is asserted so a
future refactor cannot swap the readback for a serialized round-trip.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ares_runtime.collaboration import ContractError, ResolvedPolicyBasisV2
from ares_runtime.governed_bootstrap import resolve_policy_basis

FIXTURE = Path(__file__).parent / "fixtures" / "profile_runtime_v2_owner.json"


def _owner() -> dict:
    return json.loads(FIXTURE.read_text())["v2"]


def _now(owner: dict) -> datetime:
    return datetime.fromisoformat(
        owner["owner_projection"]["not_before"].replace("Z", "+00:00")
    )


def test_current_owner_is_admitted_with_one_readback():
    owner = _owner()
    calls: list[tuple[str, str]] = []

    def resolver(basis_ref: str, digest: str):
        calls.append((basis_ref, digest))
        return copy.deepcopy(owner)

    basis = resolve_policy_basis(
        owner,
        expected_task_ref="task:1",
        resolve_owner=resolver,
        now=_now(owner),
    )

    assert basis.owner_verified is True
    assert len(calls) == 1
    assert calls[0][0] == owner["basis_ref"]
    assert calls[0][1] == "blake3:" + owner["basis_digest"]


def test_serialized_basis_is_evidence_only():
    owner = _owner()
    basis = resolve_policy_basis(
        owner,
        expected_task_ref="task:1",
        resolve_owner=lambda *_: copy.deepcopy(owner),
        now=_now(owner),
    )
    round_tripped = ResolvedPolicyBasisV2.parse(basis.to_dict())
    assert round_tripped.owner_verified is False


def test_wrong_task_is_refused_before_readback():
    owner = _owner()
    calls: list[tuple[str, str]] = []

    def resolver(*args):
        calls.append(args)
        return copy.deepcopy(owner)

    with pytest.raises(ContractError) as caught:
        resolve_policy_basis(
            owner,
            expected_task_ref="task:2",
            resolve_owner=resolver,
            now=_now(owner),
        )
    assert caught.value.code == "POLICY_TASK_MISMATCH"
    assert calls == []


def test_missing_owner_is_refused():
    owner = _owner()
    with pytest.raises(ContractError) as caught:
        resolve_policy_basis(
            owner,
            expected_task_ref="task:1",
            resolve_owner=lambda *_: None,
            now=_now(owner),
        )
    assert caught.value.code == "OWNER_POLICY_UNAVAILABLE"


def test_changed_owner_is_refused():
    owner = _owner()
    other = json.loads(FIXTURE.read_text())["other_task_v2"]
    with pytest.raises(ContractError) as caught:
        resolve_policy_basis(
            owner,
            expected_task_ref="task:1",
            resolve_owner=lambda *_: copy.deepcopy(other),
            now=_now(owner),
        )
    assert caught.value.code == "OWNER_POLICY_MISMATCH"


def test_expired_window_is_refused_at_consume_time():
    owner = _owner()
    stale = copy.deepcopy(owner)
    stale["owner_projection"]["not_before"] = "2020-01-01T00:00:00Z"
    stale["owner_projection"]["not_after"] = "2020-01-02T00:00:00Z"
    with pytest.raises(ContractError) as caught:
        resolve_policy_basis(
            stale,
            expected_task_ref="task:1",
            resolve_owner=lambda *_: copy.deepcopy(stale),
            now=datetime(2026, 10, 21, tzinfo=timezone.utc),
        )
    assert caught.value.code == "POLICY_EXPIRED"


def test_not_yet_valid_window_is_refused():
    owner = _owner()
    future = copy.deepcopy(owner)
    future["owner_projection"]["not_before"] = "2099-01-01T00:00:00Z"
    future["owner_projection"]["not_after"] = "2099-01-02T00:00:00Z"
    with pytest.raises(ContractError) as caught:
        resolve_policy_basis(
            future,
            expected_task_ref="task:1",
            resolve_owner=lambda *_: copy.deepcopy(future),
            now=datetime(2026, 10, 21, tzinfo=timezone.utc),
        )
    assert caught.value.code == "POLICY_NOT_YET_VALID"
