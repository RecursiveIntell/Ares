"""Tests for the default-off OFFLINE tool-observation canary staging bridge.

Unit tests use real tiny shell-script "probes" (real process-group/timeout
helper, no mocks of the helper) plus one mocked-helper test for exact error
mapping.  The parent runs the real Rust binary separately.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import textwrap
from pathlib import Path

import pytest

import plugins.tool_observation_canary as canary
from ares_runtime.legacy_transition import LegacyTransitionError

# Real child processes are spawned in their own session and SIGKILLed by
# ``bounded_probe`` (killpg of that child's group only); the conftest guard
# otherwise blocks killpg outside the test process group.
pytestmark = [
    pytest.mark.live_system_guard_bypass,
    pytest.mark.skipif(os.name != "posix", reason="canary child probes require POSIX"),
]

GOOD = {
    "backend_payload": "withheld", "canary": "offline_disposable",
    "embedder": "mock_offline_only", "executed": True,
    "forge_readback": {"found": True, "scope_matches": True, "status": "succeeded"},
    "namespace": "ares-tool-observation-offline-canary",
    "projection_readback": {"found": True, "status": "succeeded"},
    "receipt_id": "3e86cdf9-3a4e-47f7-af93-585ce29832f3", "reopened": True,
    "retries": 0, "status": "succeeded",
    "tool_name": "ares_offline_available_parallelism",
}


class FakeCtx:
    def __init__(self, settings):
        self.settings = settings
        self.tools = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home.resolve()


@pytest.fixture
def state_dir(home):
    path = home / "pilot"
    path.mkdir(mode=0o700)
    return path


def make_exe(tmp_path: Path, body: str, name="probe") -> Path:
    bindir = tmp_path / "stage"
    bindir.mkdir(mode=0o755, exist_ok=True)
    path = bindir / name
    path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    path.chmod(0o755)
    return path.resolve()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def settings(exe: Path, state: Path, **over):
    value = {"enabled": True, "executable": str(exe), "sha256": sha(exe),
             "state_dir": str(state)}
    value.update(over)
    return value


def build(settings_value):
    ctx = FakeCtx(settings_value)
    canary.register(ctx)
    tool = ctx.tools[canary.TOOL_NAME]
    return ctx, tool, tool["check_fn"], tool["handler"]


def test_missing_posix_primitives_refuse_before_launch(tmp_path, state_dir, monkeypatch):
    witness = tmp_path / "launched"
    exe = make_exe(tmp_path, f"touch {witness}\n")
    _, _, check_fn, handler = build(settings(exe, state_dir))
    monkeypatch.delattr(os, "geteuid")
    assert check_fn() is False
    result = run(handler)
    assert result["reason"] == "unsupported_platform"
    assert result["executed"] is False
    assert not witness.exists() and list(state_dir.iterdir()) == []


def run(handler, args=None):
    return json.loads(handler({} if args is None else args))


def echo_script(payload: dict) -> str:
    return f"printf '%s\\n' '{json.dumps(payload)}'\n"


# ---------------------------------------------------------------- schema/reg

def test_schema_is_zero_arg_and_registered(tmp_path, state_dir):
    exe = make_exe(tmp_path, "exit 0\n")
    ctx, tool, check_fn, _ = build(settings(exe, state_dir))
    params = tool["schema"]["parameters"]
    assert params["properties"] == {} and params["additionalProperties"] is False
    assert tool["toolset"] == "tool_observation_canary"
    assert check_fn() is True
    assert getattr(check_fn, "__name__", "") == "check_fn"


def test_manifest_is_default_off_bundled_standalone():
    import yaml
    manifest = yaml.safe_load(
        (Path(canary.__file__).parent / "plugin.yaml").read_text())
    assert manifest["kind"] == "standalone"
    assert manifest["provides_tools"] == [canary.TOOL_NAME]


def test_not_loaded_without_operator_opt_in(home):
    from hermes_cli import plugins as pmod
    mgr = pmod.PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins["tool_observation_canary"]
    assert not loaded.enabled


# -------------------------------------------------------------- disabled etc.

@pytest.mark.parametrize("override", [
    {"enabled": False}, {"enabled": "true"}, {"enabled": None},
])
def test_disabled_fails_closed(tmp_path, state_dir, override):
    exe = make_exe(tmp_path, echo_script(GOOD))
    _, _, check_fn, handler = build(settings(exe, state_dir, **override))
    assert check_fn() is False
    result = run(handler)
    assert result["outcome"] == "refused" and result["reason"] == "disabled"
    assert result["executed"] is False
    assert list(state_dir.iterdir()) == []


def test_no_settings_at_all_is_disabled(state_dir):
    _, _, check_fn, handler = build({})
    assert check_fn() is False
    assert run(handler)["reason"] == "disabled"


@pytest.mark.parametrize("field,value", [
    ("executable", "probe"), ("executable", "relative/probe"),
    ("executable", "/tmp/../tmp/probe"), ("executable", ""),
    ("sha256", "ABC"), ("sha256", "G" * 64), ("sha256", None),
    ("state_dir", "pilot"),
])
def test_invalid_admission_syntax(tmp_path, state_dir, field, value):
    exe = make_exe(tmp_path, echo_script(GOOD))
    _, _, check_fn, handler = build(settings(exe, state_dir, **{field: value}))
    assert check_fn() is False
    assert run(handler)["reason"] == "invalid_admission"


def test_missing_artifact(tmp_path, state_dir):
    missing = tmp_path / "stage" / "nope"
    cfg = {"enabled": True, "executable": str(missing), "sha256": "a" * 64,
           "state_dir": str(state_dir)}
    _, _, check_fn, handler = build(cfg)
    assert check_fn() is False
    result = run(handler)
    assert result["reason"] == "executable_missing" and result["executed"] is False


def test_no_path_fallback(tmp_path, state_dir, monkeypatch):
    exe = make_exe(tmp_path, echo_script(GOOD), name="ares-tool-observation-probe")
    monkeypatch.setenv("PATH", str(exe.parent))
    cfg = {"enabled": True, "executable": "ares-tool-observation-probe",
           "sha256": sha(exe), "state_dir": str(state_dir)}
    _, _, _, handler = build(cfg)
    assert run(handler)["reason"] == "invalid_admission"
    assert list(state_dir.iterdir()) == []


def test_symlink_executable_refused(tmp_path, state_dir):
    exe = make_exe(tmp_path, echo_script(GOOD))
    link = exe.parent / "link"
    link.symlink_to(exe)
    _, _, _, handler = build(settings(exe, state_dir, executable=str(link)))
    assert run(handler)["reason"] == "executable_unsafe"


def test_group_writable_executable_refused(tmp_path, state_dir):
    exe = make_exe(tmp_path, echo_script(GOOD))
    exe.chmod(0o775)
    _, _, check_fn, handler = build(settings(exe, state_dir))
    assert check_fn() is False
    assert run(handler)["reason"] == "executable_unsafe"


def test_non_executable_refused(tmp_path, state_dir):
    exe = make_exe(tmp_path, echo_script(GOOD))
    exe.chmod(0o644)
    _, _, _, handler = build(settings(exe, state_dir))
    assert run(handler)["reason"] == "executable_unsafe"


# ---------------------------------------------------------------- state dir

def test_state_dir_must_be_private_inside_profile(tmp_path, home, state_dir):
    exe = make_exe(tmp_path, echo_script(GOOD))
    state_dir.chmod(0o755)
    _, _, check_fn, handler = build(settings(exe, state_dir))
    assert check_fn() is False
    assert run(handler)["reason"] == "state_dir_invalid"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    _, _, _, handler = build(settings(exe, outside))
    assert run(handler)["reason"] == "state_dir_invalid"
    _, _, _, handler = build(settings(exe, home))
    assert run(handler)["reason"] == "state_dir_invalid"


def test_state_dir_symlink_refused(tmp_path, home, state_dir):
    exe = make_exe(tmp_path, echo_script(GOOD))
    link = home / "linked"
    link.symlink_to(state_dir)
    _, _, _, handler = build(settings(exe, link))
    assert run(handler)["reason"] == "state_dir_invalid"


def test_state_dir_must_be_empty_for_first_attempt(tmp_path, state_dir):
    exe = make_exe(tmp_path, echo_script(GOOD))
    (state_dir / "stray").write_text("x")
    _, _, _, handler = build(settings(exe, state_dir))
    assert run(handler)["reason"] == "state_dir_not_empty"


# --------------------------------------------------------------------- hash

def test_bad_hash_never_launches(tmp_path, state_dir):
    witness = tmp_path / "launched"
    exe = make_exe(tmp_path, f"touch {witness}\n" + echo_script(GOOD))
    _, _, check_fn, handler = build(settings(exe, state_dir, sha256="0" * 64))
    assert check_fn() is True  # cheap gate does not hash; dispatch recheck does
    result = run(handler)
    assert result["reason"] == "hash_mismatch" and result["executed"] is False
    assert not witness.exists() and list(state_dir.iterdir()) == []


def test_artifact_swapped_after_admission_is_refused(tmp_path, state_dir):
    exe = make_exe(tmp_path, echo_script(GOOD))
    _, _, _, handler = build(settings(exe, state_dir))
    exe.write_text("#!/bin/sh\necho swapped\n")
    exe.chmod(0o755)
    assert run(handler)["reason"] == "hash_mismatch"
    assert list(state_dir.iterdir()) == []


def test_config_hot_override_is_refused(tmp_path, state_dir):
    exe = make_exe(tmp_path, echo_script(GOOD))
    other = make_exe(tmp_path, echo_script(GOOD) + "# other\n", name="other")
    ctx, _, check_fn, handler = build(settings(exe, state_dir))
    ctx.settings.update(executable=str(other), sha256=sha(other))
    assert check_fn() is False
    assert run(handler)["reason"] == "admission_changed"
    ctx.settings["enabled"] = False
    assert run(handler)["reason"] == "disabled"
    assert list(state_dir.iterdir()) == []


# ------------------------------------------------------------------ success

def test_success_returns_validated_metadata_only(tmp_path, state_dir):
    exe = make_exe(tmp_path, echo_script(GOOD))
    _, _, _, handler = build(settings(exe, state_dir))
    result = run(handler)
    assert result == {
        "outcome": "succeeded", "receipt_id": GOOD["receipt_id"],
        "namespace": GOOD["namespace"], "tool_name": GOOD["tool_name"],
        "forge_readback": "succeeded", "projection_readback": "succeeded",
        "retries": 0, "offline_only": True, "production_claim": False,
    }
    marker = json.loads((state_dir / "attempt.json").read_text())
    assert marker["status"] == "succeeded"
    assert stat.S_IMODE((state_dir / "attempt.json").stat().st_mode) == 0o600


def test_argv_is_plugin_constant_and_env_is_scrubbed(tmp_path, state_dir, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    seen = tmp_path / "seen"
    exe = make_exe(tmp_path, f'echo "$@" > {seen}\nenv >> {seen}\n' + echo_script(GOOD))
    _, _, _, handler = build(settings(exe, state_dir))
    assert run(handler, {})["outcome"] == "succeeded"
    text = seen.read_text()
    assert text.splitlines()[0] == (
        "--enable-offline-canary --scope ares-tool-observation-offline-canary "
        f"--out-dir {state_dir}/run")
    assert "OPENAI_API_KEY" not in text


def test_model_arguments_rejected_without_launch(tmp_path, state_dir):
    witness = tmp_path / "launched"
    exe = make_exe(tmp_path, f"touch {witness}\n" + echo_script(GOOD))
    _, _, _, handler = build(settings(exe, state_dir))
    result = run(handler, {"out_dir": "/etc", "args": ["--x"]})
    assert result["reason"] == "arguments_not_accepted"
    assert not witness.exists() and list(state_dir.iterdir()) == []


def test_second_call_never_relaunches(tmp_path, state_dir):
    counter = tmp_path / "count"
    exe = make_exe(tmp_path, f"echo x >> {counter}\n" + echo_script(GOOD))
    _, _, _, handler = build(settings(exe, state_dir))
    assert run(handler)["outcome"] == "succeeded"
    second = run(handler)
    assert second["outcome"] == "already_attempted" and second["executed"] is False
    assert second["marker_status"] == "succeeded"
    assert len(counter.read_text().splitlines()) == 1


# ---------------------------------------------------- unexpected / malformed

def _variant(**changes):
    value = json.loads(json.dumps(GOOD))
    value.update(changes)
    return value


@pytest.mark.parametrize("payload", [
    _variant(status="readback_mismatch"),
    _variant(extra_field=1),
    _variant(retries=1),
    _variant(retries=True),
    _variant(namespace="other"),
    _variant(tool_name="other"),
    _variant(receipt_id="../../etc/passwd"),
    _variant(receipt_id=5),
    _variant(executed="true"),
    _variant(forge_readback={"found": True, "scope_matches": False, "status": "succeeded"}),
    _variant(projection_readback={"found": True}),
    {k: v for k, v in GOOD.items() if k != "receipt_id"},
])
def test_unexpected_success_record_is_unknown_not_success(tmp_path, state_dir, payload):
    exe = make_exe(tmp_path, echo_script(payload))
    _, _, _, handler = build(settings(exe, state_dir))
    result = run(handler)
    assert result["outcome"] == "unknown"
    assert result["reason"] == "malformed_or_unexpected_output"
    assert "receipt_id" not in result
    assert json.loads((state_dir / "attempt.json").read_text())["status"] == "unknown"


@pytest.mark.parametrize("body", [
    "echo not-json\n",
    "printf '[1,2]\\n'\n",
    "printf '{\"a\":1,\"a\":2}\\n'\n",
    "printf '{\"a\":NaN}\\n'\n",
    "printf '\\xff\\xfe\\n'\n",
    "exit 0\n",
])
def test_malformed_stdout_is_unknown(tmp_path, state_dir, body):
    exe = make_exe(tmp_path, body)
    _, _, _, handler = build(settings(exe, state_dir))
    result = run(handler)
    assert result["outcome"] == "unknown" and result["executed"] is True
    assert result["relaunched"] is False


def test_oversize_output_is_unknown_and_not_echoed(tmp_path, state_dir):
    exe = make_exe(tmp_path, "head -c 100000 /dev/zero | tr '\\0' 'A'\n")
    _, _, _, handler = build(settings(exe, state_dir))
    text = handler({})
    assert json.loads(text)["outcome"] == "unknown"
    assert "AAAA" not in text


def test_nonzero_exit_is_failed_without_raw_output(tmp_path, state_dir):
    exe = make_exe(tmp_path, 'echo "SECRET-DIAGNOSTIC"; echo "SECRET-ERR" >&2; exit 1\n')
    _, _, _, handler = build(settings(exe, state_dir))
    text = handler({})
    result = json.loads(text)
    assert result["outcome"] == "failed" and result["reason"] == "probe_exit_nonzero"
    assert "SECRET" not in text
    assert json.loads((state_dir / "attempt.json").read_text())["status"] == "failed"
    assert json.loads(handler({}))["outcome"] == "already_attempted"


# ------------------------------------------------------------------ timeout

def test_timeout_is_typed_unknown_and_late_success_not_adopted(
        tmp_path, state_dir, monkeypatch):
    monkeypatch.setattr(canary, "_LAUNCH_TIMEOUT_S", 0.5)
    late = tmp_path / "late"
    count = tmp_path / "count"
    # Sleeps past the deadline, then would write a valid success + a witness.
    exe = make_exe(tmp_path, f"echo x >> {count}\nsleep 3\ntouch {late}\n" + echo_script(GOOD))
    _, _, _, handler = build(settings(exe, state_dir))
    first = run(handler)
    assert first["outcome"] == "unknown" and first["reason"] == "launch_timeout"
    assert first["relaunched"] is False and "receipt_id" not in first
    import time
    time.sleep(3.5)
    assert not late.exists()  # process group was killed at the deadline
    second = run(handler)
    assert second["outcome"] == "already_attempted" and second["marker_status"] == "unknown"
    assert len(count.read_text().splitlines()) == 1
    assert json.loads((state_dir / "attempt.json").read_text())["status"] == "unknown"


def test_helper_error_mapping_is_pinned(tmp_path, state_dir, monkeypatch):
    """The helper's exact messages drive timeout-vs-failure typing."""
    exe = make_exe(tmp_path, echo_script(GOOD))
    for message, outcome in (
        ("release import probe timed out", "unknown"),
        ("release import probe failed", "failed"),
        ("release import probe exceeded output limit", "unknown"),
    ):
        sd = state_dir.parent / f"pilot-{outcome}-{abs(hash(message))}"
        sd.mkdir(mode=0o700)

        def boom(*_a, _m=message, **_k):
            raise LegacyTransitionError(_m)

        monkeypatch.setattr(canary, "bounded_probe", boom)
        _, _, _, handler = build(settings(exe, sd))
        assert run(handler)["outcome"] == outcome


