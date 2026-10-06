"""Tests for cronjob action='run' background dispatch.

A manual `cronjob(action='run')` used to execute the job synchronously on the
calling agent's tool thread — a full agent run (minutes to hours) inside ONE
tool call, uninterruptible and serial. It now dispatches through the async
delegation registry (same rail as delegate_task background mode): the tool
returns immediately with a handle and the run's outcome re-enters the
conversation as a type='async_delegation' completion event.

Sync fallbacks preserved:
  - no routable session (direct Python callers, `hermes cron run`)
  - async delivery unsupported (one-shot runners, cron child sessions)
  - dispatch pool at capacity (claim already taken — must not strand it)
"""
import json
import threading
from unittest.mock import patch

from tools.cronjob_tools import (
    _try_dispatch_background_run,
    cronjob,
)


_JOB = {"id": "job-bg-1", "name": "bg run", "prompt": "hi",
        "schedule": {"kind": "cron", "expr": "0 9 * * *"}}


def _job(job_id):
    """Per-test job dict with a UNIQUE id.

    Background workers outlive their test (daemon executor) and hold the id
    in the scheduler's shared running set until the run finishes; reusing one
    id across tests makes the in-flight dedupe guard see a phantom
    'already running' from a previous test's straggler worker.
    """
    return {"id": job_id, "name": f"bg run {job_id}", "prompt": "hi",
            "schedule": {"kind": "cron", "expr": "0 9 * * *"}}


def _bound_session_key(key="agent:main:telegram:dm:123"):
    """Context manager binding the approval session key contextvar."""
    import contextlib

    from tools.approval import _approval_session_key

    @contextlib.contextmanager
    def _cm():
        token = _approval_session_key.set(key)
        try:
            yield
        finally:
            _approval_session_key.reset(token)

    return _cm()


class TestBackgroundDispatch:
    def test_dispatches_and_returns_handle_immediately(self):
        """With a routable session, run claims sync then dispatches async."""
        run_started = threading.Event()
        run_release = threading.Event()

        def slow_run_one_job(job, **kw):
            run_started.set()
            assert run_release.wait(timeout=5.0)
            return True

        with _bound_session_key():
            with patch("tools.cronjob_tools.claim_job_for_fire", side_effect=lambda jid, **kw: {**_job(jid), "fire_claim": {"by": "bg-owner"}}) as m_claim, \
                 patch("cron.scheduler.run_one_job", side_effect=slow_run_one_job), \
                 patch("tools.cronjob_tools.get_job",
                       return_value={"last_status": "ok", "last_error": None}):
                res = _try_dispatch_background_run(_job('job-bg-01'))

        try:
            # Returned BEFORE the job finished — that's the whole point.
            assert res is not None
            assert res["claimed"] is True
            assert res["dispatched"] is True
            assert res["delegation_id"]
            m_claim.assert_called_once_with("job-bg-01", return_job=True)
            # The job actually starts on the daemon executor.
            assert run_started.wait(timeout=5.0), "job never started in background"
        finally:
            run_release.set()

    def test_completion_event_reaches_shared_queue(self):
        """The finished run pushes a type='async_delegation' event carrying
        the job outcome onto process_registry.completion_queue."""
        import time

        from tools.process_registry import process_registry

        # The runner executes on a daemon thread — the patches must stay
        # active until the completion event lands, so poll INSIDE the blocks.
        with _bound_session_key("agent:main:telegram:dm:777"):
            with patch("tools.cronjob_tools.claim_job_for_fire", side_effect=lambda jid, **kw: {**_job(jid), "fire_claim": {"by": "bg-owner"}}), \
                 patch("cron.scheduler.run_one_job", return_value=True), \
                 patch("tools.cronjob_tools.get_job",
                       return_value={"last_status": "ok", "last_error": None,
                                     "next_run_at": "2026-08-07T09:00:00"}):
                res = _try_dispatch_background_run(_job('job-bg-02'))
                assert res["dispatched"] is True

                found = None
                for _ in range(100):
                    try:
                        evt = process_registry.completion_queue.get_nowait()
                    except Exception:
                        time.sleep(0.05)
                        continue
                    if (evt.get("type") == "async_delegation"
                            and evt.get("delegation_id") == res["delegation_id"]):
                        found = evt
                        break
                    process_registry.completion_queue.put(evt)
                    time.sleep(0.05)
        assert found is not None, "completion event never reached the queue"
        assert found["session_key"] == "agent:main:telegram:dm:777"
        assert found["status"] == "completed"
        assert "bg run" in (found.get("summary") or "")
        assert "Next scheduled run" in found["summary"]

    def test_failed_run_reports_error_status_in_event(self):
        import time

        from tools.process_registry import process_registry

        with _bound_session_key("agent:main:telegram:dm:778"):
            with patch("tools.cronjob_tools.claim_job_for_fire", side_effect=lambda jid, **kw: {**_job(jid), "fire_claim": {"by": "bg-owner"}}), \
                 patch("cron.scheduler.run_one_job", return_value=True), \
                 patch("tools.cronjob_tools.get_job",
                       return_value={"last_status": "error",
                                     "last_error": "provider exploded"}):
                res = _try_dispatch_background_run(_job('job-bg-03'))
                assert res["dispatched"] is True

                found = None
                for _ in range(100):
                    try:
                        evt = process_registry.completion_queue.get_nowait()
                    except Exception:
                        time.sleep(0.05)
                        continue
                    if evt.get("delegation_id") == res["delegation_id"]:
                        found = evt
                        break
                    process_registry.completion_queue.put(evt)
                    time.sleep(0.05)
        assert found is not None
        assert found["status"] == "error"
        assert "provider exploded" in (found.get("error") or "")

    def test_claim_lost_reports_immediately_without_dispatch(self):
        """Paused/already-firing jobs report in the tool response, not as a
        delayed completion event."""
        with _bound_session_key():
            with patch("tools.cronjob_tools.claim_job_for_fire", return_value=False), \
                 patch("tools.cronjob_tools.get_job",
                       return_value={**_JOB, "enabled": False}), \
                 patch("tools.async_delegation.dispatch_async_delegation") as m_disp:
                res = _try_dispatch_background_run(_job('job-bg-04'))
        assert res["claimed"] is False
        assert "paused/disabled" in res["error"]
        m_disp.assert_not_called()


