from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "run_panel.py"
VERIFY_SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_receipt.py"
TELEMETRY_SCRIPT = Path(__file__).parents[1] / "scripts" / "summarize_receipts.py"
spec = importlib.util.spec_from_file_location("run_panel", SCRIPT)
assert spec and spec.loader
run_panel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_panel)
telemetry_spec = importlib.util.spec_from_file_location("summarize_receipts", TELEMETRY_SCRIPT)
assert telemetry_spec and telemetry_spec.loader
summarize_receipts = importlib.util.module_from_spec(telemetry_spec)
telemetry_spec.loader.exec_module(summarize_receipts)


def make_runtime(tmp_path: Path, program: str) -> Path:
    runtime = tmp_path / "runtime"
    python = runtime / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    indented = "\n".join(f"    {line}" for line in program.splitlines())
    python.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "if args and args[0] == '-P':\n"
        "    args = args[1:]\n"
        "if len(args) >= 2 and args[0] == '-c':\n"
        "    exec(args[1])\n"
        "else:\n"
        f"{indented}\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    return runtime


def invoke(runtime: Path, out: Path, *extra: str, timeout: float = 10) -> subprocess.CompletedProcess[str]:
    routing = () if any(item in extra for item in ("--profiles", "--full-panel")) else ("--full-panel",)
    home = runtime.parent / "home"
    home.mkdir(exist_ok=True)
    for profile in run_panel.PROFILES:
        profile_home = home / ".ares" / "profiles" / profile
        profile_home.mkdir(parents=True, exist_ok=True)
        config_path = profile_home / "config.yaml"
        if not config_path.exists():
            config_path.write_text(
                "model:\n  provider: openai-codex\nmcp_servers: {}\nplugins:\n  enabled: []\n",
                encoding="utf-8",
            )
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--runtime", str(runtime),
            "--workspace", str(runtime.parent),
            "--out", str(out),
            "--brief", "test-only fake profile consultation",
            *routing,
            *extra,
        ],
        text=True,
        capture_output=True,
        timeout=timeout,
        env={**os.environ, "HOME": str(home)},
    )


def profile_program(body: str) -> str:
    return "import os, pathlib, time, subprocess, sys\nprofile = pathlib.Path(os.environ['HERMES_HOME']).name\n" + body


def initialize_git_repository(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    marker = path / "runtime.txt"
    marker.write_text("test runtime\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", marker.name], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "test runtime"], check=True)


@pytest.mark.parametrize("report", ["", "   "])
def test_zero_exit_empty_report_is_not_completion(tmp_path: Path, report: str) -> None:
    runtime = make_runtime(tmp_path, profile_program(f"print({report!r})\n"))
    out = tmp_path / "receipt"
    completed = invoke(runtime, out, "--profiles", "public")
    assert completed.returncode == 1
    panel = json.loads((out / "panel.json").read_text())
    assert panel["execution_complete"] is False
    assert panel["results"][0]["operational_block"] == "empty_report"


@pytest.mark.parametrize("message", [
    "API call failed after 3 retries: HTTP 429: You used all included Pro usage",
    "API call failed after 3 retries: HTTP 429: You used all included Pro usage. Max is $100/month for $300 of usage: https://ollama.com/settings/billing (ref: 90637c43-62dc-45b3-8b2b-4841d7117b1c)",
])
def test_zero_exit_provider_quota_failure_is_not_review(tmp_path: Path, message: str) -> None:
    runtime = make_runtime(tmp_path, profile_program(f"print({message!r})\n"))
    out = tmp_path / "receipt"
    completed = invoke(runtime, out, "--profiles", "public")
    assert completed.returncode == 1
    panel = json.loads((out / "panel.json").read_text())
    assert panel["execution_complete"] is False
    assert panel["results"][0]["outcome"] == "blocked"
    assert panel["results"][0]["operational_block"] == "provider_quota_exhausted"


def test_quoted_historical_quota_failure_is_not_blocked() -> None:
    message = b"API call failed after 3 retries: HTTP 429: You used all included Pro usage"
    assert run_panel.reported_operational_block(b"Historical error: " + message, b"") is None
    assert run_panel.reported_operational_block(b"Reviewed evidence.\n" + message + b"\nReview approved with qualifiers.", b"") is None


def test_verifier_rejects_hash_valid_quota_failure_report(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, profile_program("print('fixture review completed')\n"))
    initialize_git_repository(runtime)
    out = tmp_path / "receipt"
    assert invoke(runtime, out, "--profiles", "public").returncode == 0
    panel = json.loads((out / "panel.json").read_text())
    result = panel["results"][0]
    report = out / result["stdout_path"]
    report.write_text("API call failed after 3 retries: HTTP 429: You used all included Pro usage\n")
    result["stdout_sha256"] = run_panel.sha256_file(report)
    result["stdout_bytes"] = report.stat().st_size
    (out / "panel.json").write_text(json.dumps(panel))
    environment = {**os.environ, "PYTHONPATH": str(SCRIPT.parent)}
    command = [sys.executable, "-P", str(VERIFY_SCRIPT), "--receipt", str(out), "--runtime", str(runtime)]
    completed = subprocess.run(command, env=environment, capture_output=True, text=True)
    assert completed.returncode == 1
    assert "provider_quota_exhausted" in completed.stdout


def test_truncated_report_is_not_completion(tmp_path: Path) -> None:
    runtime = make_runtime(
        tmp_path, profile_program(f"print('x' * {run_panel.MAX_CAPTURE_BYTES + 1})\n")
    )
    out = tmp_path / "receipt"
    completed = invoke(runtime, out, "--profiles", "public")
    assert completed.returncode == 1
    panel = json.loads((out / "panel.json").read_text())
    assert panel["results"][0]["operational_block"] == "report_truncated"
    assert panel["results"][0]["stdout_truncated"] is True
    assert panel["execution_complete"] is False


def test_verifier_preflight_runs_exact_safe_path_context(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, profile_program("print('must not dispatch')\n"))
    result = run_panel.preflight_verifier(runtime)
    assert result["status"] == "passed"
    assert result["safe_path"] is True
    assert result["verifier_sha256"] == run_panel.sha256_file(VERIFY_SCRIPT)


