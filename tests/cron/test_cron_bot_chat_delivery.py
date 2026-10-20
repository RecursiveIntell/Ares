"""Bot Chat cron delivery: deliver='bot-chat[:<profile>]' injects job output
into a local profile's canonical Bot Chat session as a real inbound turn.

Covers token parsing, target resolution (own profile / named / missing),
preflight exemption, create-time validation, the subprocess delivery lane,
and the delivery-targets listing used by UI pickers.
"""

import subprocess
from unittest import mock

import pytest

from cron import scheduler as sched
from cron.scheduler import (
    BOT_CHAT_PLATFORM,
    _deliver_to_bot_chat,
    _preflight_check_delivery,
    _resolve_bot_chat_target,
    _resolve_delivery_targets,
    parse_bot_chat_deliver_token,
)


# ── token parsing ────────────────────────────────────────────────────────────

def test_bare_token_targets_own_profile():
    assert parse_bot_chat_deliver_token("bot-chat") == ""
    assert parse_bot_chat_deliver_token("  Bot-Chat  ") == ""


def test_named_token_returns_profile():
    assert parse_bot_chat_deliver_token("bot-chat:research") == "research"
    assert parse_bot_chat_deliver_token("BOT-CHAT:Research") == "Research"


def test_non_bot_chat_tokens_pass_through():
    assert parse_bot_chat_deliver_token("telegram:-100:17") is None
    assert parse_bot_chat_deliver_token("origin") is None
    assert parse_bot_chat_deliver_token("local") is None
    assert parse_bot_chat_deliver_token("all") is None
    # A platform whose name merely CONTAINS bot-chat must not match.
    assert parse_bot_chat_deliver_token("bot-chatter") is None


# ── target resolution ────────────────────────────────────────────────────────

def test_own_profile_resolves_without_name():
    target = _resolve_bot_chat_target({"id": "j1"}, "")
    assert target == {"platform": BOT_CHAT_PLATFORM, "chat_id": "", "thread_id": None}


def test_named_profile_resolves_when_exists():
    with mock.patch("hermes_cli.profiles.profile_exists", return_value=True):
        target = _resolve_bot_chat_target({"id": "j1"}, "research")
    assert target is not None
    assert target["platform"] == BOT_CHAT_PLATFORM
    assert target["chat_id"] == "research"


def test_unknown_profile_resolves_to_none():
    with mock.patch("hermes_cli.profiles.profile_exists", return_value=False):
        assert _resolve_bot_chat_target({"id": "j1"}, "ghost") is None


def test_resolve_delivery_targets_combines_with_platform_targets():
    """bot-chat rides the same comma-separated deliver string as platforms."""
    job = {"id": "j1", "deliver": "bot-chat,telegram"}
    with mock.patch.object(sched, "_get_home_target_chat_id", return_value="-100123"), \
         mock.patch.object(sched, "_get_home_target_thread_id", return_value=None), \
         mock.patch.object(sched, "_is_known_delivery_platform", return_value=True), \
         mock.patch.object(sched, "_resolve_origin", return_value=None):
        targets = _resolve_delivery_targets(job)
    platforms = {t["platform"] for t in targets}
    assert BOT_CHAT_PLATFORM in platforms
    assert "telegram" in platforms


# ── preflight ────────────────────────────────────────────────────────────────

def test_preflight_ignores_bot_chat_targets():
    """bot-chat needs no gateway credentials — preflight must not block it."""
    assert _preflight_check_delivery({"id": "j1", "deliver": "bot-chat"}) is None
    assert _preflight_check_delivery({"id": "j1", "deliver": "bot-chat:research"}) is None


def test_preflight_still_blocks_unknown_platforms():
    with mock.patch.object(sched, "_is_known_delivery_platform", return_value=False):
        err = _preflight_check_delivery({"id": "j1", "deliver": "nonexistent-platform"})
    assert err is not None and "not a known" in err


# ── create-time validation ───────────────────────────────────────────────────