class TestSyncFallbacks:
    def test_no_session_key_falls_back_to_sync(self):
        """Direct Python callers (no agent session) keep the sync path."""
        res = _try_dispatch_background_run(_job('job-bg-05'))
        assert res is None

    def test_async_delivery_unsupported_falls_back_to_sync(self):
        """One-shot runtimes (hermes -z, cron child, Kanban) keep sync."""
        with _bound_session_key():
            with patch("gateway.session_context.async_delivery_supported",
                       return_value=False):
                res = _try_dispatch_background_run(_job('job-bg-06'))
        assert res is None

    def test_pool_at_capacity_runs_inline(self, sd03b_cron_state):
        """Typed pool refusal keeps exactly one inline run with its claim owner."""
        state = sd03b_cron_state
        state.dispatch.return_value = {
            "status": "rejected", "error": "capacity",
            "error_code": "pool_capacity", "execution_started": False,
        }
        res = _try_dispatch_background_run(state.job)
        assert res["dispatched"] is False
        assert res["success"] is True
        state.run.assert_called_once_with(state.claimed_job, extra_prompt=None)
        state.release.assert_not_called()
        state.mark.assert_not_called()


class TestInFlightDedupe:
    """Manual runs must not double-fire a job that is already mid-run
    (salvaged from #53395 by @izumi0uu): the fire claim's 300s TTL is
    routinely outlived by real jobs, so the claim alone can't prevent it."""

    def test_run_claimed_job_skips_when_already_running(self):
        """The authoritative guard: _run_claimed_job refuses to fire a job
        whose id is already registered in the scheduler running set."""
        from cron import scheduler as sched
        from tools.cronjob_tools import _run_claimed_job

        assert sched.try_register_running_job("job-bg-08")   # simulate ticker mid-run
        try:
            with patch("cron.scheduler.run_one_job") as m_run:
                res = _run_claimed_job(_job('job-bg-08'))
            assert res["success"] is False
            assert "already running" in res["error"]
            m_run.assert_not_called()
        finally:
            sched.release_running_job("job-bg-08")

    def test_run_claimed_job_registers_and_releases(self):
        """A normal run holds the registration for run_one_job's duration and
        releases it after — visible to get_running_job_ids mid-run."""
        from cron import scheduler as sched
        from tools.cronjob_tools import _run_claimed_job

        seen_during_run = {}

        def probe_run(job, **kw):
            seen_during_run["registered"] = "job-bg-09" in sched.get_running_job_ids()
            return True

        with patch("cron.scheduler.run_one_job", side_effect=probe_run), \
             patch("tools.cronjob_tools.get_job",
                   return_value={"last_status": "ok", "last_error": None}):
            res = _run_claimed_job(_job('job-bg-09'))

        assert res["success"] is True
        assert seen_during_run["registered"] is True
        assert "job-bg-09" not in sched.get_running_job_ids()   # released after

    def test_background_dispatch_reports_running_job_immediately(self):
        """The dispatch path pre-checks the running set so a mid-run job
        reports in the tool response, not as a delayed completion event."""
        from cron import scheduler as sched

        assert sched.try_register_running_job("job-bg-10")
        try:
            with _bound_session_key():
                with patch("tools.cronjob_tools.claim_job_for_fire") as m_claim, \
                     patch("tools.async_delegation.dispatch_async_delegation") as m_disp:
                    res = _try_dispatch_background_run(_job('job-bg-10'))
            assert res["claimed"] is False
            assert "already running" in res["error"]
            m_claim.assert_not_called()   # no claim consumed for a skipped run
            m_disp.assert_not_called()
        finally:
            sched.release_running_job("job-bg-10")

    def test_ticker_guard_uses_shared_helpers(self):
        """The ticker's _submit_with_guard and manual runs share ONE dedupe
        owner: registration through either side blocks the other."""
        from cron import scheduler as sched

        # Manual-run registration…
        assert sched.try_register_running_job("job-shared-1")
        try:
            # …is exactly what the ticker-side helper consults.
            assert not sched.try_register_running_job("job-shared-1")
            assert "job-shared-1" in sched.get_running_job_ids()
        finally:
            sched.release_running_job("job-shared-1")
        assert "job-shared-1" not in sched.get_running_job_ids()
        # Idempotent release: never raises on a non-member.
        sched.release_running_job("job-shared-1")