def test_broken_verifier_preflight_cannot_start_worker(tmp_path: Path) -> None:
    copied = tmp_path / "broken-scripts"
    copied.mkdir()
    (copied / "run_panel.py").write_bytes(SCRIPT.read_bytes())
    (copied / "verify_receipt.py").write_text("import nonexistent_verifier_dependency\n")
    marker = tmp_path / "worker-started"
    runtime = make_runtime(tmp_path, profile_program(f"pathlib.Path({str(marker)!r}).touch()\nprint('report')\n"))
    out = tmp_path / "receipt"
    invoke(runtime, tmp_path / "setup-only", "--profiles", "public", "--dry-run")
    completed = subprocess.run(
        [sys.executable, str(copied / "run_panel.py"), "--runtime", str(runtime),
         "--workspace", str(tmp_path), "--out", str(out), "--profiles", "public", "--brief", "bounded fixture"],
        env={**os.environ, "HOME": str(tmp_path / "home")}, capture_output=True, text=True,
    )
    assert completed.returncode == 1
    assert not marker.exists()
    panel = json.loads((out / "panel.json").read_text())
    assert panel["verifier_preflight"]["status"] == "failed"
    assert panel["results"] == []
    assert panel["execution_complete"] is False


@pytest.mark.linux_only
def test_readonly_review_denies_writes_and_preserves_session_accounting(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, profile_program("print('report')\n"))
    owner = tmp_path / "owner"
    owner.mkdir()
    initialize_git_repository(owner)
    source = owner / "runtime.txt"
    index = owner / ".git" / "index"
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    config = profile_home / "config.yaml"
    config.write_text("model: fixture\n")
    original_index = index.read_bytes()
    receipt = tmp_path / "receipt"
    receipt.mkdir()
    context = run_panel.readonly_review_context(
        runtime=runtime, profile_home=profile_home, receipt_dir=receipt, profile="public"
    )
    state = Path(context["state_home"])
    (state / "escape").symlink_to(source)
    program = f'''
import json, pathlib, sqlite3, subprocess, sys
denied = []
for name in {str(source)!r}, {str(index)!r}, {str(config)!r}, {str(state / "escape")!r}:
    try:
        pathlib.Path(name).write_text("unauthorized")
    except OSError:
        denied.append(name)
result = subprocess.run([sys.executable, "-c", "from pathlib import Path; Path(" + repr({str(source)!r}) + ").write_text('unauthorized')"], capture_output=True)
db = sqlite3.connect({str(state / "state.db")!r})
db.execute("create table probe(value text)")
db.execute("insert into probe values ('accounting works')")
db.commit()
db.close()
print(json.dumps({{"denied": denied, "subprocess_denied": result.returncode != 0}}))
'''
    completed = subprocess.run(
        [*context["launch_prefix"], sys.executable, "-P", "-c", program],
        capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert len(payload["denied"]) == 4
    assert payload["subprocess_denied"] is True
    assert source.read_text() == "test runtime\n"
    assert index.read_bytes() == original_index
    assert config.read_text() == "model: fixture\n"
    assert (state / "state.db").is_file()
    assert not (profile_home / "state.db").exists()


@pytest.mark.linux_only
def test_readonly_review_uses_run_owned_auth_lock_and_readonly_auth_store(
    tmp_path: Path,
) -> None:
    runtime = make_runtime(tmp_path, profile_program("print('report')\n"))
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    auth_store = profile_home / "auth.json"
    auth_store.write_text('{"version": 1, "providers": {}}\n', encoding="utf-8")
    canonical_lock = profile_home / "auth.lock"
    canonical_lock.write_text("canonical-lock\n", encoding="utf-8")
    receipt = tmp_path / "receipt"
    receipt.mkdir()

    context = run_panel.readonly_review_context(
        runtime=runtime,
        profile_home=profile_home,
        receipt_dir=receipt,
        profile="public",
    )
    state = Path(context["state_home"])
    run_lock = state / "auth.lock"

    assert context["run_owned_profile_entries"] == ["auth.lock"]
    assert "auth.json" in context["protected_profile_entries"]
    assert "auth.lock" not in context["protected_profile_entries"]
    assert run_lock.is_file()
    assert run_lock.stat().st_mode & 0o777 == 0o600

    program = f'''
import json, pathlib
auth_store = pathlib.Path({str(auth_store)!r})
auth_lock = pathlib.Path({str(canonical_lock)!r})
try:
    auth_store.write_text("unauthorized")
    store_denied = False
except OSError:
    store_denied = True
auth_lock.write_text("run-owned-lock\\n")
print(json.dumps({{"store_denied": store_denied, "lock_text": auth_lock.read_text()}}))
'''
    completed = subprocess.run(
        [*context["launch_prefix"], sys.executable, "-P", "-c", program],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "store_denied": True,
        "lock_text": "run-owned-lock\n",
    }
    assert auth_store.read_text(encoding="utf-8") == '{"version": 1, "providers": {}}\n'
    assert canonical_lock.read_text(encoding="utf-8") == "canonical-lock\n"
    assert run_lock.read_text(encoding="utf-8") == "run-owned-lock\n"


def test_normal_return_reaps_live_descendant_without_inherited_pipes(tmp_path: Path) -> None:
    pid_file = tmp_path / "descendant.pid"
    ready = tmp_path / "descendant.ready"
    child_program = (
        "import signal,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).touch(); time.sleep(60)"
    )
    runtime = make_runtime(tmp_path, profile_program(
        f"child = subprocess.Popen([sys.executable, '-c', {child_program!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        "deadline = time.monotonic() + 5\n"
        f"while not pathlib.Path({str(ready)!r}).exists() and time.monotonic() < deadline:\n time.sleep(.01)\n"
        "print('completed report')\n"
    ))
    out = tmp_path / "receipt"
    try:
        completed = invoke(runtime, out, "--profiles", "public")
        assert completed.returncode == 0
        panel = json.loads((out / "panel.json").read_text())
        result = panel["results"][0]
        assert result["cleanup"]["group_gone"] is True
        assert result["cleanup"]["kill_sent"] is True
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_missing_sandbox_fails_closed_before_state_creation(tmp_path: Path, monkeypatch) -> None:
    runtime = make_runtime(tmp_path, profile_program("print('must not run')\n"))
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setattr(run_panel.shutil, "which", lambda _: None)
    with pytest.raises(run_panel.ReadonlyReviewBlocked, match="UNAVAILABLE"):
        run_panel.readonly_review_context(runtime=runtime, profile_home=profile_home, receipt_dir=tmp_path / "receipt", profile="public")
    assert not (tmp_path / "receipt").exists()


def test_safe_path_verification_preserves_failed_and_successful_attempts(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, profile_program("print('report')\n"))
    initialize_git_repository(runtime)
    out = tmp_path / "receipt"
    assert invoke(runtime, out, "--profiles", "public").returncode == 0
    environment = {**os.environ, "PYTHONPATH": str(SCRIPT.parent)}
    command = [sys.executable, "-P", str(VERIFY_SCRIPT), "--receipt", str(out), "--runtime", str(runtime)]
    assert subprocess.run(command, env=environment, capture_output=True).returncode == 0
    first = json.loads((out / "verification.json").read_text())
    first_path = Path(first["attempt_path"])
    first_bytes = first_path.read_bytes()
    assert first["execution_artifacts_verified"] is True
    assert first["semantic_review_complete"] is False
    panel_path = out / "panel.json"
    original = panel_path.read_bytes()
    panel = json.loads(original)
    panel["execution_complete"] = False
    panel_path.write_text(json.dumps(panel))
    assert subprocess.run(command, env=environment, capture_output=True).returncode == 1
    failed = json.loads((out / "verification.json").read_text())
    failed_path = Path(failed["attempt_path"])
    failed_bytes = failed_path.read_bytes()
    panel_path.write_bytes(original)
    assert subprocess.run(command, env=environment, capture_output=True).returncode == 0
    assert first_path.read_bytes() == first_bytes
    assert failed_path.read_bytes() == failed_bytes
    assert failed["execution_artifacts_verified"] is False
    assert subprocess.run(command + ["--require-read-only-review"], env=environment, capture_output=True).returncode == 1


def test_verifier_rejects_contained_receipt_without_run_owned_auth_lock(
    tmp_path: Path,
) -> None:
    runtime = make_runtime(tmp_path, profile_program("print('report')\n"))
    initialize_git_repository(runtime)
    out = tmp_path / "receipt"
    assert invoke(runtime, out, "--profiles", "public").returncode == 0

    state = out / "session-state" / "public"
    scratch = out / "scratch" / "public"
    state.mkdir(parents=True)
    scratch.mkdir(parents=True)
    panel_path = out / "panel.json"
    panel = json.loads(panel_path.read_text(encoding="utf-8"))
    panel["read_only_review"] = True
    result = panel["results"][0]
    result["containment"] = {
        "policy": run_panel.READONLY_REVIEW_POLICY,
        "state_home": str(state),
        "scratch": str(scratch),
        "network_isolation": "none",
        "secret_isolation": "not_provided",
        "protected_profile_entries": ["auth.json", "auth.lock"],
        "run_owned_profile_entries": [],
    }
    result["session_archive_outcome"] = "archived"
    input_path = out / result["input_manifest_path"]
    input_manifest = json.loads(input_path.read_text(encoding="utf-8"))
    input_manifest["containment"] = result["containment"]
    input_path.write_text(json.dumps(input_manifest, indent=2) + "\n", encoding="utf-8")
    result["input_manifest_bytes"] = input_path.stat().st_size
    result["input_manifest_sha256"] = run_panel.sha256_file(input_path)
    panel_path.write_text(json.dumps(panel), encoding="utf-8")

    environment = {**os.environ, "PYTHONPATH": str(SCRIPT.parent)}
    command = [
        sys.executable,
        "-P",
        str(VERIFY_SCRIPT),
        "--receipt",
        str(out),
        "--runtime",
        str(runtime),
        "--require-read-only-review",
    ]
    completed = subprocess.run(command, env=environment, capture_output=True, text=True)

    assert completed.returncode == 1
    assert "run-owned auth lock" in completed.stdout


@pytest.mark.linux_only
def test_readonly_namespace_cleanup_kills_descendants(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, profile_program("print('report')\n"))
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    receipt = tmp_path / "receipt"
    receipt.mkdir()
    context = run_panel.readonly_review_context(runtime=runtime, profile_home=profile_home, receipt_dir=receipt, profile="public")
    ready = Path(context["scratch"]) / "ready"
    program = f"import subprocess,sys,time,pathlib; subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)']); pathlib.Path({str(ready)!r}).touch(); time.sleep(60)"
    process = subprocess.Popen([*context["launch_prefix"], sys.executable, "-P", "-c", program], stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        cleanup = run_panel.terminate_process_group(process)
        process.communicate(timeout=5)
        assert cleanup["group_gone"] is True
        assert cleanup["leader_reaped"] is True
    finally:
        run_panel.terminate_process_group(process)


def test_defaults_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["run_panel.py", "--brief", "x", "--full-panel"])
    args = run_panel.parse_args()
    assert args.timeout == 300.0
    assert args.panel_timeout == 900.0
    assert args.max_workers == 10
    assert run_panel.SESSION_WORKER_LIMIT == 10
    assert run_panel.GLOBAL_WORKER_LIMIT == 25


def test_dirty_runtime_source_is_rejected(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, profile_program("print('should not run')\n"))
    initialize_git_repository(runtime)
    (runtime / "runtime.txt").write_text("modified source\n", encoding="utf-8")
    completed = invoke(runtime, tmp_path / "dirty-receipt", "--profiles", "public")
    assert completed.returncode != 0
    assert "runtime worktree is dirty" in completed.stderr


def test_profile_process_imports_runtime_not_workspace_shadow(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    python = runtime / ".venv" / "bin" / "python"
    runtime.mkdir()
    if sys.prefix != sys.base_prefix:
        # Preserve the actual interpreter's dependency environment. A bare
        # executable symlink loses pyvenv.cfg and breaks verifier preflight.
        (runtime / ".venv").symlink_to(sys.prefix, target_is_directory=True)
    else:
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
    package = runtime / "hermes_cli"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "main.py").write_text("print('runtime-owner')\n", encoding="utf-8")
    (runtime / "hermes_state.py").write_text(
        "class SessionDB:\n"
        "    def get_session(self, session_id): return None\n"
        "    def close(self): pass\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(runtime)], check=True)
    subprocess.run(["git", "-C", str(runtime), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(runtime), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(runtime), "add", "."], check=True)
    subprocess.run(["git", "-C", str(runtime), "commit", "-qm", "test runtime"], check=True)

    workspace = tmp_path / "workspace"
    shadow = workspace / "hermes_cli"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("", encoding="utf-8")
    (shadow / "main.py").write_text("print('workspace-shadow')\n", encoding="utf-8")
    poison_marker = workspace / "workspace-hermes-state-imported"
    (workspace / "hermes_state.py").write_text(
        f"from pathlib import Path\nPath({str(poison_marker)!r}).write_text('poison')\n"
        "raise RuntimeError('workspace hermes_state imported')\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    profile_home = home / ".ares" / "profiles" / "public"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "model:\n  provider: openai-codex\nmcp_servers: {}\nplugins:\n  enabled: []\n",
        encoding="utf-8",
    )
    out = tmp_path / "receipt"

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--runtime", str(runtime),
            "--workspace", str(workspace),
            "--out", str(out),
            "--brief", "verify source ownership",
            "--profiles", "public",
        ],
        text=True,
        capture_output=True,
        timeout=10,
        env={**os.environ, "HOME": str(home)},
    )

    assert completed.returncode == 0
    panel = json.loads((out / "panel.json").read_text(encoding="utf-8"))
    stdout = (out / panel["results"][0]["stdout_path"]).read_text(encoding="utf-8")
    assert stdout.strip() == "runtime-owner"
    assert panel["results"][0]["session_archive_outcome"] == "not_created"
    assert not poison_marker.exists()