def test_create_validation_rejects_unknown_profile():
    from tools.cronjob_tools import _validate_bot_chat_deliver

    with mock.patch("hermes_cli.profiles.profile_exists", return_value=False):
        err = _validate_bot_chat_deliver("bot-chat:ghost")
    assert err is not None
    assert "machine-local" in err


def test_create_validation_accepts_bare_and_existing():
    from tools.cronjob_tools import _validate_bot_chat_deliver

    assert _validate_bot_chat_deliver("bot-chat") is None
    assert _validate_bot_chat_deliver(None) is None
    assert _validate_bot_chat_deliver("telegram:-100") is None
    with mock.patch("hermes_cli.profiles.profile_exists", return_value=True):
        assert _validate_bot_chat_deliver("bot-chat:research") is None


# ── delivery lane ────────────────────────────────────────────────────────────

def _completed(returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def test_deliver_runs_canonical_bot_chat_lane():
    """The subprocess must use the Bot Mode agent-to-agent chat lane:
    chat --in ~ -c "Bot Chat" --create-if-missing -Q --query-file <tmp>."""
    calls = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return _completed()

    with mock.patch.object(sched.subprocess, "run", side_effect=fake_run), \
         mock.patch.object(sched.shutil, "which", return_value="/usr/bin/hermes"):
        err = _deliver_to_bot_chat({"id": "j1", "name": "Daily digest"}, "the output", "")

    assert err is None
    argv = calls["argv"]
    assert argv[0] == "/usr/bin/hermes"
    assert "-p" not in argv  # own profile: subprocess inherits HERMES_HOME
    assert "chat" in argv
    assert "Bot Chat" in argv
    assert "--create-if-missing" in argv
    assert "-Q" in argv
    assert "--query-file" in argv
    # Message rides a temp file, never inline argv (quote/expansion safety).
    assert not any("the output" in str(a) for a in argv)


@pytest.fixture
def named_delivery_profiles(tmp_path, monkeypatch):
    from hermes_cli.env_loader import _record_external_secret_snapshot

    root = tmp_path / "root"
    source = root / "profiles" / "source"
    target = root / "profiles" / "research"
    source.mkdir(parents=True)
    target.mkdir()
    (root / ".env").write_text("ROOT_ONLY_TOKEN=synthetic-root\n")
    (source / ".env").write_text(
        "OPENAI_API_KEY=synthetic-source\n"
        "GROQ_API_KEY=synthetic-source-only\n"
        "SOURCE_CUSTOM=synthetic-custom\n"
    )
    (target / ".env").write_text("OPENAI_API_KEY=synthetic-target\n")
    for home in (source, target):
        (home / "config.yaml").write_text(
            "security:\n  inherit_root_credentials: "
            + ("true" if home == source else "false") + "\n"
        )
    _record_external_secret_snapshot(
        source, data={"SOURCE_EXTERNAL_TOKEN": "synthetic-source-external"}, status="ready"
    )
    _record_external_secret_snapshot(
        target, data={"ANTHROPIC_API_KEY": "synthetic-target-only"}, status="ready"
    )
    monkeypatch.setenv("HERMES_HOME", str(source))
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-source")
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-source-only")
    monkeypatch.setenv("SOURCE_CUSTOM", "synthetic-custom")
    monkeypatch.setenv("SOURCE_EXTERNAL_TOKEN", "synthetic-source-external")
    monkeypatch.setenv("ROOT_ONLY_TOKEN", "synthetic-root")
    monkeypatch.setenv("SHELL_CONTROL", "synthetic-unowned")
    return source, target


def test_deliver_named_profile_uses_p_flag_and_target_authority(named_delivery_profiles):
    _, target = named_delivery_profiles
    calls = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return _completed()

    with mock.patch.object(sched.subprocess, "run", side_effect=fake_run), \
         mock.patch.object(sched.shutil, "which", return_value="/usr/bin/hermes"):
        err = _deliver_to_bot_chat({"id": "j1", "name": "n"}, "out", "research")

    assert err is None
    argv = calls["argv"]
    assert argv[1:3] == ["-p", "research"]
    env = calls["kwargs"]["env"]
    assert env["HERMES_HOME"] == str(target)
    assert env["OPENAI_API_KEY"] == "synthetic-target"
    assert env["ANTHROPIC_API_KEY"] == "synthetic-target-only"
    for name in ("GROQ_API_KEY", "SOURCE_CUSTOM", "SOURCE_EXTERNAL_TOKEN", "ROOT_ONLY_TOKEN"):
        assert name not in env
    assert env["SHELL_CONTROL"] == "synthetic-unowned"


def test_deliver_named_profile_missing_authority_starts_no_child(tmp_path, monkeypatch):
    source = tmp_path / "root" / "profiles" / "source"
    source.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(source))
    with mock.patch.object(sched.subprocess, "run") as child, \
         mock.patch.object(sched.shutil, "which", return_value="/usr/bin/hermes"):
        err = _deliver_to_bot_chat({"id": "j1"}, "out", "research")
    assert err == "bot-chat delivery failed: profile authority unavailable"
    child.assert_not_called()