class TestCronjobRunToolIntegration:
    def test_run_action_returns_background_note(self):
        """cronjob(action='run') surfaces the handle + do-not-wait note."""
        with _bound_session_key():
            with patch("tools.cronjob_tools.resolve_job_ref", return_value=_job('job-bg-12')), \
                 patch("tools.cronjob_tools.claim_job_for_fire", side_effect=lambda jid, **kw: {**_job(jid), "fire_claim": {"by": "bg-owner"}}), \
                 patch("cron.scheduler.run_one_job", return_value=True), \
                 patch("tools.cronjob_tools.get_job",
                       return_value={"id": "job-bg-12", "name": "bg run",
                                     "last_status": "ok", "last_error": None}):
                out = json.loads(cronjob(action="run", job_id="job-bg-12"))

        assert out["success"] is True
        assert out["job"]["executed"] is True
        assert out["job"]["execution_mode"] == "background"
        assert out["job"]["delegation_id"]
        assert "background" in out["note"]

    def test_run_action_sync_path_unchanged_without_session(self):
        """No session context → the legacy synchronous behavior (executed +
        execution_success populated from the completed run)."""
        ran = {"job": "after-run", "last_status": "ok", "last_error": None}
        with patch("tools.cronjob_tools.resolve_job_ref", return_value=_job('job-bg-13')), \
             patch("tools.cronjob_tools.claim_job_for_fire", side_effect=lambda jid, **kw: {**_job(jid), "fire_claim": {"by": "bg-owner"}}) as m_claim, \
             patch("cron.scheduler.run_one_job", return_value=True) as m_run, \
             patch("tools.cronjob_tools.get_job", return_value=ran):
            out = json.loads(cronjob(action="run", job_id="job-bg-13"))

        assert out["success"] is True
        assert out["job"]["executed"] is True
        assert out["job"]["execution_success"] is True
        m_claim.assert_called_once_with("job-bg-13", return_job=True)
        m_run.assert_called_once()