def test_session_worker_limit_accepts_ten_and_rejects_eleven(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, profile_program("print('ok')\n"))
    accepted = invoke(runtime, tmp_path / "accepted", "--profiles", "public", "--max-workers", "10")
    rejected = invoke(runtime, tmp_path / "rejected", "--profiles", "public", "--max-workers", "11")

    assert accepted.returncode == 0
    assert rejected.returncode != 0
    assert "between 1 and 10" in rejected.stderr


def test_global_admission_pool_is_shared_and_bounded(tmp_path: Path) -> None:
    directory = tmp_path / "admission"
    first_pool = run_panel.GlobalAdmissionPool(directory, limit=3)
    second_pool = run_panel.GlobalAdmissionPool(directory, limit=3)
    cancelled = threading.Event()
    first, _ = first_pool.acquire(profile="public", run_id="one", weight=2, panel_cancelled=cancelled)
    second, _ = second_pool.acquire(profile="explorer", run_id="two", weight=1, panel_cancelled=cancelled)
    assert len(first.slots) == 2
    assert len(second.slots) == 1
    assert set(first.slots).isdisjoint(second.slots)
    acquired = threading.Event()
    result: dict[str, Any] = {}

    def acquire_third() -> None:
        lease, waited = second_pool.acquire(
            profile="public",
            run_id="three",
            weight=2,
            panel_cancelled=cancelled,
        )
        result["lease"] = lease
        result["waited"] = waited
        acquired.set()

    worker = threading.Thread(target=acquire_third)
    worker.start()
    time.sleep(0.12)
    assert not acquired.is_set()
    first.release()
    assert acquired.wait(1.0)
    assert float(result["waited"]) >= 0.1
    third = result["lease"]
    assert isinstance(third, run_panel.AdmissionLease)
    third.release()
    second.release()
    worker.join(timeout=1.0)


