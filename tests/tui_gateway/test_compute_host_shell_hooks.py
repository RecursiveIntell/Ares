"""Test config shell-hook registration in the Desktop compute-host startup path.

With ``dashboard.turn_isolation`` enabled, Desktop turns run in this child.
Without explicit registration, this startup path does not load config shell
hooks. The witnesses below cover startup, consent, safe mode, and dispatch.

Managers are cached per profile home. A retained callback must nevertheless
check its registration home before executing in a different turn's context.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent import shell_hooks
from hermes_cli import plugins as plugins_mod
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture(autouse=True)
def _clean_registration(monkeypatch):
    shell_hooks.reset_for_tests()
    manager = plugins_mod.PluginManager()
    monkeypatch.setattr(plugins_mod, "_plugin_manager", manager, raising=False)
    monkeypatch.setattr(plugins_mod, "get_plugin_manager", lambda: manager)
    yield manager
    shell_hooks.reset_for_tests()


def _hook_script(tmp_path: Path) -> Path:
    script = tmp_path / "ctx_hook.py"
    script.write_text(
        "import json, sys\n"
        "json.load(sys.stdin)\n"
        "print(json.dumps({'context': 'HOOK-FIRED'}))\n"
    )
    return script


def _root_with_hook(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    root = tmp_path / "root_home"
    root.mkdir()
    command = f"{sys.executable} {_hook_script(tmp_path)}"
    monkeypatch.setenv("HERMES_HOME", str(root))
    (root / "config.yaml").write_text(json.dumps({
        "hooks": {"pre_llm_call": [{"command": command, "timeout": 10}]},
        "hooks_auto_accept": True,
    }))
    return root, command


def _fire(manager) -> list:
    return [cb(user_message="hello world memory question") for cb in manager._hooks.get("pre_llm_call", [])]


def test_compute_host_startup_registers_configured_shell_hooks(tmp_path, monkeypatch, _clean_registration):
    _root_with_hook(tmp_path, monkeypatch)
    from tui_gateway import compute_host

    compute_host._register_config_shell_hooks()

    results = _fire(_clean_registration)
    assert results == [{"context": "HOOK-FIRED"}]


def test_registration_failure_never_blocks_host_startup(tmp_path, monkeypatch, caplog):
    from tui_gateway import compute_host

    def _boom(*_a, **_k):
        raise RuntimeError("config unreadable")

    monkeypatch.setattr("hermes_cli.config.load_config", _boom)
    compute_host._register_config_shell_hooks()  # must not raise
    assert "shell-hook registration failed" in caplog.text


def test_hook_registered_from_root_does_not_fire_for_other_profile_turn(tmp_path, monkeypatch, _clean_registration):
    _root_with_hook(tmp_path, monkeypatch)
    from tui_gateway import compute_host

    compute_host._register_config_shell_hooks()
    profile = tmp_path / "root_home" / "profiles" / "other"
    profile.mkdir(parents=True)

    token = set_hermes_home_override(str(profile))
    try:
        results = _fire(_clean_registration)
    finally:
        reset_hermes_home_override(token)
    assert results == [None]

    # Same process, root-profile turn: fires.
    assert _fire(_clean_registration) == [{"context": "HOOK-FIRED"}]


def test_cli_registration_keeps_firing_without_override(tmp_path, monkeypatch, _clean_registration):
    _, command = _root_with_hook(tmp_path, monkeypatch)
    from hermes_cli.config import load_config

    shell_hooks.register_from_config(load_config(), accept_hooks=True)
    assert _fire(_clean_registration) == [{"context": "HOOK-FIRED"}]


@pytest.mark.parametrize(
    ("consent", "safe_mode", "expected_executions"),
    [(True, False, 2), (False, False, 0), (True, True, 0)],
    ids=["accepted", "unapproved-non-tty", "safe-mode"],
)
def test_real_host_startup_and_dispatch_respect_consent_and_home(
    tmp_path, consent, safe_mode, expected_executions,
):
    """Exercise run_host and the real manager dispatcher in a clean child.

    Retaining the root manager across the home override deliberately tests its
    callbacks, rather than trivially querying a different home's empty manager.
    The dispatcher also exercises its own ContextVar propagation to hook threads.
    """
    import os
    import shlex
    import subprocess
    import textwrap

    home = tmp_path / "home"
    root = tmp_path / "root"
    other = root / "profiles" / "other"
    home.mkdir()
    other.mkdir(parents=True)
    marker = tmp_path / "executions.jsonl"
    hook = tmp_path / "harmless_hook.py"
    hook.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "payload = json.load(sys.stdin)\n"
        f"with Path({str(marker)!r}).open('a') as stream:\n"
        "    stream.write(json.dumps(payload['hook_event_name']) + '\\n')\n"
        "print(json.dumps({'context': 'REAL-HOST-HOOK'}))\n"
    )
    cfg = {"hooks": {"pre_llm_call": [{
        "command": shlex.join([sys.executable, str(hook)]), "timeout": 10,
    }]}}
    if consent:
        cfg["hooks_auto_accept"] = True
    (root / "config.yaml").write_text(json.dumps(cfg))
    repo = Path(__file__).resolve().parents[2]
    driver = textwrap.dedent("""
        import io, json, sys
        sys.path.insert(0, sys.argv[1])
        from tui_gateway import compute_host
        from hermes_cli.plugins import get_plugin_manager
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override

        wire = io.StringIO()
        compute_host.run_host(stdin=io.StringIO(''), stdout=wire)
        manager = get_plugin_manager()
        registered = len(manager._hooks.get('pre_llm_call', []))
        # Existing registration must remain idempotent.
        compute_host._register_config_shell_hooks()
        same = manager.invoke_hook('pre_llm_call', user_message='root witness')
        token = set_hermes_home_override(sys.argv[2])
        try:
            foreign = manager.invoke_hook('pre_llm_call', user_message='foreign witness')
        finally:
            reset_hermes_home_override(token)
        restored = manager.invoke_hook('pre_llm_call', user_message='restored witness')
        from pathlib import Path
        Path(sys.argv[3]).write_text(json.dumps({
            'registered': registered,
            'after_repeat': len(manager._hooks.get('pre_llm_call', [])),
            'same': same, 'foreign': foreign, 'restored': restored,
            'wire': [json.loads(line) for line in wire.getvalue().splitlines()],
        }))
    """)
    env = {
        "HOME": str(home), "HERMES_HOME": str(root),
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.defpath]),
        "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
        "HERMES_COMPUTE_HOST_HEARTBEAT_SECS": "0",
    }
    if safe_mode:
        env["HERMES_SAFE_MODE"] = "1"
    result_path = tmp_path / "host-result.json"
    result = subprocess.run(
        [sys.executable, "-c", driver, str(repo), str(other), str(result_path)],
        cwd=repo, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result_path.exists(), (result.stdout, result.stderr)
    observed = json.loads(result_path.read_text())
    expected_registered = 1 if expected_executions else 0
    expected_context = [{"context": "REAL-HOST-HOOK"}] if expected_executions else []
    assert observed["registered"] == expected_registered
    assert observed["after_repeat"] == expected_registered
    assert observed["same"] == expected_context
    assert observed["foreign"] == []
    assert observed["restored"] == expected_context
    assert any(frame["type"] == "hello" for frame in observed["wire"])
    executions = marker.read_text().splitlines() if marker.exists() else []
    assert executions == [json.dumps("pre_llm_call")] * expected_executions
    if not consent or safe_mode:
        assert not (root / "shell-hooks-allowlist.json").exists()
