"""A model picked mid-turn must still get its selection-guard confirm step.

``config.set model`` on a *running* session cannot swap the agent in place --
the worker thread is reading ``agent.model`` / ``agent.client`` on every
iteration -- so it stashes the pick in ``session["pending_model_switch"]`` and
``_apply_pending_model_switch`` applies it at the next turn start.

That deferral used to skip the selection guards entirely: the stash branch
answered ``confirm_required: False`` without ever calling them. A client that
implements the confirm round-trip was therefore told no consent was needed, so
it never prompted. One turn later ``_apply_pending_model_switch`` ran the
guards with the stashed (unconfirmed) flag, saw the warning, and deliberately
dropped the switch -- correct on its own terms, but by then no round-trip was
possible. The user's pick silently reverted and the confirm was never offered
on this path at all.
"""

import threading
import types

import pytest

from tui_gateway import server


# A vendor-documented data-training tier. The data-policy guard keys on the
# model id alone (no base_url / api_key / model_info), which is exactly what
# the stash branch can see before resolution.
GUARDED_MODEL = "muse-spark-1.2-contributor"
UNGUARDED_MODEL = "anthropic/claude-sonnet-4.6"


def _session(**extra):
    return {
        "agent": types.SimpleNamespace(),
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        **extra,
    }


def _config_set_model(value, **extra_params):
    params = {"session_id": "sid", "key": "model", "value": value}
    params.update(extra_params)

    return server.handle_request({"id": "1", "method": "config.set", "params": params})


@pytest.fixture
def running_session(monkeypatch):
    """A busy session whose live swap path is fatal if it is ever reached."""

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError(
            "_apply_model_switch ran on the busy path -- it would race the "
            "worker thread reading agent.model / agent.client"
        )

    monkeypatch.setattr(server, "_apply_model_switch", _must_not_run)
    server._sessions["sid"] = _session(running=True)
    try:
        yield server._sessions["sid"]
    finally:
        server._sessions.pop("sid", None)


class TestGuardedPickAsksBeforeStashing:
    def test_reports_confirm_required_instead_of_deferring(self, running_session):
        resp = _config_set_model(GUARDED_MODEL)

        assert not resp.get("error")
        result = resp["result"]
        assert result["confirm_required"] is True, (
            "the deferred path answered confirm_required=False without running "
            "the guards, so a correct client never prompts and the pick is "
            "dropped a turn later with no way to consent"
        )
        assert result["confirm_message"].strip()
        assert result["deferred"] is False
        assert "model_control_revision" not in result

    def test_leaves_the_session_untouched(self, running_session):
        _config_set_model(GUARDED_MODEL)

        assert "pending_model_switch" not in running_session, (
            "an unconfirmed guarded pick must not be queued -- the next turn "
            "start would drop it anyway, after the pill already moved"
        )

    def test_confirm_message_names_the_guard(self, running_session):
        message = _config_set_model(GUARDED_MODEL)["result"]["confirm_message"]

        assert "CONTRIBUTOR TIER" in message
        assert "train" in message.lower()

    def test_reconfirming_queues_the_pick(self, running_session):
        resp = _config_set_model(GUARDED_MODEL, confirm_expensive_model=True)

        result = resp["result"]
        assert result["deferred"] is True
        assert result["confirm_required"] is False
        assert isinstance(result["model_control_revision"], int)

        pending = running_session["pending_model_switch"]
        assert pending["raw"] == GUARDED_MODEL
        assert pending["model_control_revision"] == result["model_control_revision"]
        assert pending["confirm_expensive_model"] is True, (
            "the ack must survive into the stash or _apply_pending_model_switch "
            "re-runs the guard at turn start and drops the confirmed pick"
        )


class TestUnguardedPickStillDefers:
    """The queue-don't-race behaviour is the whole point of this branch."""

    def test_defers_without_a_confirm_step(self, running_session):
        result = _config_set_model(UNGUARDED_MODEL)["result"]

        assert result["deferred"] is True
        assert result["confirm_required"] is False
        assert result["confirm_message"] == ""
        assert result["value"] == UNGUARDED_MODEL
        assert isinstance(result["model_control_revision"], int)

    def test_stashes_the_pick_for_the_next_turn(self, running_session):
        _config_set_model(UNGUARDED_MODEL)

        pending = running_session["pending_model_switch"]
        assert pending["raw"] == UNGUARDED_MODEL
        assert pending["confirm_expensive_model"] is False
        assert pending["model_control_revision"] == server._session_info(running_session["agent"], running_session)[
            "model_control_revision"
        ]

    def test_explicit_provider_is_still_recorded_for_display(self, running_session):
        _config_set_model(f"{UNGUARDED_MODEL} --provider anthropic")

        pending = running_session["pending_model_switch"]
        assert pending["display_provider"] == "anthropic"