def test_launch_oserror_is_unknown_no_retry(tmp_path, state_dir, monkeypatch):
    exe = make_exe(tmp_path, echo_script(GOOD))
    calls = []

    def boom(*_a, **_k):
        calls.append(1)
        raise OSError("exec failed")

    monkeypatch.setattr(canary, "bounded_probe", boom)
    _, _, _, handler = build(settings(exe, state_dir))
    first = run(handler)
    assert first["outcome"] == "unknown" and first["reason"] == "launch_error"
    assert first["executed"] is None  # spawn may not have happened
    second = run(handler)
    assert second["outcome"] == "already_attempted"
    assert second["marker_is_diagnostic_only"] is True
    assert len(calls) == 1


def test_stale_launching_marker_blocks_relaunch(tmp_path, state_dir):
    """A crash between marker and settle leaves 'launching' => unknown, no launch."""
    witness = tmp_path / "launched"
    exe = make_exe(tmp_path, f"touch {witness}\n" + echo_script(GOOD))
    (state_dir / "attempt.json").write_text('{"schema": 1, "status": "launching"}\n')
    _, _, _, handler = build(settings(exe, state_dir))
    result = run(handler)
    assert result["outcome"] == "already_attempted" and result["marker_status"] == "unknown"
    assert not witness.exists()