# SD03B fixtures replace every execution/claim/routing/config dependency with
# in-memory recording objects. No runner, provider, DB or service is started.
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def sd03b_cron_state(monkeypatch):
    import tools.cronjob_tools as caller
    import tools.async_delegation as async_owner
    import tools.delegate_tool as delegate_owner
    import tools.approval as approval
    import cron.jobs as job_owner

    scheduler = ModuleType("cron.scheduler")
    scheduler.get_running_job_ids = Mock(return_value=set())
    scheduler.run_one_job = Mock(side_effect=AssertionError("real cron runner forbidden"))
    scheduler.try_register_running_job = Mock(return_value=True)
    scheduler.release_running_job = Mock()
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    executions = ModuleType("cron.executions")
    executions.recover_interrupted_executions = Mock(return_value=0)
    monkeypatch.setitem(sys.modules, "cron.executions", executions)
    session_context = ModuleType("gateway.session_context")
    session_context.async_delivery_supported = Mock(return_value=True)
    session_context.get_session_env = Mock(return_value="")
    monkeypatch.setitem(sys.modules, "gateway.session_context", session_context)
    monkeypatch.setattr(approval, "get_current_session_key", lambda **_kw: "sd03b-session")
    monkeypatch.setattr(async_owner, "_current_origin_session_id", lambda: "sd03b-parent")
    monkeypatch.setattr(delegate_owner, "_get_max_async_children", lambda: 1)

    job = _job("sd03b-job")
    claimed_job = {**job, "fire_claim": {"by": "sd03b-fire-owner", "at": "2026-10-04T00:00:00Z"}}
    receipt = object()  # An ephemeral exact object; never serialize or persist it.
    old_claim = Mock(return_value=claimed_job)
    receipt_claim = Mock(return_value=(claimed_job, receipt))
    release = Mock(return_value={"status": "released"})
    # Both surfaces are present so baseline and candidate enter the same
    # inert path, while candidate is required to release the exact receipt.
    for owner in (caller, job_owner):
        monkeypatch.setattr(owner, "claim_job_for_fire", old_claim)
        monkeypatch.setattr(owner, "claim_job_for_fire_with_unstarted_receipt", receipt_claim, raising=False)
        monkeypatch.setattr(owner, "release_unstarted_fire_claim", release, raising=False)
    dispatch = Mock()
    monkeypatch.setattr(async_owner, "dispatch_async_delegation", dispatch)
    run = Mock(return_value={"claimed": True, "success": True, "error": None})
    mark = Mock()
    monkeypatch.setattr(caller, "_run_claimed_job", run)
    monkeypatch.setattr(caller, "mark_job_run", mark)
    monkeypatch.setattr(caller, "get_job", Mock(return_value=claimed_job))
    monkeypatch.setattr(caller, "_latest_job_output_excerpt", Mock(return_value=None))
    monkeypatch.setattr(caller, "_notify_provider_jobs_changed_safe", Mock())
    return SimpleNamespace(
        caller=caller, job=job, claimed_job=claimed_job, receipt=receipt,
        old_claim=old_claim, receipt_claim=receipt_claim, release=release,
        dispatch=dispatch, run=run, mark=mark, scheduler=scheduler,
        reclaim=executions.recover_interrupted_executions,
    )


def _sd03b_assert_exact_receipt_released(state):
    state.release.assert_called_once()
    args, kwargs = state.release.call_args
    assert any(value is state.receipt for value in (*args, *kwargs.values()))