class TestGuardFailureIsNotFatal:
    def test_a_raising_guard_falls_back_to_deferring(self, running_session, monkeypatch):
        """A broken guard must never cost the user their model pick.

        The apply-time check in ``_apply_pending_model_switch`` is still there,
        so failing open here degrades to the old behaviour rather than to a
        silently unguarded switch.
        """

        def _boom(*_args, **_kwargs):
            raise RuntimeError("guard table is broken")

        monkeypatch.setattr(
            "hermes_cli.model_selection_guards.combined_selection_warning", _boom
        )

        result = _config_set_model(GUARDED_MODEL)["result"]

        assert result["deferred"] is True
        assert running_session["pending_model_switch"]["raw"] == GUARDED_MODEL


@pytest.mark.parametrize("failure", ["error", "confirmation"])
def test_session_model_mutations_serialize_and_never_reuse_failed_revisions(monkeypatch, failure):
    first_entered, release_first, second_entered = (threading.Event() for _ in range(3))
    ready = threading.Event()
    ready.set()
    record = _session(agent=types.SimpleNamespace(model="initial", provider="test"), agent_ready=ready)
    server._sessions["sid"] = record
    responses = {}

    def controlled_apply(_sid, session, value, **_kwargs):
        if value.startswith("first"):
            first_entered.set()
            assert release_first.wait(timeout=3.0)
            if failure == "error":
                raise RuntimeError("first switch refused")
            return {"value": "first", "scope": "session", "confirm_required": True, "warning": "confirm first", "confirm_message": "confirm first"}
        second_entered.set()
        session["agent"].model = "second"
        return {"value": "second", "scope": "session", "confirm_required": False, "warning": "", "confirm_message": ""}

    monkeypatch.setattr(server, "_apply_model_switch", controlled_apply)
    monkeypatch.setattr(server, "_start_agent_build", lambda *_: None)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_: False)
    first = threading.Thread(target=lambda: responses.update(first=_config_set_model("first --session")))
    second = threading.Thread(target=lambda: responses.update(second=_config_set_model("second --session")))
    try:
        first.start()
        assert first_entered.wait(timeout=1.0)
        second.start()
        assert not second_entered.wait(timeout=0.1)
        release_first.set()
        first.join(timeout=2.0)
        second.join(timeout=2.0)
        assert not first.is_alive() and not second.is_alive()
        assert not responses["second"].get("error")
        revision = responses["second"]["result"]["model_control_revision"]
        assert revision >= 2
        assert record["_model_control_revision"] == revision
        assert server._session_info(record["agent"], record)["model_control_revision"] == revision
        assert record["agent"].model == "second"
    finally:
        release_first.set()
        first.join(timeout=2.0)
        if second.ident is not None:
            second.join(timeout=2.0)
        server._sessions.pop("sid", None)


class TestHelperContract:
    def test_returns_none_for_an_empty_model(self):
        assert server._pending_switch_selection_warning("", "") is None

    def test_returns_none_when_no_guard_fires(self):
        assert server._pending_switch_selection_warning(UNGUARDED_MODEL, "") is None

    def test_returns_the_message_when_a_guard_fires(self):
        message = server._pending_switch_selection_warning(GUARDED_MODEL, "")

        assert message is not None
        assert "CONTRIBUTOR TIER" in message

    def test_an_explicit_provider_reaches_the_guards(self, monkeypatch):
        """Provider-keyed guards are useless if the provider is dropped here.

        The docstring promises the early call can only under-fire relative to
        the resolved one, and that only holds if what the caller DID say is
        forwarded. Asserting on a guarded model id would pass even with
        ``provider`` dropped, so record the kwargs instead.
        """
        seen = {}

        def _fake(model, provider=None, **kwargs):
            seen["model"] = model
            seen["provider"] = provider
            return None

        import hermes_cli.model_selection_guards as guards

        monkeypatch.setattr(guards, "combined_selection_warning", _fake)
        server._pending_switch_selection_warning(UNGUARDED_MODEL, "openrouter")

        assert seen == {"model": UNGUARDED_MODEL, "provider": "openrouter"}

    def test_an_empty_provider_is_normalised_to_none(self, monkeypatch):
        """`provider or None` is load-bearing: "" is not "no provider" to a
        guard that does an `is None` check, and the TUI sends "" for unset."""
        seen = {}

        def _fake(model, provider=None, **kwargs):
            seen["provider"] = provider
            return None

        import hermes_cli.model_selection_guards as guards

        monkeypatch.setattr(guards, "combined_selection_warning", _fake)
        server._pending_switch_selection_warning(UNGUARDED_MODEL, "")

        assert seen == {"provider": None}