def test_profile_resource_plan_adapts_to_current_config(tmp_path: Path) -> None:
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    config_path = profile_home / "config.yaml"
    config_path.write_text(
        "model:\n  provider: openai-codex\nmcp_servers:\n  one: {}\nplugins:\n  enabled: []\n",
        encoding="utf-8",
    )
    light = run_panel.profile_resource_plan(profile_home)
    assert light["weight"] == 1
    assert light["timeout_floor_seconds"] is None
    assert light["reasoning_effort"] == "low"

    config_path.write_text(
        "model:\n  provider: openai-codex\n"
        "mcp_servers: {}\nplugins:\n  enabled: []\n"
        "agent:\n  reasoning_effort: high\n"
        "profile_collaboration:\n  timeout_seconds: 600\n",
        encoding="utf-8",
    )
    timed = run_panel.profile_resource_plan(profile_home)
    assert timed["timeout_floor_seconds"] == 600.0
    assert timed["reasoning_effort"] == "high"
    assert run_panel.effective_profile_timeout(300.0, timed) == 600.0
    assert run_panel.effective_profile_timeout(900.0, timed) == 900.0

    config_path.write_text(
        "model:\n  provider: openai-codex\n"
        "mcp_servers:\n  one: {}\n  two: {}\n  three: {}\n  four: {}\n  five: {}\n  six: {}\n"
        "plugins:\n  enabled: [recursive-agent-native]\n",
        encoding="utf-8",
    )
    auxiliary_heavy = run_panel.profile_resource_plan(profile_home)
    assert auxiliary_heavy["weight"] == 4
    assert auxiliary_heavy["enabled_mcp_count"] == 6
    assert auxiliary_heavy["enabled_plugin_count"] == 1

    config_path.write_text(
        "model:\n  provider: ollama\nmcp_servers: {}\nplugins:\n  enabled: []\n",
        encoding="utf-8",
    )
    local = run_panel.profile_resource_plan(profile_home)
    assert local["weight"] == 3

    config_path.write_text(
        "model:\n  provider: openai-codex\nmcp_servers: []\nplugins: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="mcp_servers is not a mapping"):
        run_panel.profile_resource_plan(profile_home)

    config_path.write_text(
        "model:\n  provider: openai-codex\nmcp_servers: {}\nplugins: {}\n"
        "profile_collaboration:\n  timeout_seconds: 0\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="timeout_seconds"):
        run_panel.profile_resource_plan(profile_home)

    config_path.write_text(
        "model:\n  provider: openai-codex\nmcp_servers: {}\nplugins: {}\n"
        "agent:\n  reasoning_effort: unsupported\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="reasoning_effort"):
        run_panel.profile_resource_plan(profile_home)


def test_panel_child_environment_drops_ambient_profile_and_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-controller-secret")
    monkeypatch.setenv("HERMES_HOME", "/synthetic/controller-profile")
    monkeypatch.setenv("HERMES_TUI", "1")
    monkeypatch.setenv("UNRELATED_CONTROLLER_STATE", "must-not-cross-boundary")
    environment = run_panel.panel_child_environment()
    assert environment.get("HOME")
    assert environment.get("PATH")
    assert "OPENAI_API_KEY" not in environment
    assert "HERMES_HOME" not in environment
    assert "HERMES_TUI" not in environment
    assert "UNRELATED_CONTROLLER_STATE" not in environment


def test_weighted_admission_wait_cancels_cleanly(tmp_path: Path) -> None:
    pool = run_panel.GlobalAdmissionPool(tmp_path / "admission", limit=2)
    holder, _ = pool.acquire(
        profile="public",
        run_id="holder",
        weight=2,
        panel_cancelled=None,
    )
    cancelled = threading.Event()
    outcome: dict[str, str] = {}

    def blocked_acquire() -> None:
        try:
            pool.acquire(
                profile="explorer",
                run_id="blocked",
                weight=1,
                panel_cancelled=cancelled,
            )
        except concurrent.futures.CancelledError:
            outcome["state"] = "cancelled"

    worker = threading.Thread(target=blocked_acquire)
    worker.start()
    time.sleep(0.12)
    cancelled.set()
    worker.join(timeout=1.0)
    holder.release()
    assert outcome == {"state": "cancelled"}


def test_weighted_admission_releases_partial_slots_on_metadata_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "admission"
    pool = run_panel.GlobalAdmissionPool(directory, limit=2)
    real_fsync = run_panel.os.fsync

    def fail_fsync(_fd: int) -> None:
        raise OSError("injected metadata failure")

    monkeypatch.setattr(run_panel.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="injected metadata failure"):
        pool.acquire(
            profile="public",
            run_id="failure",
            weight=2,
            panel_cancelled=None,
        )
    monkeypatch.setattr(run_panel.os, "fsync", real_fsync)
    lease, _ = pool.acquire(
        profile="public",
        run_id="recovered",
        weight=2,
        panel_cancelled=None,
    )
    lease.release()


def test_same_specialist_can_run_independently_in_two_sessions(tmp_path: Path) -> None:
    runtime = make_runtime(
        tmp_path,
        profile_program(
            "time.sleep(0.15)\n"
            "print(os.environ['HERMES_ONESHOT_SESSION_ID'])\n"
        ),
    )
    # Same receipt-directory leaf names used in separate case/workspace folders
    # must not alias the deterministic oneshot session identity.
    outputs = [tmp_path / "case-one" / "panel", tmp_path / "case-two" / "panel"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(invoke, runtime, output, "--profiles", "public")
            for output in outputs
        ]
    completed = [future.result() for future in futures]

    assert all(item.returncode == 0 for item in completed)
    panels = [json.loads((output / "panel.json").read_text()) for output in outputs]
    session_ids = {panel["results"][0]["session_id"] for panel in panels}
    assert len(session_ids) == 2
    assert all(panel["results"][0]["profile"] == "public" for panel in panels)
    assert all(panel["session_identity_policy"] == "panel_run_id_bound_v1" for panel in panels)
    assert all(panel["results"][0]["session_id"] == f"profile-panel-{panel['run_id']}-public" for panel in panels)
    assert all(panel["results"][0]["session_id"] in (output / panel["results"][0]["stdout_path"]).read_text() for panel, output in zip(panels, outputs))


def test_oneshot_session_preflight_detects_prior_id_without_reading_content(tmp_path: Path) -> None:
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    db = profile_home / "state.db"
    import sqlite3

    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO sessions (id) VALUES (?)", ("profile-panel-old-run-public",))

    assert run_panel.panel_session_id("new-run", "public") == "profile-panel-new-run-public"
    assert run_panel.panel_session_id("new-run", "public") != run_panel.panel_session_id("old-run", "public")
    assert run_panel.panel_session_id_exists(profile_home, "profile-panel-old-run-public") is True
    assert run_panel.panel_session_id_exists(profile_home, "profile-panel-new-run-public") is False


def test_verifier_enforces_admission_contract(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, profile_program("print('verified report')\n"))
    initialize_git_repository(runtime)
    out = tmp_path / "receipt"
    completed = invoke(runtime, out, "--profiles", "public")
    environment = {**os.environ, "HOME": str(tmp_path / "home")}

    assert completed.returncode == 0
    verified = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), "--receipt", str(out), "--runtime", str(runtime)],
        text=True,
        capture_output=True,
        env=environment,
    )
    assert verified.returncode == 0

    panel_path = out / "panel.json"
    panel = json.loads(panel_path.read_text(encoding="utf-8"))
    panel["global_worker_limit"] = 24
    panel_path.write_text(json.dumps(panel), encoding="utf-8")
    rejected = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), "--receipt", str(out), "--runtime", str(runtime)],
        text=True,
        capture_output=True,
        env=environment,
    )
    assert rejected.returncode == 1
    assert "global worker limit does not match" in rejected.stdout


