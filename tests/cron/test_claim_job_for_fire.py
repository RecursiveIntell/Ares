"""Tests for the store-level CAS fire claim (Phase 4C).

`claim_job_for_fire` gives multi-machine at-most-once semantics when an external
scheduler (Chronos) fires a job: across N gateway replicas, exactly ONE wins the
claim for a given fire. Single-machine deployments always win (unaffected).

These exercise the real store against a temp HERMES_HOME (no mocks) per the
E2E-over-mocks discipline for file-touching code.
"""
import threading
import time

import pytest


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so jobs.json doesn't touch the real store."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # cron.jobs caches no home at import; get_hermes_home() reads the env live.
    yield tmp_path


def test_claim_succeeds_once_then_blocks(temp_home):
    """First claim for a fire wins; a second claim for the same fire loses, and
    next_run_at is advanced (a re-delivery for the old time can't re-fire)."""
    from cron.jobs import create_job, claim_job_for_fire, get_job

    job = create_job(prompt="x", schedule="every 5m", name="t")
    jid = job["id"]
    before = get_job(jid)["next_run_at"]

    assert claim_job_for_fire(jid) is True
    assert claim_job_for_fire(jid) is False
    assert get_job(jid)["next_run_at"] != before


def test_claim_oneshot_cannot_be_double_claimed(temp_home):
    """A one-shot can't be double-claimed (the fresh claim blocks the retry)."""
    from cron.jobs import create_job, claim_job_for_fire

    job = create_job(prompt="x", schedule="30m", name="o")
    assert claim_job_for_fire(job["id"]) is True
    assert claim_job_for_fire(job["id"]) is False


def test_claim_unknown_job_returns_false(temp_home):
    from cron.jobs import claim_job_for_fire

    assert claim_job_for_fire("nope-does-not-exist") is False


def test_claim_paused_job_returns_false(temp_home):
    """A paused job can't be claimed."""
    from cron.jobs import create_job, claim_job_for_fire, pause_job

    job = create_job(prompt="x", schedule="every 5m", name="p")
    pause_job(job["id"])
    assert claim_job_for_fire(job["id"]) is False


def test_forced_claim_atomically_resumes_paused_job(temp_home):
    """Explicit manual fire may resume a paused job without exposing a due
    intermediate state to the ticker."""
    from cron.jobs import create_job, claim_job_for_fire, get_job, pause_job

    job = create_job(prompt="x", schedule="every 5m", name="manual")
    pause_job(job["id"])

    assert claim_job_for_fire(job["id"], force=True) is True
    claimed = get_job(job["id"])
    assert claimed["enabled"] is True
    assert claimed["state"] == "scheduled"
    assert claimed["paused_at"] is None
    assert claimed["paused_reason"] is None
    assert claimed["fire_claim"] is not None


def test_stale_claim_is_reclaimable(temp_home, monkeypatch):
    """A claim older than the TTL is overwritten — the fire isn't stuck forever
    if the winning machine crashed before mark_job_run cleared the claim."""
    from cron.jobs import create_job, claim_job_for_fire

    job = create_job(prompt="x", schedule="every 5m", name="s")
    jid = job["id"]
    assert claim_job_for_fire(jid) is True
    # With a 0s TTL, the existing claim is always considered stale.
    assert claim_job_for_fire(jid, claim_ttl_seconds=0) is True


def test_mark_job_run_clears_claim(temp_home):
    """After a recurring job completes, its claim is cleared so the next fire
    can be claimed again."""
    from cron.jobs import create_job, claim_job_for_fire, mark_job_run, get_job

    job = create_job(prompt="x", schedule="every 5m", name="c")
    jid = job["id"]
    assert claim_job_for_fire(jid) is True
    assert get_job(jid).get("fire_claim") is not None

    mark_job_run(jid, success=True)
    assert get_job(jid).get("fire_claim") is None
    # …and the re-armed recurring job is claimable again.
    assert claim_job_for_fire(jid) is True