@pytest.mark.parametrize("reservation_released", [False, True])
@pytest.mark.parametrize("error_code", ["durable_backlog_full", "durable_storage_unavailable"])
def test_background_storage_refusal_releases_unstarted_claim_without_inline(
    sd03b_cron_state, error_code, reservation_released
):
    state = sd03b_cron_state
    state.dispatch.return_value = {
        "status": "rejected", "error_code": error_code,
        "execution_started": False, "error": "inert storage refusal",
        "delegation_id": "sd03b-refused-reservation",
        "durable_reservation_released": reservation_released,
    }
    result = state.caller._try_dispatch_background_run(state.job)
    state.run.assert_not_called()
    state.scheduler.run_one_job.assert_not_called()
    state.mark.assert_not_called()
    _sd03b_assert_exact_receipt_released(state)
    assert result["status"] == "rejected"
    assert result["error_code"] == error_code
    assert result["execution_started"] is False
    assert result["delegation_id"] == "sd03b-refused-reservation"
    assert result["durable_reservation_released"] is reservation_released
    assert result["dispatched"] is False
    assert result["success"] is False
    assert result["claim_release_status"] == "released"
    assert result["claim_release_confirmed"] is True


@pytest.mark.parametrize("release_status", ["conflict", "missing", "write_uncertain"])
def test_background_storage_refusal_reports_unconfirmed_claim_release(
    sd03b_cron_state, release_status
):
    state = sd03b_cron_state
    state.dispatch.return_value = {
        "status": "rejected", "error_code": "durable_storage_unavailable",
        "execution_started": False, "error": "inert storage refusal",
    }
    state.release.return_value = {"status": release_status}
    result = state.caller._try_dispatch_background_run(state.job)
    state.run.assert_not_called()
    state.scheduler.run_one_job.assert_not_called()
    state.mark.assert_not_called()
    _sd03b_assert_exact_receipt_released(state)
    assert result["status"] == "rejected"
    assert result["error_code"] == "durable_storage_unavailable"
    assert result["execution_started"] is False
    assert result["success"] is False
    assert result["claim_release_status"] == release_status
    assert result["claim_release_confirmed"] is False


def test_background_dispatch_uncertain_keeps_claim_without_inline(sd03b_cron_state):
    state = sd03b_cron_state
    state.dispatch.return_value = {
        "status": "dispatch_uncertain", "error_code": "scheduling_uncertain",
        "execution_started": None, "delegation_id": "sd03b-uncertain",
        "error": "inert uncertain submit",
    }
    result = state.caller._try_dispatch_background_run(state.job)
    state.run.assert_not_called()
    state.scheduler.run_one_job.assert_not_called()
    state.release.assert_not_called()
    state.mark.assert_not_called()
    assert result["status"] == "dispatch_uncertain"
    assert result["error_code"] == "scheduling_uncertain"
    assert result["execution_started"] is None
    assert result["delegation_id"] == "sd03b-uncertain"


def test_background_pool_capacity_runs_owner_bearing_claim_once(sd03b_cron_state):
    state = sd03b_cron_state
    state.dispatch.return_value = {
        "status": "rejected", "error_code": "pool_capacity",
        "execution_started": False, "error": "inert pool capacity",
    }
    result = state.caller._try_dispatch_background_run(state.job, extra_prompt="inert context")
    state.run.assert_called_once_with(state.claimed_job, extra_prompt="inert context")
    state.release.assert_not_called()
    state.mark.assert_not_called()
    state.scheduler.run_one_job.assert_not_called()
    assert result["dispatched"] is False
    assert result["success"] is True