def test_receipt_telemetry_summarizes_without_reading_reports(tmp_path: Path) -> None:
    first = tmp_path / "one" / "panel.json"
    second = tmp_path / "two" / "panel.json"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text(
        json.dumps(
            {
                "schema": "AresProfilePanelReceiptV1",
                "results": [
                    {
                        "profile": "public",
                        "outcome": "returned",
                        "duration_seconds": 10.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    second.write_text(
        json.dumps(
            {
                "schema": "AresProfilePanelReceiptV2",
                "results": [
                    {
                        "profile": "public",
                        "outcome": "timed_out",
                        "duration_seconds": 20.0,
                        "admission_wait_seconds": 2.0,
                        "resource_weight": 4,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = summarize_receipts.summarize([first, second])
    public = report["profiles"]["public"]
    assert report["receipt_count"] == 2
    assert report["lane_count"] == 2
    assert public["return_rate"] == 0.5
    assert public["duration_seconds"]["p50"] == 10.0
    assert public["duration_seconds"]["p95"] == 20.0
    assert public["admission_wait_seconds"]["p95"] == 2.0
    assert public["observed_resource_weights"] == [4]


def test_profile_selection_is_explicit_minimal_and_canonical() -> None:
    assert run_panel.select_profiles("psychometrician,public", False) == (
        "public",
        "psychometrician",
    )
    assert run_panel.select_profiles(None, True) == run_panel.PROFILES
    with pytest.raises(ValueError, match="select --profiles"):
        run_panel.select_profiles(None, False)
    with pytest.raises(ValueError, match="unknown profiles"):
        run_panel.select_profiles("not-a-profile", False)


def test_selective_run_requests_hidden_automation_session(tmp_path: Path) -> None:
    runtime = make_runtime(
        tmp_path,
        profile_program(
            "print(os.environ['HERMES_ONESHOT_SESSION_ID'])\n"
            "print(os.environ['HERMES_ONESHOT_ARCHIVE_SESSION'])\n"
        ),
    )
    out = tmp_path / "receipt"
    completed = invoke(runtime, out, "--profiles", "public")

    assert completed.returncode == 0
    panel = json.loads((out / "panel.json").read_text())
    assert panel["required_profiles"] == ["public"]
    assert panel["routing_mode"] == "relevance_gated"
    assert panel["session_projection_policy"] == "archive_automation_oneshot_v1"
    assert panel["terminal_state"] == "completed"
    result = panel["results"][0]
    assert result["session_archive_requested"] is True
    stdout = (out / result["stdout_path"]).read_text()
    assert result["session_id"] in stdout
    assert stdout.rstrip().endswith("1")


def test_exit_zero_runtime_block_report_fails_closed(tmp_path: Path) -> None:
    runtime = make_runtime(
        tmp_path,
        profile_program(
            'print("Unable to complete because every inspection tool failed: '
            'DaemonThreadPoolExecutor object has no attribute \'_initializer\'")\n'
        ),
    )
    out = tmp_path / "receipt"

    completed = invoke(runtime, out, "--profiles", "public")

    assert completed.returncode == 1
    panel = json.loads((out / "panel.json").read_text(encoding="utf-8"))
    result = panel["results"][0]
    assert result["exit_code"] == 0
    assert result["outcome"] == "blocked"
    assert result["termination_reason"] == "operational_blocked_report"
    assert result["operational_block"] == "daemon_pool_initializer_failure"
    assert panel["terminal_state"] == "completed_with_failures"
    assert panel["execution_complete"] is False


def test_exit_zero_invalidated_oauth_report_fails_closed(tmp_path: Path) -> None:
    runtime = make_runtime(
        tmp_path,
        profile_program(
            'print("HTTP 401: Encountered invalidated oauth token for user, failing request")\n'
        ),
    )
    out = tmp_path / "receipt"
    completed = invoke(runtime, out, "--profiles", "psychometrician")

    assert completed.returncode == 1
    panel = json.loads((out / "panel.json").read_text(encoding="utf-8"))
    result = panel["results"][0]
    assert result["exit_code"] == 0
    assert result["outcome"] == "blocked"
    assert result["termination_reason"] == "operational_blocked_report"
    assert result["operational_block"] == "oauth_token_invalidated"
    assert (out / result["stdout_path"]).read_text().startswith("HTTP 401:")
    assert panel["terminal_state"] == "completed_with_failures"
    assert panel["execution_complete"] is False


def test_quoted_historical_oauth_401_is_not_a_runtime_block(tmp_path: Path) -> None:
    report = json.dumps({
        "analysis": "A historical fixture returned HTTP 401: Encountered invalidated oauth token for user, failing request; no live auth error occurred."
    })
    runtime = make_runtime(tmp_path, profile_program(f"print({report!r})\n"))
    out = tmp_path / "receipt"
    completed = invoke(runtime, out, "--profiles", "psychometrician")

    assert completed.returncode == 0
    panel = json.loads((out / "panel.json").read_text(encoding="utf-8"))
    result = panel["results"][0]
    assert result["outcome"] == "returned"
    assert result["termination_reason"] == "returned"
    assert result["operational_block"] is None
    assert panel["execution_complete"] is True


def test_controller_contract_binds_workspace_and_forbids_nested_work(tmp_path: Path) -> None:
    runtime = make_runtime(
        tmp_path,
        profile_program("print(sys.argv[-1])\n"),
    )
    out = tmp_path / "receipt"
    completed = invoke(runtime, out, "--profiles", "public", "--timeout", "2")

    assert completed.returncode == 0
    panel = json.loads((out / "panel.json").read_text())
    result = panel["results"][0]
    brief = (out / result["stdout_path"]).read_text()
    assert f"Assigned absolute workspace: {tmp_path}" in brief
    assert "Do not spawn nested profile" in brief
    assert panel["controller_contract"] == "bounded_workspace_no_nested_delegation_v1"
    assert panel["effective_brief_sha256"]


def test_profile_timeout_floor_raises_selected_lane_and_panel_deadline(tmp_path: Path) -> None:
    runtime = make_runtime(
        tmp_path,
        profile_program(
            "print(f\"{os.environ['ARES_PANEL_TIMEOUT_SECONDS']}:{os.environ['ARES_PANEL_REASONING_EFFORT']}\")\n"
        ),
    )
    profile_home = runtime.parent / "home" / ".ares" / "profiles" / "public"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "model:\n  provider: openai-codex\nmcp_servers: {}\nplugins:\n  enabled: []\n"
        "agent:\n  reasoning_effort: high\n"
        "profile_collaboration:\n  timeout_seconds: 600\n",
        encoding="utf-8",
    )
    out = tmp_path / "receipt"
    completed = invoke(
        runtime,
        out,
        "--profiles",
        "public",
        "--timeout",
        "2",
        "--panel-timeout",
        "3",
    )

    assert completed.returncode == 0
    panel = json.loads((out / "panel.json").read_text(encoding="utf-8"))
    assert panel["timeout_seconds"] == 2.0
    assert panel["effective_timeout_seconds_by_profile"] == {"public": 600.0}
    assert panel["requested_panel_timeout_seconds"] == 3.0
    assert panel["panel_timeout_seconds"] == 600.0
    result = panel["results"][0]
    assert result["timeout_seconds"] == 600.0
    assert result["reasoning_effort"] == "high"
    assert (out / result["stdout_path"]).read_text(encoding="utf-8").rstrip().endswith("600.0:high")
    input_document = json.loads((out / result["input_manifest_path"]).read_text(encoding="utf-8"))
    assert input_document["timeout_seconds"] == 600.0
    assert input_document["reasoning_effort"] == "high"
    assert input_document["effective_brief_sha256"] == panel["effective_brief_sha256_by_profile"]["public"]


def test_mixed_profile_timeout_floors_bind_each_effective_brief(tmp_path: Path) -> None:
    runtime = make_runtime(
        tmp_path,
        profile_program(
            "print(f'{profile}:{os.environ[\"ARES_PANEL_TIMEOUT_SECONDS\"]}:{os.environ[\"ARES_PANEL_REASONING_EFFORT\"]}')\n"
        ),
    )
    initialize_git_repository(runtime)
    home = runtime.parent / "home"
    for profile, config in {
        "public": (
            "model:\n  provider: openai-codex\nmcp_servers: {}\nplugins:\n  enabled: []\n"
            "agent:\n  reasoning_effort: high\n"
            "profile_collaboration:\n  timeout_seconds: 600\n"
        ),
        "explorer": "model:\n  provider: openai-codex\nmcp_servers: {}\nplugins:\n  enabled: []\n",
    }.items():
        profile_home = home / ".ares" / "profiles" / profile
        profile_home.mkdir(parents=True, exist_ok=True)
        (profile_home / "config.yaml").write_text(config, encoding="utf-8")
    out = tmp_path / "receipt"
    completed = invoke(
        runtime,
        out,
        "--profiles",
        "public,explorer",
        "--timeout",
        "2",
        "--panel-timeout",
        "3",
        "--max-workers",
        "2",
    )

    assert completed.returncode == 0
    panel = json.loads((out / "panel.json").read_text(encoding="utf-8"))
    assert panel["effective_brief_sha256"] is None
    assert panel["effective_timeout_seconds_by_profile"] == {"public": 600.0, "explorer": 2.0}
    assert panel["panel_timeout_seconds"] == 600.0
    results = {result["profile"]: result for result in panel["results"]}
    assert results["public"]["timeout_seconds"] == 600.0
    assert results["explorer"]["timeout_seconds"] == 2.0
    assert results["public"]["reasoning_effort"] == "high"
    assert results["explorer"]["reasoning_effort"] == "low"
    for profile, result in results.items():
        input_document = json.loads((out / result["input_manifest_path"]).read_text(encoding="utf-8"))
        assert input_document["timeout_seconds"] == result["timeout_seconds"]
        assert input_document["reasoning_effort"] == result["reasoning_effort"]
        assert input_document["effective_brief_sha256"] == panel["effective_brief_sha256_by_profile"][profile]
    verified = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), "--receipt", str(out), "--runtime", str(runtime)],
        text=True,
        capture_output=True,
        env={**os.environ, "HOME": str(home)},
    )
    assert verified.returncode == 0


def test_first_pass_profiles_receive_digest_bound_inputs_without_peer_edges(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, profile_program("print('independent first pass')\n"))
    initialize_git_repository(runtime)
    out = tmp_path / "receipt"
    completed = invoke(
        runtime,
        out,
        "--profiles",
        "public,explorer",
        "--max-workers",
        "2",
    )

    assert completed.returncode == 0
    panel = json.loads((out / "panel.json").read_text(encoding="utf-8"))
    assert panel["input_manifest_policy"] == "first_pass_no_peer_edges_v1"
    manifests = []
    for result in panel["results"]:
        path = out / result["input_manifest_path"]
        raw = path.read_bytes()
        assert len(raw) == result["input_manifest_bytes"]
        assert __import__("hashlib").sha256(raw).hexdigest() == result["input_manifest_sha256"]
        manifest = json.loads(raw)
        manifests.append(manifest)
        assert manifest["profile"] == result["profile"]
        assert manifest["session_id"] == result["session_id"]
        assert manifest["brief_sha256"] == panel["brief_sha256"]
        assert manifest["effective_brief_sha256"] == panel["effective_brief_sha256"]
        assert manifest["edge_policy"] == "controller_to_first_pass_profile_only_v1"
        assert manifest["review_barrier"] == "panel_complete"
        assert manifest["peer_outputs_admissible"] is False
        assert {item["kind"] for item in manifest["input_objects"]} == {
            "controller_brief",
            "assigned_workspace",
        }
        assert set(manifest["excluded_input_kinds"]) == {
            "peer_conclusion",
            "peer_report",
            "peer_stdout",
            "peer_stderr",
            "peer_transcript",
        }
    assert manifests[0]["brief_sha256"] == manifests[1]["brief_sha256"]
    assert manifests[0]["effective_brief_sha256"] == manifests[1]["effective_brief_sha256"]

    environment = {**os.environ, "HOME": str(tmp_path / "home")}
    verified = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), "--receipt", str(out), "--runtime", str(runtime)],
        text=True,
        capture_output=True,
        env=environment,
    )
    assert verified.returncode == 0

    first_path = out / panel["results"][0]["input_manifest_path"]
    first = json.loads(first_path.read_text(encoding="utf-8"))
    first["input_objects"].append({"kind": "peer_report", "locator": "peer.txt"})
    first_path.write_text(json.dumps(first), encoding="utf-8")
    rejected = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), "--receipt", str(out), "--runtime", str(runtime)],
        text=True,
        capture_output=True,
        env=environment,
    )
    assert rejected.returncode == 1
    assert "input manifest" in rejected.stdout