def test_deliver_named_profile_failed_snapshot_starts_no_child(named_delivery_profiles):
    from hermes_cli.env_loader import _record_external_secret_snapshot

    _, target = named_delivery_profiles
    _record_external_secret_snapshot(target, data={}, status="failed", error_kind="synthetic")
    with mock.patch.object(sched.subprocess, "run") as child, \
         mock.patch.object(sched.shutil, "which", return_value="/usr/bin/hermes"):
        err = _deliver_to_bot_chat({"id": "j1"}, "out", "research")
    assert err == "bot-chat delivery failed: profile authority unavailable"
    child.assert_not_called()


def test_deliver_failure_returns_error_string():
    with mock.patch.object(
        sched.subprocess, "run", return_value=_completed(returncode=1, stderr="boom")
    ), mock.patch.object(sched.shutil, "which", return_value="/usr/bin/hermes"):
        err = _deliver_to_bot_chat({"id": "j1", "name": "n"}, "out", "")
    assert err is not None
    assert "boom" in err


def test_deliver_timeout_returns_error_string():
    with mock.patch.object(
        sched.subprocess, "run",
        side_effect=subprocess.TimeoutExpired(cmd="hermes", timeout=600),
    ), mock.patch.object(sched.shutil, "which", return_value="/usr/bin/hermes"):
        err = _deliver_to_bot_chat({"id": "j1", "name": "n"}, "out", "")
    assert err is not None
    assert "timed out" in err


def test_deliver_message_carries_cron_attribution(tmp_path):
    """The injected turn must self-identify as scheduled output, not the user."""
    captured = {}

    def fake_run(argv, **kwargs):
        qf = argv[argv.index("--query-file") + 1]
        with open(qf, encoding="utf-8") as fh:
            captured["message"] = fh.read()
        return _completed()

    with mock.patch.object(sched.subprocess, "run", side_effect=fake_run), \
         mock.patch.object(sched.shutil, "which", return_value="/usr/bin/hermes"):
        _deliver_to_bot_chat({"id": "j1", "name": "Daily digest"}, "the payload", "")

    assert 'Cronjob "Daily digest" output' in captured["message"]
    assert "not the user" in captured["message"]
    assert "the payload" in captured["message"]


# ── delivery-targets listing (UI pickers) ────────────────────────────────────

def test_delivery_targets_include_local_profiles():
    with mock.patch("hermes_cli.profiles.list_profile_names",
                    return_value=["default", "research"]):
        targets = sched.cron_delivery_targets()
    ids = [t["id"] for t in targets]
    assert f"{BOT_CHAT_PLATFORM}:default" in ids
    assert f"{BOT_CHAT_PLATFORM}:research" in ids
    bot_chat_entries = [t for t in targets if t["id"].startswith(BOT_CHAT_PLATFORM)]
    # No gateway home channel needed for bot-chat targets.
    assert all(t["home_target_set"] for t in bot_chat_entries)