def test_fire_claim_heartbeat_refreshes_only_expected_owner(temp_home, monkeypatch):
    from datetime import datetime, timedelta

    import cron.jobs as jobs

    job = jobs.create_job(prompt="x", schedule="every 5m", name="heartbeat")
    assert jobs.claim_job_for_fire(job["id"]) is True
    claimed = jobs.get_job(job["id"])["fire_claim"]
    claimed_at = datetime.fromisoformat(claimed["at"])
    monkeypatch.setattr(
        jobs,
        "_hermes_now",
        lambda: claimed_at + timedelta(seconds=30),
    )

    assert jobs.heartbeat_fire_claim(
        job["id"],
        expected_owner=claimed["by"],
    ) is True
    refreshed = jobs.get_job(job["id"])["fire_claim"]
    assert refreshed["at"] != claimed["at"]
    assert refreshed["by"] == claimed["by"]
    assert jobs.heartbeat_fire_claim(
        job["id"],
        expected_owner="replacement-owner",
    ) is False


def test_reclaimed_fire_uses_new_owner_token(temp_home, monkeypatch):
    from datetime import datetime, timedelta

    import cron.jobs as jobs

    job = jobs.create_job(prompt="x", schedule="every 5m", name="reclaim")
    assert jobs.claim_job_for_fire(job["id"]) is True
    original = dict(jobs.get_job(job["id"])["fire_claim"])
    original_at = datetime.fromisoformat(original["at"])
    monkeypatch.setattr(
        jobs,
        "_hermes_now",
        lambda: original_at + timedelta(seconds=301),
    )

    assert jobs.claim_job_for_fire(job["id"]) is True
    replacement = dict(jobs.get_job(job["id"])["fire_claim"])
    assert replacement["by"] != original["by"]
    assert jobs.heartbeat_fire_claim(
        job["id"],
        expected_owner=original["by"],
    ) is False
    assert jobs.get_job(job["id"])["fire_claim"] == replacement


def test_stale_fire_owner_cannot_mark_replacement_run(temp_home):
    import cron.jobs as jobs

    job = jobs.create_job(prompt="x", schedule="every 5m", name="fenced")
    assert jobs.claim_job_for_fire(job["id"]) is True
    original = dict(jobs.get_job(job["id"])["fire_claim"])
    records = jobs.load_jobs()
    records[0]["fire_claim"] = {"at": original["at"], "by": "replacement"}
    jobs.save_jobs(records)

    assert jobs.mark_job_run(
        job["id"],
        success=True,
        expected_fire_owner=original["by"],
    ) is False
    persisted = jobs.get_job(job["id"])
    assert persisted["fire_claim"]["by"] == "replacement"
    assert persisted.get("last_run_at") is None


def test_fire_claim_fence_serializes_terminal_revocation(temp_home):
    """A side effect authorized by owner linearizes before terminal revocation."""
    from cron.jobs import (
        claim_job_for_fire,
        create_job,
        fire_claim_fence,
        mark_job_run,
    )

    job = create_job(prompt="x", schedule="every 5m", name="fenced-side-effect")
    claimed = claim_job_for_fire(job["id"], return_job=True)
    assert isinstance(claimed, dict)
    owner = claimed["fire_claim"]["by"]
    terminal_done = threading.Event()

    def finish_run():
        mark_job_run(job["id"], True, expected_fire_owner=owner)
        terminal_done.set()

    with fire_claim_fence(job["id"], expected_owner=owner) as owns_claim:
        assert owns_claim is True
        thread = threading.Thread(target=finish_run)
        thread.start()
        time.sleep(0.05)
        assert terminal_done.is_set() is False

    thread.join(timeout=1)
    assert terminal_done.is_set() is True


def test_fire_claim_fence_rejects_stale_owner(temp_home):
    from cron.jobs import claim_job_for_fire, create_job, fire_claim_fence

    job = create_job(prompt="x", schedule="every 5m", name="stale-fence")
    claim_job_for_fire(job["id"])

    with fire_claim_fence(job["id"], expected_owner="stale") as owns_claim:
        assert owns_claim is False