def test_returning_and_hanging_profiles_hit_total_deadline_promptly(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, profile_program(
        "if profile == 'public':\n print('returned report')\n sys.exit(0)\ntime.sleep(60)\n"
    ))
    out = tmp_path / "receipt"
    started = time.monotonic()
    completed = invoke(runtime, out, "--max-workers", "2", "--timeout", "30", "--panel-timeout", "0.45")
    elapsed = time.monotonic() - started
    panel = json.loads((out / "panel.json").read_text())
    assert completed.returncode == 1
    assert elapsed < 3.0
    assert [r["profile"] for r in panel["results"]] == list(run_panel.PROFILES)
    outcomes = {r["profile"]: r["outcome"] for r in panel["results"]}
    assert outcomes["public"] == "returned"
    assert "timed_out" in outcomes.values()
    assert "cancelled" in outcomes.values()
    assert panel["panel_timed_out"] is True
    assert panel["terminal_state"] == "timed_out"
    assert panel["execution_complete"] is False


def test_atomic_partial_manifest_is_always_valid_json(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, profile_program("time.sleep(60)\n"))
    home = runtime.parent / "home"
    for profile in run_panel.PROFILES:
        profile_home = home / ".ares" / "profiles" / profile
        profile_home.mkdir(parents=True, exist_ok=True)
        (profile_home / "config.yaml").write_text(
            "model:\n  provider: openai-codex\nmcp_servers: {}\nplugins:\n  enabled: []\n",
            encoding="utf-8",
        )
    out = tmp_path / "receipt"
    process = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--runtime", str(runtime), "--workspace", str(tmp_path),
         "--out", str(out), "--brief", "fake", "--full-panel", "--max-workers", "2", "--timeout", "30",
         "--panel-timeout", "0.6"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "HOME": str(home)},
    )
    panel_path = out / "panel.json"
    saw_baseline = False
    deadline = time.monotonic() + 3
    while process.poll() is None and time.monotonic() < deadline:
        if panel_path.exists():
            document = json.loads(panel_path.read_text(encoding="utf-8"))
            assert document["schema"] == "AresProfilePanelReceiptV2"
            assert isinstance(document["results"], list)
            saw_baseline = True
        time.sleep(0.01)
    process.communicate(timeout=3)
    assert saw_baseline
    final = json.loads(panel_path.read_text(encoding="utf-8"))
    assert len(final["results"]) == len(run_panel.PROFILES)