# SD03B public response fixtures: the real wrapper is exercised with every
# execution, claim, store, scanner and notification dependency made inert.
@pytest.fixture
def sd03b_public_state(monkeypatch):
    import tools.cronjob_tools as caller

    job = {"id": "sd03b-public-job", "name": "inert public job"}
    refreshed_job = {"id": "sd03b-public-job", "name": "inert refreshed job"}
    resolve = Mock(return_value=job)
    read = Mock(return_value=refreshed_job)
    # This small fixed view is independent of the production formatter/schema.
    format_job = Mock(side_effect=lambda _job: {"id": "formatted-job", "view": "inert"})
    scanner = Mock(return_value=None)
    background = Mock()
    sync = Mock(return_value={"claimed": True, "success": True, "error": None})
    notify = Mock()
    for name, value in (
        ("resolve_job_ref", resolve), ("get_job", read),
        ("_format_job", format_job), ("_scan_cron_prompt", scanner),
        ("_try_dispatch_background_run", background),
        ("_execute_job_now", sync), ("_notify_provider_jobs_changed_safe", notify),
    ):
        monkeypatch.setattr(caller, name, value)
    guards = {}
    for name in (
        "claim_job_for_fire", "claim_job_for_fire_with_unstarted_receipt",
        "release_unstarted_fire_claim", "mark_job_run", "pause_job",
        "resume_job", "remove_job", "update_job", "list_jobs",
        "parse_schedule", "_run_claimed_job", "_latest_job_output_excerpt",
        "_origin_from_env", "_gateway_liveness_notice",
        "_validate_cron_script_path", "_validate_cron_base_url",
        "_validate_bot_chat_deliver", "_resolve_cron_context_deliver",
    ):
        guard = Mock(side_effect=AssertionError("public wrapper crossed inert guard: " + name))
        monkeypatch.setattr(caller, name, guard, raising=False)
        guards[name] = guard
    return SimpleNamespace(
        caller=caller, job=job, refreshed_job=refreshed_job,
        resolve=resolve, read=read, format_job=format_job, scanner=scanner,
        background=background, sync=sync, notify=notify, guards=guards,
    )


def _sd03b_public_call(state, action="run"):
    return json.loads(state.caller.cronjob(
        action=action, job_id="inert requested name",
        session_id="sd03b-public-session", prompt="inert per-run context",
    ))


def _sd03b_public_assert_inert_route(state, *, read_after_run, notifications):
    state.resolve.assert_called_once_with("inert requested name")
    state.scanner.assert_called_once_with("inert per-run context")
    state.background.assert_called_once_with(
        state.job, session_id="sd03b-public-session", extra_prompt="inert per-run context",
    )
    if read_after_run:
        state.read.assert_called_once_with("sd03b-public-job")
        state.format_job.assert_called_once_with(state.refreshed_job)
    else:
        state.read.assert_not_called()
        state.format_job.assert_called_once_with(state.job)
    assert state.notify.call_count == notifications
    if notifications:
        state.notify.assert_called_once_with()
    for guard in state.guards.values():
        guard.assert_not_called()


@pytest.mark.parametrize("error_code", ["durable_backlog_full", "durable_storage_unavailable"])
@pytest.mark.parametrize("reservation_released", [False, True])
@pytest.mark.parametrize("release_status,release_confirmed", [
    ("released", True), ("write_uncertain", False),
])
def test_public_storage_refusal_preserves_dispatch_evidence(
    sd03b_public_state, error_code, reservation_released, release_status, release_confirmed
):
    state = sd03b_public_state
    state.background.return_value = {
        "claimed": True, "dispatched": False, "success": False,
        "status": "rejected", "error_code": error_code,
        "execution_started": False, "error": "inert storage refusal",
        "delegation_id": "sd03b-public-refused-reservation",
        "durable_reservation_released": reservation_released,
        "claim_release_status": release_status,
        "claim_release_confirmed": release_confirmed,
    }
    result = _sd03b_public_call(state)
    assert result == {
        "success": False,
        "job": {"id": "formatted-job", "view": "inert", "executed": False},
        "status": "rejected", "error_code": error_code,
        "execution_started": False, "error": "inert storage refusal",
        "delegation_id": "sd03b-public-refused-reservation",
        "durable_reservation_released": reservation_released,
        "claim_release_status": release_status,
        "claim_release_confirmed": release_confirmed,
    }
    state.sync.assert_not_called()
    _sd03b_public_assert_inert_route(state, read_after_run=False, notifications=0)


def test_public_storage_refusal_does_not_invent_optional_fields(sd03b_public_state):
    state = sd03b_public_state
    state.background.return_value = {
        "claimed": True, "dispatched": False, "success": False,
        "status": "rejected", "error_code": "executor_unavailable",
        "execution_started": False, "error": "inert unavailable executor",
    }
    result = _sd03b_public_call(state)
    assert result == {
        "success": False,
        "job": {"id": "formatted-job", "view": "inert", "executed": False},
        "status": "rejected", "error_code": "executor_unavailable",
        "execution_started": False, "error": "inert unavailable executor",
    }
    for key in (
        "delegation_id", "durable_reservation_released",
        "claim_release_status", "claim_release_confirmed",
    ):
        assert key not in result
    state.sync.assert_not_called()
    _sd03b_public_assert_inert_route(state, read_after_run=False, notifications=0)


