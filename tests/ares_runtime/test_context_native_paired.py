"""Actual Ares controller -> native daemon -> disposable file -> native receipt.

The qualification job supplies its exact paired-source binary. No installed
daemon, user profile, provider, or external approval credential is used.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from ares_runtime.collaboration import DaemonPermitReceiptAdapter
from ares_runtime.continuity.runtime import context_tool_control_scope
from hermes_state import SessionDB
from tests.ares_runtime.test_context_native_dispatch import publish

pytestmark = pytest.mark.linux_only


@contextmanager
def daemon(binary, root, socket, verifier, worktree, configuration=None):
    args = [binary, "serve", "--root", str(root), "--socket", str(socket),
            "--production-verifier-file", str(verifier), "--production-write-root", str(worktree)]
    if configuration:
        args += ["--context-authority-file", str(configuration)]
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(args, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 10
            while not socket.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            if not socket.exists():
                log.seek(0)
                pytest.fail(log.read().decode(errors="replace"))
            yield
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            socket.unlink(missing_ok=True)


def test_real_native_rollover_scoped_dispatch_restart_and_receipt(monkeypatch):
    binary = os.environ.get("HERMES_TEST_CONTEXT_DAEMON_BIN")
    if not binary:
        pytest.skip("exact paired native daemon supplied by qualification job")
    assert Path(binary).is_file()
    import model_tools
    import tools.approval as approval

    def command(*args):
        return subprocess.check_output([binary, *map(str, args)], text=True, timeout=15).strip()

    def stamp(value):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")

    with tempfile.TemporaryDirectory(prefix="ctx-pair-") as temporary:
        base = Path(temporary)
        root, worktree, socket = base / "native", base / "work", base / "ipc.sock"
        root.mkdir(mode=0o700)
        worktree.mkdir(mode=0o700)
        key = Ed25519PrivateKey.generate()
        verifier = base / "verifier.json"
        verifier.write_text(json.dumps({"schema": "recursive-agent.desktop-production-public-key/v1",
            "key_id": "paired-approval", "public_key": base64.b64encode(key.public_key().public_bytes_raw()).decode()}))
        verifier.chmod(0o600)
        incarnation = command("initialize-context-authority", "--root", root)
        verifier_digest = command("describe-production-verifier", "--production-verifier-file", verifier,
                                  "--production-write-root", worktree)
        connection = {"socket_path": str(socket), "timeout_seconds": 5.0}
        with SessionDB(db_path=base / "state.db") as db:
            db.create_session("s", source="cli", profile_name="p")
            db.append_message("s", "user", "Do the bounded work")
            assert db.try_acquire_session_turn_lease("s", "holder", ttl_seconds=300)
            with daemon(binary, root, socket, verifier, worktree):
                exported = db.export_context_authority_identity("s", turn_lease_holder="holder", transport=connection)
            now = datetime.now(timezone.utc).replace(microsecond=0)
            configuration = base / "authority.json"
            configuration.write_text(json.dumps({"schema": "recursive-agent.context-authority/v1",
                "incarnation": incarnation, "revision": 1, "approval_verifier": verifier_digest,
                "grants": [{**exported, "actor": "desktop:operator", "policy_version": "paired-v1",
                    "policy_digest": "4" * 64, "write_root": str(worktree), "not_before": stamp(now - timedelta(seconds=5)),
                    "expires_at": stamp(now + timedelta(hours=1)), "max_effects": 8, "max_transitions": 8,
                    "previous_grant": None}]}))
            configuration.chmod(0o600)
            with daemon(binary, root, socket, verifier, worktree, configuration):
                db.enroll_native_context_authority("s", turn_lease_holder="holder", transport=connection, incarnation=incarnation)
                transition = publish(db)
                reservation = db.begin_context_rebase_recovery("transition", turn_lease_holder="holder")
                db.activate_native_context_rebase("transition", session_id="child", turn_lease_holder="holder",
                    reservation={"transition_id": "transition", "attempt": reservation["attempts"],
                                 "control_digest": reservation["action_control_digest"]})
                db.mark_context_rebase_ready("transition", expected_continuation_digest=transition.continuation_digest,
                    expected_child_session_id="child", turn_lease_holder="holder", recovery_attempt=reservation["attempts"],
                    expected_control_digest=reservation["action_control_digest"])
            # Actual daemon restart must preserve the child generation and charge.
            with daemon(binary, root, socket, verifier, worktree, configuration):
                db.verify_native_context_current("child")
                snapshot = db.read_context_rebase_snapshot("child")
                db.admit_context_dispatch("child", turn_lease_holder="holder", attempt_id="a",
                    expected_snapshot_digest=snapshot.digest, payload_digest="sha256:" + "7" * 64, route_ref="paired-test")
                db.settle_context_dispatch_response("a", turn_lease_holder="holder")
                registration = db.read_native_context_authority("child")
                agent = SimpleNamespace(session_id="child", _session_db=db, context_rebase_enabled=False,
                    _context_response_admission={"attempt_id": "a"}, _active_session_turn_lease_holder="holder",
                    _context_response_native_authority=registration)
                route = "gateway:original-live-route"
                notified = []
                approval.register_gateway_notify(route, notified.append)
                token = approval.set_current_session_key(route)

                def decision(session_key, notify, request, **kwargs):
                    assert session_key == route
                    notify(request)
                    envelope = request["production_permit"]
                    issued = datetime.now(timezone.utc).replace(microsecond=0)
                    witness = {"approval_id": envelope["approval_id"], "mission_ref": envelope["mission_ref"],
                        "target_ref": envelope["target_ref"], "call": envelope["call"], "actor": "desktop:operator",
                        "effect": {"scope_name": "production-per-call:" + envelope["approval_id"],
                            "read_roots": [], "write_roots": [str(worktree)], "network_allowed": False},
                        "budget": {"max_wall_time_ms": 1000, "max_output_bytes": 4096, "max_artifact_bytes": 8192},
                        "policy_version": "paired-v1", "issued_at": stamp(issued), "not_before": stamp(issued),
                        "expires_at": stamp(issued + timedelta(seconds=60)), "retry": "no_retry", "delegation": "forbidden",
                        "outcome_policy": "terminal_quarantine", "key_id": "paired-approval"}
                    # This ASCII/integer-only test fixture is JCS-compatible;
                    # production Desktop and native owners supply canonicalization.
                    witness["signature"] = list(key.sign(json.dumps(witness, sort_keys=True, separators=(",", ":")).encode()))
                    return {"resolved": True, "choice": "once", "witness": witness}

                def effect(name, args, **kwargs):
                    assert name == "write_file"
                    Path(args["path"]).write_text(args["content"])
                    return '{"ok":true}'
                try:
                    with (context_tool_control_scope(agent), patch("agent.context_input.validate_turn_input_authority"),
                          patch.object(approval, "_await_gateway_decision", side_effect=decision),
                          patch("acp_adapter.edit_approval.maybe_require_edit_approval", return_value=None),
                          patch.object(model_tools.registry, "dispatch", side_effect=effect)):
                        result = model_tools.handle_function_call("write_file", {"path": str(worktree / "out.txt"),
                            "content": "approved exact bytes"}, task_id="paired-mission", session_id="child")
                    assert json.loads(result).get("ok") is True, result
                    assert len(notified) == 1
                    assert (worktree / "out.txt").read_text() == "approved exact bytes"
                    authority = registration["authority"]
                    observed = DaemonPermitReceiptAdapter(connection).context_request("context_authority_readback",
                        incarnation=incarnation, scope=authority["scope"])["snapshot"]
                    assert observed["authority"]["head"] == {"context": "child", "generation": 2, "mode": "active"}
                    assert observed["transitions_charged"] == 2
                    assert observed["effects_charged"] == 1
                    assert len(observed["consumed"]) == 1
                    assert observed["consumed"][0]["reported_state"] == "succeeded"
                    assert observed["consumed"][0]["outcome_receipt_digest"]
                finally:
                    approval.reset_current_session_key(token)
                    approval.unregister_gateway_notify(route)