def test_panel_deadline_kills_spawned_child_process_group(tmp_path: Path) -> None:
    pid_path = tmp_path / "child.pid"
    runtime = make_runtime(tmp_path, profile_program(
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"pathlib.Path({str(pid_path)!r}).write_text(str(p.pid))\n"
        "time.sleep(60)\n"
    ))
    completed = invoke(runtime, tmp_path / "receipt", "--max-workers", "1", "--timeout", "30", "--panel-timeout", "0.5")
    assert completed.returncode == 1
    child_pid = int(pid_path.read_text())
    deadline = time.monotonic() + 2
    while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
        status = Path(f"/proc/{child_pid}/status").read_text(errors="replace")
        if "State:\tZ" in status:
            break
        time.sleep(0.02)
    if Path(f"/proc/{child_pid}/status").exists():
        assert "State:\tZ" in Path(f"/proc/{child_pid}/status").read_text(errors="replace")


def test_cleanup_kills_descendant_after_leader_exits(tmp_path: Path) -> None:
    child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    parent_code = (
        "import signal,subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "print(child.pid, flush=True); "
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); time.sleep(60)"
    )
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", parent_code],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    child_pid = int(process.stdout.readline().strip())
    time.sleep(0.2)
    cleanup = run_panel.terminate_process_group(process)
    assert cleanup["leader_reaped"] is True
    assert cleanup["kill_sent"] is True
    assert cleanup["group_gone"] is True
    process.communicate(timeout=1)
    assert not Path(f"/proc/{child_pid}").exists()


