"""Live disposable owner → actual Ares port/materialization/egress qualification.

CI builds the peer from its exact pinned Libraries source. The binary is never
shipped, and this test does not qualify authenticated transport or bootstrap.
"""
from __future__ import annotations

import copy
import json
import select
import subprocess
from pathlib import Path

import pytest

from ares_runtime.collaboration import ContractError
from ares_runtime.governed_context import (
    MANAGED_MODEL_CALL_KINDS,
    MemoryRequirement,
    SemanticMemoryWitnessedPort,
)
from tests.ares_runtime.test_governed_context_materialization import authorize, materialize


BINARY = Path(__file__).parent / "semantic_memory_revocation_owner"
pytestmark = pytest.mark.skipif(
    not BINARY.is_file(), reason="exact pinned owner peer is built by current-owner CI"
)


class OwnerPeer:
    def __init__(self, process):
        self.process = process
        self.initial = self.read()
        self.last_response = None

    def read(self):
        ready, _, _ = select.select([self.process.stdout], [], [], 20)
        assert ready, "canonical owner timed out"
        line = self.process.stdout.readline()
        assert line, "canonical owner exited before response"
        return json.loads(line)

    def command(self, command):
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()
        return self.read()

    def prepare(self, intent):
        request = copy.deepcopy(self.initial["access_request"])
        for key, value in intent.items():
            assert request[key] == value
        return request

    def call(self, name, request):
        assert name == "sm_search_governed_witnessed_v2"
        response = self.command("search")
        assert json.loads(response["payload_json"])["request"] == request
        self.last_response = copy.deepcopy(response)
        return response

    def port(self):
        return SemanticMemoryWitnessedPort(
            prepare_access_request=self.prepare,
            call_owner_tool=self.call,
            resolve_current_state=lambda: self.command("state"),
        )


@pytest.fixture
def owner_peer(tmp_path):
    with (tmp_path / "owner.stderr").open("w+") as errors:
        process = subprocess.Popen(
            [str(BINARY)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=errors, text=True, bufsize=1,
        )
        try:
            yield OwnerPeer(process)
        finally:
            process.stdin.close()
            try:
                code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                pytest.fail("canonical owner did not stop on EOF")
            errors.seek(0)
            assert code == 0, errors.read()
            process.stdout.close()


@pytest.mark.parametrize("call_kind", MANAGED_MODEL_CALL_KINDS)
@pytest.mark.parametrize("requirement", [MemoryRequirement.REQUIRED, MemoryRequirement.OPTIONAL])
def test_revoked_owner_epoch_blocks_persisted_ares_egress(owner_peer, call_kind, requirement):
    result, memory, owner, basis = materialize(
        memory_owner=owner_peer, call_kind=call_kind, memory_requirement=requirement,
    )
    receipt = result.materialization.to_dict()
    before = owner_peer.command("state")
    assert receipt["memory"]["state"] == "applied"
    assert receipt["memory"]["retrieval_epoch"] == before["retrieval_epoch"]
    assert receipt["memory"]["authority_snapshot_ref"] == before["snapshot_id"]
    assert "revocable sentinel" in result.serialized_request.decode()
    assert authorize(result, memory, owner, basis) == result.serialized_request
    allowed = copy.deepcopy(owner_peer.last_response)

    after = owner_peer.command("revoke")
    # Test the actual consumer before asserting owner metadata, so the original
    # owner bug fails as an admitted stale egress, not only an epoch assertion.
    with pytest.raises(ContractError, match="MEMORY_AUTHORITY_CHANGED"):
        authorize(result, memory, owner, basis)
    assert after["retrieval_epoch"] == before["retrieval_epoch"] + 1
    assert after["snapshot_id"] != before["snapshot_id"]
    assert owner_peer.command("revoke") == after  # exact owner idempotent replay
    with pytest.raises(ContractError, match="MEMORY_AUTHORITY_CHANGED"):
        authorize(result, memory, owner, basis)

    # A previously allowed, intact V2 response must also fail port admission
    # against the freshly queried owner state; OPTIONAL cannot downgrade it.
    stale_port = SemanticMemoryWitnessedPort(
        prepare_access_request=owner_peer.prepare,
        call_owner_tool=lambda *_: copy.deepcopy(allowed),
        resolve_current_state=lambda: owner_peer.command("state"),
    )
    with pytest.raises(ContractError, match="MEMORY_AUTHORITY_CHANGED"):
        stale_port.resolve(
            requirement=requirement, request_id="memory-request:1", query="find evidence",
            top_k=3, requested_namespaces=["public-memory"],
            authorized_namespaces=["public-memory"], caller="principal:ares",
            subject="principal:ares", audiences=["principal:ares"],
        )
    denied = owner_peer.port().resolve(
        requirement=requirement, request_id="memory-request:1", query="find evidence",
        top_k=3, requested_namespaces=["public-memory"],
        authorized_namespaces=["public-memory"], caller="principal:ares",
        subject="principal:ares", audiences=["principal:ares"],
    ).to_dict()
    assert denied["state"] == "forbidden"
    assert denied["results"] == []