# SD03B controls: real claim/release owners, dictionary-only persistence and
# inert locks. Select this class alone; the preceding legacy file tests use
# task-local filesystem stores and include a threaded fence control.
@pytest.fixture
def sd03b_memory_jobs(monkeypatch):
    from contextlib import contextmanager
    from copy import deepcopy
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from cron import jobs as subject

    store = SimpleNamespace(
        subject=subject,
        jobs=[{
            "id": "inert-job",
            "name": "inert",
            "prompt": "inert request",
            "enabled": True,
            "state": "scheduled",
            "schedule": {"kind": "interval", "seconds": 300},
            "next_run_at": "2026-10-04T12:00:00+00:00",
            "repeat": {"times": 3, "completed": 1},
            "last_status": "ok",
            "last_run_at": "2026-10-04T11:00:00+00:00",
        }],
        trace=[],
        fire_lock_allowed=True,
        save_failure=None,
        saves=0,
    )
    now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    sequence = [0]

    @contextmanager
    def fire_lock(job_id):
        store.trace.append(("fire_enter", job_id))
        try:
            yield store.fire_lock_allowed
        finally:
            store.trace.append(("fire_exit", job_id))

    @contextmanager
    def jobs_lock():
        store.trace.append(("jobs_enter",))
        try:
            yield
        finally:
            store.trace.append(("jobs_exit",))

    def load():
        store.trace.append(("load",))
        return deepcopy(store.jobs)

    def save(records, **kwargs):
        store.trace.append(("save",))
        store.saves += 1
        if store.save_failure == "before":
            raise OSError("inert save refusal before publication")
        store.jobs = deepcopy(records)
        if store.save_failure == "after":
            raise OSError("inert save uncertainty after publication")

    def next_run(schedule, reference):
        store.trace.append(("compute", deepcopy(schedule), reference))
        return "2026-10-04T12:05:00+00:00"

    def uuid4():
        sequence[0] += 1
        return SimpleNamespace(hex=f"inert-acquisition-{sequence[0]}")

    def forbidden_accounting(*args, **kwargs):
        pytest.fail("unstarted disposition must not call run accounting")

    monkeypatch.setattr(subject, "_fire_job_lock", fire_lock)
    monkeypatch.setattr(subject, "_jobs_lock", jobs_lock)
    monkeypatch.setattr(subject, "load_jobs", load)
    monkeypatch.setattr(subject, "save_jobs", save)
    monkeypatch.setattr(subject, "_hermes_now", lambda: now)
    monkeypatch.setattr(subject, "compute_next_run", next_run)
    monkeypatch.setattr(subject, "_machine_id", lambda: "inert-owner")
    monkeypatch.setattr(subject, "uuid", SimpleNamespace(uuid4=uuid4))
    monkeypatch.setattr(subject, "mark_job_run", forbidden_accounting)
    monkeypatch.setattr(subject, "_mark_job_run_locked", forbidden_accounting)
    return store


def _sd03b_api(store, name):
    # Missing baseline API is an assertion in the selected test, never a
    # collection-time import or attribute error.
    api = getattr(store.subject, name, None)
    assert callable(api), f"SD03B owner API is absent: {name}"
    return api


def _sd03b_claim(store):
    result = _sd03b_api(
        store, "claim_job_for_fire_with_unstarted_receipt",
    )("inert-job")
    assert isinstance(result, tuple) and len(result) == 2
    claimed, receipt = result
    assert isinstance(claimed, dict)
    return claimed, receipt


def _sd03b_release(store, receipt):
    result = _sd03b_api(store, "release_unstarted_fire_claim")(receipt)
    assert isinstance(result, dict)
    assert isinstance(result.get("released"), bool)
    return result