def test_per_profile_timeout_without_panel_timeout(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, profile_program("time.sleep(60)\n"))
    out = tmp_path / "receipt"
    completed = invoke(runtime, out, "--max-workers", "8", "--timeout", "0.2", "--panel-timeout", "5")
    panel = json.loads((out / "panel.json").read_text())
    assert completed.returncode == 1
    assert panel.get("panel_timed_out") is not True
    assert panel["terminal_state"] == "completed_with_failures"
    assert len(panel["results"]) == len(run_panel.PROFILES)
    assert all(result["outcome"] == "timed_out" for result in panel["results"])


def test_profile_timeout_bounds_pipe_drain_with_descendant(tmp_path: Path) -> None:
    runtime = make_runtime(
        tmp_path,
        profile_program(
            "if profile == 'public':\n"
            "    child = subprocess.Popen([sys.executable, '-c', "
            "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
            "    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
            "time.sleep(60)\n"
        ),
    )
    out = tmp_path / "receipt"
    started = time.monotonic()
    completed = invoke(runtime, out, "--profiles", "public", "--timeout", "0.2", "--panel-timeout", "5")
    elapsed = time.monotonic() - started
    panel = json.loads((out / "panel.json").read_text())
    result = panel["results"][0]
    assert completed.returncode == 1
    assert elapsed < 3.0
    assert result["termination_reason"] == "profile_timeout"
    assert result["cleanup_state"] == "clean"
    assert result["pipe_drain_completed"] is True