@pytest.mark.parametrize("action", ["run", "run_now", "trigger"])
def test_public_dispatch_uncertain_preserves_handle_and_no_replay_note(sd03b_public_state, action):
    state = sd03b_public_state
    state.background.return_value = {
        "claimed": True, "dispatched": False, "success": False,
        "status": "dispatch_uncertain", "error_code": "scheduling_uncertain",
        "execution_started": None, "error": "inert uncertain submit",
        "delegation_id": "sd03b-public-uncertain",
        "durable_reservation_released": False,
    }
    result = _sd03b_public_call(state, action)
    assert result == {
        "success": False,
        "job": {"id": "formatted-job", "view": "inert", "executed": None},
        "status": "dispatch_uncertain", "error_code": "scheduling_uncertain",
        "execution_started": None, "error": "inert uncertain submit",
        "delegation_id": "sd03b-public-uncertain",
        "durable_reservation_released": False,
        "note": (
            "Work may already be running. Keep the delegation handle; do not retry "
            "or run inline while submission remains uncertain."
        ),
    }
    state.sync.assert_not_called()
    _sd03b_public_assert_inert_route(state, read_after_run=False, notifications=0)


def test_public_confirmed_dispatch_keeps_existing_payload(sd03b_public_state):
    state = sd03b_public_state
    state.background.return_value = {
        "claimed": True, "dispatched": True, "delegation_id": "sd03b-public-confirmed",
    }
    result = _sd03b_public_call(state)
    assert result == {
        "success": True,
        "job": {
            "id": "formatted-job", "view": "inert", "executed": True,
            "execution_mode": "background", "delegation_id": "sd03b-public-confirmed",
        },
        "note": (
            "The job is running in the background. You and the user can keep working; "
            "its outcome re-enters the conversation as a new message when it finishes. "
            "Do not wait or poll — just continue."
        ),
    }
    state.sync.assert_not_called()
    _sd03b_public_assert_inert_route(state, read_after_run=True, notifications=1)


def test_public_typed_pool_fallback_keeps_existing_terminal_payload(sd03b_public_state):
    state = sd03b_public_state
    # The core helper has already executed the permitted pool-capacity fallback.
    state.background.return_value = {
        "claimed": True, "dispatched": False, "success": True, "error": None,
    }
    result = _sd03b_public_call(state)
    assert result == {
        "success": True,
        "job": {
            "id": "formatted-job", "view": "inert", "executed": True,
            "execution_success": True,
        },
    }
    state.sync.assert_not_called()
    _sd03b_public_assert_inert_route(state, read_after_run=True, notifications=1)


def test_public_background_unsupported_executes_inert_sync_once(sd03b_public_state):
    state = sd03b_public_state
    state.background.return_value = None
    result = _sd03b_public_call(state)
    assert result == {
        "success": True,
        "job": {
            "id": "formatted-job", "view": "inert", "executed": True,
            "execution_success": True,
        },
    }
    state.sync.assert_called_once_with(state.job, extra_prompt="inert per-run context")
    _sd03b_public_assert_inert_route(state, read_after_run=True, notifications=1)


def test_public_claim_lost_keeps_existing_skipped_payload(sd03b_public_state):
    state = sd03b_public_state
    state.background.return_value = {
        "claimed": False, "dispatched": False, "success": False,
        "error": "inert claim lost",
    }
    result = _sd03b_public_call(state)
    assert result == {
        "success": True,
        "job": {
            "id": "formatted-job", "view": "inert", "executed": False,
            "execution_success": False, "execution_skipped": "inert claim lost",
        },
    }
    state.sync.assert_not_called()
    _sd03b_public_assert_inert_route(state, read_after_run=True, notifications=0)