def test_unexpected_exception_is_typed_error(tmp_path, state_dir, monkeypatch):
    exe = make_exe(tmp_path, echo_script(GOOD))
    _, _, _, handler = build(settings(exe, state_dir))
    monkeypatch.setattr(canary, "run_once", lambda _a: (_ for _ in ()).throw(RuntimeError("x")))
    result = run(handler)
    assert result["outcome"] == "unknown" and result["reason"] == "bridge_error"


def test_registry_dispatch_returns_json_error_string(tmp_path, state_dir):
    """Through the real registry dispatch, failure is a JSON string, not a raise."""
    from tools.registry import ToolRegistry
    exe = make_exe(tmp_path, echo_script(GOOD))
    ctx, tool, _, _ = build(settings(exe, state_dir, enabled=False))
    reg = ToolRegistry()
    reg.register(name=tool["name"], toolset=tool["toolset"], schema=tool["schema"],
                 handler=tool["handler"], check_fn=tool["check_fn"])
    out = json.loads(reg.dispatch(tool["name"], {}))
    assert out["reason"] == "disabled"
    # handler receives dispatch args positionally; extra args are refused
    out = json.loads(reg.dispatch(tool["name"], {"out_dir": "/etc"}))
    assert out["reason"] == "arguments_not_accepted"
    assert reg.get_definitions({tool["name"]}) == []