class TestSD03BUnstartedFireClaim:
    def test_legacy_bool_dict_and_force_contract(self, sd03b_memory_jobs):
        from inspect import signature

        store = sd03b_memory_jobs
        claim = store.subject.claim_job_for_fire
        assert tuple(signature(claim).parameters) == (
            "job_id", "claim_ttl_seconds", "force", "return_job",
        )
        assert claim("missing") is False
        assert claim("inert-job") is True
        assert claim("inert-job") is False
        snapshot = claim("inert-job", claim_ttl_seconds=0, return_job=True)
        assert isinstance(snapshot, dict)
        assert snapshot["fire_claim"]["by"] == store.jobs[0]["fire_claim"]["by"]
        snapshot["repeat"]["completed"] = 99
        assert store.jobs[0]["repeat"]["completed"] == 1
        store.jobs[0].pop("fire_claim")
        store.jobs[0].update({
            "enabled": False, "state": "paused", "paused_at": "paused",
            "paused_reason": "operator",
        })
        assert claim("inert-job") is False
        assert claim("inert-job", force=True) is True
        assert store.jobs[0]["enabled"] is True
        assert store.jobs[0]["state"] == "scheduled"
        assert store.jobs[0]["paused_at"] is None
        assert store.jobs[0]["paused_reason"] is None

    @pytest.mark.parametrize("prior", ["absent", "null", "value"])
    def test_receipt_is_ephemeral_frozen_and_captures_prior_field(
        self, sd03b_memory_jobs, prior,
    ):
        from copy import deepcopy
        from dataclasses import FrozenInstanceError, is_dataclass

        store = sd03b_memory_jobs
        if prior == "absent":
            store.jobs[0].pop("next_run_at")
        elif prior == "null":
            store.jobs[0]["next_run_at"] = None
        before = deepcopy(store.jobs[0])
        claimed, receipt = _sd03b_claim(store)
        receipt_type = getattr(store.subject, "UnstartedFireClaimReceipt", None)
        assert receipt_type is not None and isinstance(receipt, receipt_type)
        assert is_dataclass(receipt)
        assert receipt.job_id == "inert-job"
        assert receipt.owner == claimed["fire_claim"]["by"]
        assert receipt.prior_next_present is (prior != "absent")
        assert receipt.prior_next_value == before.get("next_run_at")
        assert receipt.claimed_next_present is True
        assert receipt.claimed_next_value == claimed["next_run_at"]
        assert receipt.claimed_schedule_present is True
        assert receipt.claimed_schedule_value == before["schedule"]
        with pytest.raises(FrozenInstanceError):
            receipt.owner = "cannot-rewrite"
        assert set(store.jobs[0]) == set(before) | {"fire_claim", "next_run_at"}
        assert claimed == store.jobs[0]
        claimed["schedule"]["seconds"] = 999
        assert receipt.claimed_schedule_value["seconds"] == 300
        assert store.jobs[0]["schedule"]["seconds"] == 300
        names = [entry[0] for entry in store.trace]
        assert names.index("fire_enter") < names.index("jobs_enter")
        assert names.index("jobs_enter") < names.index("load")
        assert names.index("save") < names.index("jobs_exit")
        assert names.index("jobs_exit") < names.index("fire_exit")

    def test_private_opt_in_returns_snapshot_and_receipt(self, sd03b_memory_jobs):
        from dataclasses import is_dataclass

        store = sd03b_memory_jobs
        _sd03b_api(store, "claim_job_for_fire_with_unstarted_receipt")
        claimed, receipt = store.subject._claim_job_for_fire_locked(
            "inert-job", return_job=True, return_unstarted_receipt=True,
        )
        assert isinstance(claimed, dict) and is_dataclass(receipt)
        assert receipt.owner == claimed["fire_claim"]["by"]
        assert set(store.jobs[0]) == set(claimed)
        assert "unstarted_receipt" not in claimed

    @pytest.mark.parametrize("prior", ["absent", "null", "value"])
    def test_matching_release_restores_only_claim_and_next(
        self, sd03b_memory_jobs, prior,
    ):
        from copy import deepcopy

        store = sd03b_memory_jobs
        if prior == "absent":
            store.jobs[0].pop("next_run_at")
        elif prior == "null":
            store.jobs[0]["next_run_at"] = None
        before = deepcopy(store.jobs[0])
        _, receipt = _sd03b_claim(store)
        store.trace.clear()
        result = _sd03b_release(store, receipt)
        assert result["released"] is True
        assert result["status"] == "released"
        assert store.jobs[0] == before
        assert [entry[0] for entry in store.trace] == [
            "fire_enter", "jobs_enter", "load", "save", "jobs_exit", "fire_exit",
        ]
        # An old receipt cannot release a later acquisition or double-release.
        store.trace.clear()
        assert _sd03b_release(store, receipt)["released"] is False
        assert store.jobs[0] == before
        assert not any(entry[0] == "save" for entry in store.trace)

    @pytest.mark.parametrize("edit", [
        "owner", "next_value", "next_absent", "next_null",
        "schedule", "schedule_absent", "deleted",
    ])
    def test_release_conflict_preserves_current_job(self, sd03b_memory_jobs, edit):
        from copy import deepcopy

        store = sd03b_memory_jobs
        _, receipt = _sd03b_claim(store)
        if edit == "owner":
            store.jobs[0]["fire_claim"]["by"] = "replacement-owner"
        elif edit == "next_value":
            store.jobs[0]["next_run_at"] = "operator-edited-next"
        elif edit == "next_absent":
            store.jobs[0].pop("next_run_at")
        elif edit == "next_null":
            store.jobs[0]["next_run_at"] = None
        elif edit == "schedule":
            store.jobs[0]["schedule"]["seconds"] = 600
        elif edit == "schedule_absent":
            store.jobs[0].pop("schedule")
        else:
            store.jobs.clear()
        current = deepcopy(store.jobs)
        store.trace.clear()
        result = _sd03b_release(store, receipt)
        assert result["released"] is False
        assert result["status"] != "released"
        assert store.jobs == current
        assert not any(entry[0] == "save" for entry in store.trace)

    def test_release_preserves_pause_and_unrelated_edits(self, sd03b_memory_jobs):
        from copy import deepcopy

        store = sd03b_memory_jobs
        old_next = store.jobs[0]["next_run_at"]
        _, receipt = _sd03b_claim(store)
        store.jobs[0].update({
            "enabled": False, "state": "paused", "paused_at": "operator-pause",
            "paused_reason": "operator", "name": "edited-name",
            "prompt": "edited-prompt", "last_status": "edited-status",
            "repeat": {"times": 7, "completed": 2},
        })
        expected = deepcopy(store.jobs[0])
        expected.pop("fire_claim")
        expected["next_run_at"] = old_next
        assert _sd03b_release(store, receipt)["released"] is True
        assert store.jobs[0] == expected

    def test_claim_and_release_lock_refusal_are_inert(self, sd03b_memory_jobs):
        from copy import deepcopy

        store = sd03b_memory_jobs
        wrapper = _sd03b_api(store, "claim_job_for_fire_with_unstarted_receipt")
        before = deepcopy(store.jobs)
        store.fire_lock_allowed = False
        assert wrapper("inert-job") is None
        assert store.jobs == before
        assert [entry[0] for entry in store.trace] == ["fire_enter", "fire_exit"]
        store.fire_lock_allowed = True
        _, receipt = _sd03b_claim(store)
        current = deepcopy(store.jobs)
        store.trace.clear()
        store.fire_lock_allowed = False
        result = _sd03b_release(store, receipt)
        assert result["released"] is False
        assert store.jobs == current
        assert [entry[0] for entry in store.trace] == ["fire_enter", "fire_exit"]

    @pytest.mark.parametrize("failure", ["before", "after"])
    def test_release_save_failure_reports_uncertainty(self, sd03b_memory_jobs, failure):
        from copy import deepcopy

        store = sd03b_memory_jobs
        original = deepcopy(store.jobs[0])
        _, receipt = _sd03b_claim(store)
        claimed = deepcopy(store.jobs[0])
        store.trace.clear()
        store.save_failure = failure
        result = _sd03b_release(store, receipt)
        assert result["released"] is False
        assert result.get("claim_release") == "uncertain"
        assert result.get("error")
        assert store.jobs[0] == (claimed if failure == "before" else original)
        assert [entry[0] for entry in store.trace] == [
            "fire_enter", "jobs_enter", "load", "save", "jobs_exit", "fire_exit",
        ]

    def test_missing_paused_and_terminal_opt_in_claims_refuse(self, sd03b_memory_jobs):
        from copy import deepcopy

        store = sd03b_memory_jobs
        wrapper = _sd03b_api(store, "claim_job_for_fire_with_unstarted_receipt")
        assert wrapper("missing") is None
        for state in ("paused", "completed", "error"):
            store.jobs[0]["state"] = state
            before = deepcopy(store.jobs)
            assert wrapper("inert-job") is None
            assert store.jobs == before
        assert store.saves == 0
