"""Real controller exit and bounded recovery through the runtime seam."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent.run_checkpoint_custody import TurnRunCustody
from ares_runtime.continuity.runtime import AutomaticRebaseStatus, reconcile_context_rebase
from hermes_state_runs import RunCustodyError
from scripts.run_checkpoint_claim import ClaimOutcomeUnknown, ClaimRefusal
from tests.test_run_task_custody import task, client_args, sha  # noqa: F401


PUBLISHER = '''
import json,os,sys
from pathlib import Path
from hermes_state import SessionDB
from scripts.run_checkpoint_claim import claim_from_files
db=SessionDB(db_path=Path(sys.argv[1]))
args=json.loads(sys.argv[2])
for args_item in args:
    claim_from_files(db, **args_item, controller_pid=os.getpid())
snapshot=db.read_context_rebase_snapshot('s')
messages=db.get_messages_as_conversation('s',repair_alternation=False)
messages.insert(0,{'role':'assistant','content':'Observed task checkpoint','display_kind':'hidden'})
db.publish_context_rebase_child(transition_id='cold-tx',parent_session_id='s',child_session_id='child',
    continuation_digest='sha256:'+'b'*64,expected_snapshot_digest=snapshot.digest,
    control_revision=snapshot.control_revision,input_watermark=snapshot.input_watermark,
    turn_lease_holder='holder',source='cli',messages=messages,system_prompt='trusted system prompt',
    profile_name='p',custody_transfers=tuple(db.list_run_custody_for_session('s')))
print('published',flush=True)
command=sys.stdin.readline().strip()
if command == 'release':
    for value in db.list_run_custody_for_session('child'):
        db.release_run_custody(value.run_id,owner_token=value.owner_token,expected_generation=value.generation)
db.close()
'''


def publisher(task, *, second=False, v1=False):
    db, _, request, _ = task
    args = client_args(task)
    args.pop("session_id")
    if v1:
        goal = '{"goal":"Repair queue","status":"cleared"}'
        db.set_meta("goal:historical", goal)
        v1_request = json.loads(request.read_text())
        goal_key_file = request.with_name("goal-key")
        goal_key_file.write_text("goal:historical")
        v1_request["checkpoint"]["members"].append(["historical-goal-key", "goal:historical"])
        v1_request["files"]["members"]["historical-goal-key"] = str(goal_key_file)
        request.write_text(json.dumps(v1_request))
        args.pop("task_binding")
        args.pop("expected_control_digest")
        args.update(historical_goal_digest=sha(goal.encode()), expected_request_digest=sha(request.read_bytes()))
    claims = [args]
    if second:
        second_request = json.loads(request.read_text())
        members = dict(second_request["checkpoint"]["members"])
        scope = json.loads(members["continuation-scope-v1"])
        scope["task_ref"] = "run:task2"
        members["continuation-scope-v1"] = json.dumps(scope)
        second_request["checkpoint"]["members"] = list(map(list, members.items()))
        member_file = request.with_name("scope2")
        member_file.write_text(members["continuation-scope-v1"])
        second_request["files"]["members"]["continuation-scope-v1"] = str(member_file)
        path = request.with_name("request2.json")
        path.write_text(json.dumps(second_request))
        claims.append({**args, "run_id": "task2", "task_binding": {**args["task_binding"], "run_id": "task2"},
                       "request_path": str(path), "expected_request_digest": sha(path.read_bytes())})
    proc = subprocess.Popen([sys.executable, "-u", "-c", PUBLISHER, str(db.db_path), json.dumps(claims)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            line = pool.submit(proc.stdout.readline).result(timeout=15).strip()
            if line != "published":
                _, err = proc.communicate(timeout=15)
                pytest.fail(f"Publisher failed: {err}")
        except BaseException:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=15)
            raise
    return proc


def exited(proc):
    _, err = proc.communicate("exit\n", timeout=15)
    assert proc.returncode == 0, err


def fresh_agent(task, *, holder="holder"):
    db = task[0]
    custody = TurnRunCustody(db)
    custody.begin_turn(holder)
    return SimpleNamespace(_session_db=db, session_id="s",
        _active_session_turn_lease_holder=holder, _run_checkpoint_custody=custody,
        _transition_context_engine_session=lambda **_: None,
        context_compressor=SimpleNamespace())


def test_real_cold_runtime_recovers_dead_controller_without_inherited_handles(task):
    proc = publisher(task)
    exited(proc)
    db = task[0]
    old = db.read_run_custody("task")
    assert old.generation == 2
    agent = fresh_agent(task)
    result = reconcile_context_rebase(agent)
    assert result.ready
    assert agent.session_id == "child"
    new = db.read_run_custody("task")
    assert new.generation == 3
    assert new.owner_token != old.owner_token
    assert new.checkpoint == old.checkpoint
    assert new.task_binding == old.task_binding
    assert new.current_session_id == "child"
    assert db.read_run_recovery_files(new)["source"] == json.loads(task[2].read_text())["files"]["source"]
    assert db.read_context_rebase_transition("cold-tx").state == "ready"
    assert "effect:unknown" in new.checkpoint.unresolved_effects


def test_separate_recovery_process_reopens_native_store_and_handles(task):
    proc = publisher(task)
    exited(proc)
    db = task[0]
    prior = db.read_run_custody("task")
    recovery_code = '''
import json,sys
from pathlib import Path
from types import SimpleNamespace
from hermes_state import SessionDB
from agent.run_checkpoint_custody import TurnRunCustody
from ares_runtime.continuity.runtime import reconcile_context_rebase
db=SessionDB(db_path=Path(sys.argv[1]))
owner=TurnRunCustody(db)
owner.begin_turn('holder')
agent=SimpleNamespace(_session_db=db,session_id='s',_active_session_turn_lease_holder='holder',
    _run_checkpoint_custody=owner,_transition_context_engine_session=lambda **_: None,
    context_compressor=SimpleNamespace())
result=reconcile_context_rebase(agent)
print(json.dumps({'ready':result.ready,'reason':result.reason}))
db.close()
'''
    recovered = subprocess.run([sys.executable, "-c", recovery_code, str(db.db_path)],
        capture_output=True, text=True, timeout=20, check=True)
    assert json.loads(recovered.stdout)["ready"]
    current = db.read_run_custody("task")
    assert current.generation == prior.generation + 1
    assert current.controller_pid != prior.controller_pid
    assert current.checkpoint == prior.checkpoint
    assert current.task_binding == prior.task_binding


def test_immutable_v1_history_recovers_without_reinterpreting_it_as_v2(task):
    proc = publisher(task, v1=True)
    exited(proc)
    db = task[0]
    prior = db.read_run_custody("task")
    goal_raw = db.get_meta("goal:historical")
    assert reconcile_context_rebase(fresh_agent(task)).ready
    current = db.read_run_custody("task")
    assert current.schema == "SessionDBRunCustodyV1"
    assert current.historical_goal_digest == prior.historical_goal_digest
    assert current.checkpoint == prior.checkpoint
    assert db.get_meta("goal:historical") == goal_raw


def test_live_predecessor_refuses_even_if_its_custody_expired(task, monkeypatch):
    import hermes_state_runs
    proc = publisher(task)
    try:
        db = task[0]
        old = db.read_run_custody("task")
        monkeypatch.setattr(hermes_state_runs.time, "monotonic_ns", lambda: old.expires_monotonic_ns + 1)
        assert not reconcile_context_rebase(fresh_agent(task)).ready
        assert db.read_run_custody("task") == old
        assert db.read_context_rebase_transition("cold-tx").state != "ready"
    finally:
        exited(proc)


@pytest.mark.parametrize("fault", ["source", "member", "symlink", "locator-missing", "locator-stale"])
def test_changed_or_missing_recovery_evidence_never_takes_over(task, fault):
    proc = publisher(task)
    exited(proc)
    db, _, request, source = task
    old = db.read_run_custody("task")
    if fault == "source":
        source.write_text("changed")
    elif fault == "member":
        Path(json.loads(request.read_text())["files"]["members"]["original-wip"]).write_text("changed")
    elif fault == "symlink":
        target = source.with_name("other-source")
        source.rename(target)
        source.symlink_to(target)
    elif fault == "locator-missing":
        db._execute_write(lambda conn: conn.execute("DELETE FROM state_meta WHERE key='run-custody:task:recovery-files'"))
    else:
        key = "run-custody:task:generation:1:files"
        document = json.loads(db.get_meta(key))
        document["checkpoint_digest"] = "0" * 64
        raw = json.dumps(document)
        db.set_meta(key, raw)
        db.set_meta("run-custody:task:recovery-files", json.dumps({"generation": 1, "digest": sha(raw.encode())}))
    result = reconcile_context_rebase(fresh_agent(task))
    assert not result.ready
    assert db.read_run_custody("task") == old
    assert db.read_context_rebase_transition("cold-tx").state != "ready"


@pytest.mark.parametrize("where", ["before-capture", "readback"])
def test_lost_cold_claim_ack_is_not_automatically_reclaimed_or_released(task, monkeypatch, where):
    proc = publisher(task)
    exited(proc)
    db = task[0]
    if where == "before-capture":
        native = db.claim_run_task_custody_checked
        def lost(*args, **kwargs):
            native(*args, **kwargs)
            raise OSError("lost ack")
        monkeypatch.setattr(db, "claim_run_task_custody_checked", lost)
    else:
        native = db.read_run_custody
        def lost(run_id):
            value = native(run_id)
            if value.generation == 3:
                raise OSError("lost readback")
            return value
        monkeypatch.setattr(db, "read_run_custody", lost)
    agent = fresh_agent(task)
    assert not reconcile_context_rebase(agent).ready
    monkeypatch.undo()
    after = db.read_run_custody("task")
    assert after.generation == 3
    assert not reconcile_context_rebase(agent).ready
    assert db.read_run_custody("task") == after
    assert agent._run_checkpoint_custody.finish_turn("holder")[0]["status"] == "unknown"
    assert db.read_run_custody("task") == after


def test_partial_recovery_retains_first_run_and_completes_remaining_inventory(task):
    proc = publisher(task, second=True)
    exited(proc)
    db, _, request, _ = task
    second_scope = request.with_name("scope2")
    raw = second_scope.read_bytes()
    second_scope.write_bytes(b"changed")
    agent = fresh_agent(task)
    assert not reconcile_context_rebase(agent).ready
    first = db.read_run_custody("task")
    assert first.generation == 3
    assert db.read_run_custody("task2").generation == 2
    cleanup = agent._run_checkpoint_custody.finish_turn("holder")
    assert cleanup == [{"run_id": "task", "status": "recovery_pending", "code": "RUN_CUSTODY_RECOVERY_PENDING"}]
    assert db.read_run_custody("task") == first
    db.release_session_turn_lease("child", "holder")
    assert db.try_acquire_session_turn_lease("child", "next-holder")
    agent._run_checkpoint_custody.begin_turn("next-holder")
    agent._active_session_turn_lease_holder = "next-holder"
    second_scope.write_bytes(raw)
    assert reconcile_context_rebase(agent).ready
    assert db.read_run_custody("task") == first
    assert db.read_run_custody("task2").generation == 3


def test_failure_after_takeover_retains_custody_through_cleanup(task):
    proc = publisher(task)
    exited(proc)
    db = task[0]
    agent = fresh_agent(task)
    def failed(**_):
        raise RuntimeError("engine not yet available")
    agent._transition_context_engine_session = failed
    assert not reconcile_context_rebase(agent).ready
    retained = db.read_run_custody("task")
    assert retained.generation == 3
    assert agent._run_checkpoint_custody.finish_turn("holder")[0]["status"] == "recovery_pending"
    assert db.read_run_custody("task") == retained
    db.release_session_turn_lease("child", "holder")
    assert db.try_acquire_session_turn_lease("child", "next-holder")
    agent._active_session_turn_lease_holder = "next-holder"
    agent._run_checkpoint_custody.begin_turn("next-holder")
    agent._transition_context_engine_session = lambda **_: None
    assert reconcile_context_rebase(agent).ready
    assert db.read_run_custody("task") == retained
    # Successful activation restores ordinary completion cleanup.
    assert agent._run_checkpoint_custody.finish_turn("next-holder") == []
    assert db.read_run_custody("task").disposition == "released"


@pytest.mark.parametrize("v1", [False, True])
def test_same_process_recovery_renews_expired_retained_handle_inside_original_budget(task, monkeypatch, v1):
    import time
    proc = publisher(task, v1=v1)
    exited(proc)
    db = task[0]
    agent = fresh_agent(task)
    def unavailable(**_):
        raise RuntimeError("engine unavailable")
    agent._transition_context_engine_session = unavailable
    assert not reconcile_context_rebase(agent).ready
    before = db.read_run_custody("task")
    assert agent._run_checkpoint_custody.finish_turn("holder")[0]["status"] == "recovery_pending"
    db.release_session_turn_lease("child", "holder")
    later_wall = time.time() + 301
    later_monotonic = time.monotonic_ns() + 301_000_000_000
    monkeypatch.setattr(time, "time", lambda: later_wall)
    monkeypatch.setattr(time, "monotonic_ns", lambda: later_monotonic)
    assert db.try_acquire_session_turn_lease("child", "renew-holder")
    agent._active_session_turn_lease_holder = "renew-holder"
    agent._run_checkpoint_custody.begin_turn("renew-holder")
    agent._transition_context_engine_session = lambda **_: None
    with pytest.raises(RunCustodyError, match="OWNER_EXPIRED"):
        db.refresh_run_custody("task", owner_token=before.owner_token, expected_generation=before.generation)
    assert reconcile_context_rebase(agent).ready
    after = db.read_run_custody("task")
    assert after.generation == before.generation + 1
    assert after.owner_token == before.owner_token
    assert after.checkpoint == before.checkpoint
    assert after.origin_session_id == before.origin_session_id
    assert after.controller_pid == before.controller_pid
    assert after.process_identity == before.process_identity
    assert after.expires_monotonic_ns > later_monotonic
    if v1:
        assert after.historical_goal_digest == before.historical_goal_digest
    else:
        assert after.task_binding == before.task_binding
    assert db.read_run_recovery_files(after)


@pytest.mark.parametrize("fault", ["source", "deadline", "lost_ack", "lost_readback"])
def test_retained_expiry_renewal_is_bounded_and_preserves_uncertainty(task, monkeypatch, fault):
    import time
    proc = publisher(task)
    exited(proc)
    db = task[0]
    agent = fresh_agent(task)
    def unavailable(**_):
        raise RuntimeError("engine unavailable")
    agent._transition_context_engine_session = unavailable
    assert not reconcile_context_rebase(agent).ready
    before = db.read_run_custody("task")
    agent._run_checkpoint_custody.finish_turn("holder")
    db.release_session_turn_lease("child", "holder")
    seconds = 901 if fault == "deadline" else 301
    later_wall = time.time() + seconds
    later_mono = time.monotonic_ns() + seconds * 1_000_000_000
    monkeypatch.setattr(time, "time", lambda: later_wall)
    monkeypatch.setattr(time, "monotonic_ns", lambda: later_mono)
    assert db.try_acquire_session_turn_lease("child", "renew-holder")
    agent._active_session_turn_lease_holder = "renew-holder"
    agent._run_checkpoint_custody.begin_turn("renew-holder")
    agent._transition_context_engine_session = lambda **_: None
    if fault == "source":
        task[3].write_text("changed")
    elif fault == "lost_ack":
        native = db.renew_run_custody_for_context_recovery
        def lost(*args, **kwargs):
            native(*args, **kwargs)
            raise OSError("lost renewal ACK")
        monkeypatch.setattr(db, "renew_run_custody_for_context_recovery", lost)
    elif fault == "lost_readback":
        native = db.read_run_custody
        def lost(run_id):
            value = native(run_id)
            if value.generation == before.generation + 1:
                raise OSError("lost readback")
            return value
        monkeypatch.setattr(db, "read_run_custody", lost)
    assert not reconcile_context_rebase(agent).ready
    monkeypatch.undo()
    after = db.read_run_custody("task")
    if fault.startswith("lost"):
        assert after.generation == before.generation + 1
        assert not reconcile_context_rebase(agent).ready
        assert db.read_run_custody("task") == after
        assert agent._run_checkpoint_custody.finish_turn("renew-holder")[0]["status"] == "unknown"
    else:
        assert after == before


@pytest.mark.parametrize("fault", ["token", "generation", "lease", "attempt", "control", "admission_deadline"])
def test_expired_retained_renewal_rechecks_native_fences(task, monkeypatch, fault):
    import time
    proc = publisher(task)
    exited(proc)
    db = task[0]
    agent = fresh_agent(task)
    def unavailable(**_):
        raise RuntimeError("engine unavailable")
    agent._transition_context_engine_session = unavailable
    assert not reconcile_context_rebase(agent).ready
    before = db.read_run_custody("task")
    agent._run_checkpoint_custody.finish_turn("holder")
    db.release_session_turn_lease("child", "holder")
    wall = [time.time() + 301]
    mono = time.monotonic_ns() + 301_000_000_000
    monkeypatch.setattr(time, "time", lambda: wall[0])
    monkeypatch.setattr(time, "monotonic_ns", lambda: mono)
    assert db.try_acquire_session_turn_lease("child", "renew-holder")
    agent._active_session_turn_lease_holder = "renew-holder"
    agent._run_checkpoint_custody.begin_turn("renew-holder")
    agent._transition_context_engine_session = lambda **_: None
    native = db.renew_run_custody_for_context_recovery
    reached = []
    def raced(*args, **kwargs):
        reached.append(True)
        if fault == "token":
            kwargs["owner_token"] = "0" * 64
        elif fault == "generation":
            kwargs["expected_generation"] += 1
        elif fault == "lease":
            kwargs["lease_holder"] = "foreign-holder"
        elif fault == "attempt":
            kwargs["recovery_reservation"] = {**kwargs["recovery_reservation"], "attempt": 99}
        elif fault == "control":
            db.append_message("child", "user", "Stop for review", turn_lease_holder="renew-holder")
        else:
            write = db._execute_write
            def delayed(operation):
                def admitted(conn):
                    wall[0] += 601
                    return operation(conn)
                return write(admitted)
            with monkeypatch.context() as temporary:
                temporary.setattr(db, "_execute_write", delayed)
                return native(*args, **kwargs)
        return native(*args, **kwargs)
    monkeypatch.setattr(db, "renew_run_custody_for_context_recovery", raced)
    assert not reconcile_context_rebase(agent).ready
    assert reached == [True]
    assert db.read_run_custody("task") == before


def test_process_exit_after_partial_cleanup_retains_both_runs_for_next_controller(task):
    proc = publisher(task, second=True)
    exited(proc)
    db, _, request, _ = task
    second_scope = request.with_name("scope2")
    raw = second_scope.read_bytes()
    second_scope.write_bytes(b"changed")
    code = '''
import json,sys
from pathlib import Path
from types import SimpleNamespace
from hermes_state import SessionDB
from agent.run_checkpoint_custody import TurnRunCustody
from ares_runtime.continuity.runtime import reconcile_context_rebase
db=SessionDB(db_path=Path(sys.argv[1]))
owner=TurnRunCustody(db);owner.begin_turn('holder')
agent=SimpleNamespace(_session_db=db,session_id='s',_active_session_turn_lease_holder='holder',
    _run_checkpoint_custody=owner,_transition_context_engine_session=lambda **_: None,
    context_compressor=SimpleNamespace())
result=reconcile_context_rebase(agent)
print(json.dumps({'ready':result.ready,'cleanup':owner.finish_turn('holder')}))
db.close()
'''
    result = subprocess.run([sys.executable, "-c", code, str(db.db_path)],
        capture_output=True, text=True, timeout=20, check=True)
    observed = json.loads(result.stdout)
    assert not observed["ready"]
    assert observed["cleanup"][0]["status"] == "recovery_pending"
    assert db.read_run_custody("task").disposition == "active"
    assert db.read_run_custody("task").generation == 3
    second_scope.write_bytes(raw)
    assert reconcile_context_rebase(fresh_agent(task)).ready
    assert db.read_run_custody("task").generation == 4
    assert db.read_run_custody("task2").generation == 3


@pytest.mark.parametrize("change", ["control", "inventory", "head"])
def test_ready_rechecks_control_and_exact_custody_inside_native_transaction(task, monkeypatch, change):
    proc = publisher(task)
    exited(proc)
    db = task[0]
    ready = db.mark_context_rebase_ready
    def raced(*args, **kwargs):
        value = db.read_run_custody("task")
        if change == "control":
            db.append_message("child", "user", "Stop for review", turn_lease_holder="holder")
        elif change == "inventory":
            db.release_run_custody("task", owner_token=value.owner_token, expected_generation=value.generation)
        else:
            db.refresh_run_custody("task", owner_token=value.owner_token, expected_generation=value.generation)
        return ready(*args, **kwargs)
    monkeypatch.setattr(db, "mark_context_rebase_ready", raced)
    result = reconcile_context_rebase(fresh_agent(task))
    assert result.status is AutomaticRebaseStatus.RECONCILIATION_REQUIRED
    assert db.read_context_rebase_transition("cold-tx").state != "ready"


def test_correction_during_file_reads_prevents_stale_takeover(task, monkeypatch):
    from scripts import run_checkpoint_claim
    proc = publisher(task)
    exited(proc)
    db = task[0]
    old = db.read_run_custody("task")
    verify = run_checkpoint_claim.verify_checkpoint_files
    def changed(*args, **kwargs):
        result = verify(*args, **kwargs)
        db.append_message("child", "user", "New instructions", turn_lease_holder="holder")
        return result
    monkeypatch.setattr(run_checkpoint_claim, "verify_checkpoint_files", changed)
    assert not reconcile_context_rebase(fresh_agent(task)).ready
    assert db.read_run_custody("task") == old


def test_locator_renewal_fences_checkpoint_advancement_and_preserves_old_evidence(task):
    db, _, request, _ = task
    owner = TurnRunCustody(db)
    owner.begin_turn("holder")
    owner.claim("holder", **client_args(task))
    first = db.read_run_custody("task")
    old_locator = db.get_meta("run-custody:task:generation:1:files")
    updated = db.publish_run_checkpoint("task", owner_token=first.owner_token, expected_generation=1,
        expected_source_digest=first.checkpoint.source_digest,
        checkpoint=replace(first.checkpoint, next_action="Review evidence"))
    with pytest.raises(RunCustodyError, match="RECOVERY_FILES_STALE"):
        db.read_run_recovery_files(updated)
    with pytest.raises(ClaimRefusal, match="FENCE_MISMATCH"):
        owner.bind_recovery_files("holder", run_id="task", expected_generation=2,
            files=json.loads(request.read_text())["files"])
    # The native operation is available to the actual checkpoint publisher,
    # while the stale turn handle is deliberately unable to adopt its token.
    from scripts.run_checkpoint_resume import BoundFileReader, verify_checkpoint_files
    files = json.loads(request.read_text())["files"]
    control = db.read_context_rebase_snapshot("s").action_control_digest.removeprefix("sha256:")
    verify_checkpoint_files(updated.checkpoint, files, BoundFileReader())
    bound = db.bind_run_recovery_files_checked("task", owner_token=updated.owner_token,
        expected_generation=2, lease_holder="holder", expected_control_digest=control, files=files)
    assert db.read_run_recovery_files(bound) == files
    assert db.get_meta("run-custody:task:generation:1:files") == old_locator


def test_released_run_remains_released_during_recovery(task):
    proc = publisher(task)
    _, err = proc.communicate("release\n", timeout=15)
    assert proc.returncode == 0, err
    db = task[0]
    before = db.read_run_custody("task")
    assert before.disposition == "released"
    assert reconcile_context_rebase(fresh_agent(task)).ready
    assert db.read_run_custody("task") == before


def test_unknown_process_inspection_never_becomes_takeover_authority(task, monkeypatch):
    from scripts import run_checkpoint_claim
    proc = publisher(task)
    exited(proc)
    db = task[0]
    before = db.read_run_custody("task")
    def unknown(_pid):
        raise RunCustodyError("PROCESS_UNKNOWN")
    monkeypatch.setattr(run_checkpoint_claim, "_process_identity", unknown)
    assert not reconcile_context_rebase(fresh_agent(task)).ready
    assert db.read_run_custody("task") == before


def test_predecessor_death_is_rechecked_inside_write_admission(task, monkeypatch):
    import hermes_state_runs
    proc = publisher(task)
    exited(proc)
    db = task[0]
    before = db.read_run_custody("task")
    inspect = hermes_state_runs._process_identity
    calls = []
    def changed(pid):
        if pid == before.controller_pid:
            calls.append(pid)
            return None if len(calls) == 1 else before.process_identity
        return inspect(pid)
    monkeypatch.setattr(hermes_state_runs, "_process_identity", changed)
    assert not reconcile_context_rebase(fresh_agent(task)).ready
    assert len(calls) >= 2
    assert db.read_run_custody("task") == before


def test_recovery_deadline_expiring_during_reads_prevents_native_takeover(task, monkeypatch):
    from scripts import run_checkpoint_claim
    proc = publisher(task)
    exited(proc)
    db = task[0]
    before = db.read_run_custody("task")
    verify = run_checkpoint_claim.verify_checkpoint_files
    def expired(*args, **kwargs):
        result = verify(*args, **kwargs)
        key = "context-rebase-recovery:cold-tx"
        record = json.loads(db.get_meta(key))
        record["deadline_at"] = 0
        db.set_meta(key, json.dumps(record))
        return result
    monkeypatch.setattr(run_checkpoint_claim, "verify_checkpoint_files", expired)
    agent = fresh_agent(task)
    assert not reconcile_context_rebase(agent).ready
    assert db.read_run_custody("task") == before
    assert agent._run_checkpoint_custody.finish_turn("holder") == []


def test_locator_write_rolls_back_if_custody_generation_fails(task):
    db = task[0]
    db._execute_write(lambda conn: conn.execute("CREATE TRIGGER reject_custody BEFORE INSERT ON state_meta "
        "WHEN new.key='run-custody:task:generation:1' BEGIN SELECT RAISE(ABORT,'fixture'); END"))
    owner = TurnRunCustody(db)
    owner.begin_turn("holder")
    with pytest.raises(ClaimOutcomeUnknown):
        owner.claim("holder", **client_args(task))
    assert db.list_meta_prefix("run-custody:") == []
