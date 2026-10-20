"""Offline behavioral contract for reviewed installer policy execution."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import tomllib

import pytest

pytestmark = pytest.mark.linux_only

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/install_uv_policy.py"
spec = importlib.util.spec_from_file_location("install_uv_policy", HELPER)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def clean_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("UV_")}


def fixture(tmp_path, code=0, extra=""):
    project = tmp_path / "project"
    project.mkdir()
    (project / "venv/bin").mkdir(parents=True)
    (project / "venv/bin/python").symlink_to(sys.executable)
    (project / "pyproject.toml").write_text('[project]\nname="policy-probe"\nversion="0.0.0"\nrequires-python=">=3.11"\n[project.optional-dependencies]\nall=[]\n[tool.uv]\nexclude-newer="14 days"\n' + extra)
    (project / "uv.lock").write_text("fixture lock")
    uv = tmp_path / "uv"
    uv.write_text(f'''#!{sys.executable}
import json, os, pathlib, stat, sys
if sys.argv[1:] == ["--version"]:
 print("uv 0.12.19"); sys.exit(0)
p = pathlib.Path(sys.argv[sys.argv.index("--config-file") + 1])
pathlib.Path({str(tmp_path / 'record')!r}).write_text(json.dumps({{"args":sys.argv[1:],"cwd":os.getcwd(),"mode":stat.S_IMODE(p.stat().st_mode),"config":p.read_text(),"env":{{k:v for k,v in os.environ.items() if k.startswith('UV_')}}}}))
sys.exit({code})
''')
    uv.chmod(0o755)
    return project, uv


def command(project, uv):
    return [sys.executable, str(HELPER), "--root", str(project), "--uv", str(uv), "--python", str(project / "venv/bin/python")]


@pytest.mark.parametrize("code", [0, 1, 37, 124])
def test_private_same_origin_cleanup_and_status(tmp_path, code):
    project, uv = fixture(tmp_path, code, 'exclude-newer-package={demo=false}\n')
    before = [(project / p).read_bytes() for p in ("pyproject.toml", "uv.lock")]
    result = subprocess.run(command(project, uv), env=clean_env(), capture_output=True, text=True)
    assert result.returncode == code, result.stderr
    record = json.loads((tmp_path / "record").read_text())
    assert Path(record["args"][1]).parent == project
    assert record["args"][2:] == ["sync", "--extra", "all", "--locked"]
    assert record["cwd"] == str(project) and record["mode"] == 0o600
    assert tomllib.loads(record["config"])["exclude-newer-package"] == {"demo": False}
    assert record["env"]["UV_NO_CONFIG"] == "1"
    assert not list(project.glob(".ares-reviewed-uv-*"))
    assert before == [(project / p).read_bytes() for p in ("pyproject.toml", "uv.lock")]


@pytest.mark.parametrize("key", ["UV_EXCLUDE_NEWER", "UV_FROZEN", "UV_NO_SOURCES", "UV_WORKING_DIR", "UV_PROJECT", "UV_NO_INSTALL_PROJECT", "UV_NO_INSTALL_LOCAL", "UV_NO_INSTALL_WORKSPACE", "UV_NO_GROUP", "UV_NO_DEV", "UV_NO_DEFAULT_GROUPS", "UV_INDEX_URL", "UV_FUTURE_UNKNOWN"])
def test_ambient_policy_is_rejected_without_values(tmp_path, key):
    project, uv = fixture(tmp_path)
    env = clean_env(); env[key] = "credential-secret-123"
    result = subprocess.run(command(project, uv), env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert "credential-secret-123" not in result.stdout + result.stderr
    assert not (tmp_path / "record").exists()


@pytest.mark.parametrize("extra", [
    '"secret-key-123"=true\n', 'exclude-newer-package={demo=1}\n',
    'exclude-newer-package={demo="secret-value-123"}\n',
    'sources={demo={url="https://user:secret-value-123@example.test/a"}}\n',
    'sources={demo={git="https://example.test/a"}}\n',
    'sources={demo={index="private"}}\n', 'index=[]\n',
    'find-links=["https://example.test/a?token=secret-value-123"]\n',
    'override-dependencies=["demo @ https://user:secret-value-123@example.test/a"]\n',
    'no-index=1\n',
])
def test_unsupported_policy_fails_without_echo(tmp_path, extra):
    project, uv = fixture(tmp_path, extra=extra)
    result = subprocess.run(command(project, uv), env=clean_env(), capture_output=True, text=True)
    assert result.returncode == 2
    assert "secret-" not in result.stdout + result.stderr
    assert not (tmp_path / "record").exists()


@pytest.mark.parametrize("target", ["pyproject.toml", "uv.lock"])
def test_missing_input_stops_before_child(tmp_path, target):
    project, uv = fixture(tmp_path)
    (project / target).unlink()
    result = subprocess.run(command(project, uv), env=clean_env(), capture_output=True)
    assert result.returncode == 2
    assert not (tmp_path / "record").exists()


def test_native_metadata_stays_native_and_all_exceptions_preserved():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    projected = tomllib.loads(policy.project_policy(data))
    assert projected["exclude-newer-package"] == data["tool"]["uv"]["exclude-newer-package"]
    assert "override-dependencies" not in projected
    data["tool"]["uv"]["sources"] = {"demo": {"path": "../demo"}}
    assert "sources" not in tomllib.loads(policy.project_policy(data))


@pytest.mark.parametrize("code", [0, 37, 124, None])
def test_real_installer_stage_never_falls_back(tmp_path, code):
    project, uv = fixture(tmp_path, code or 0)
    if code is None:
        (project / "uv.lock").unlink()
    (project / "scripts").mkdir()
    shutil.copyfile(HELPER, project / "scripts/install_uv_policy.py")
    home = tmp_path / "home"; (home / "bin").mkdir(parents=True)
    managed = home / "bin/uv"
    # Bootstrap probes are harmless; execution still reaches the actual helper.
    managed.write_text(f'''#!/bin/sh
if [ "$1" = python ] && [ "$2" = find ]; then echo {sys.executable}; exit 0; fi
exec {uv} "$@"
'''); managed.chmod(0o755)
    bins = tmp_path / "bin"; bins.mkdir()
    (bins / "dpkg").write_text("#!/bin/sh\nexit 0\n"); (bins / "dpkg").chmod(0o755)
    env = clean_env(); env.update(HERMES_HOME=str(home), HERMES_INSTALL_DIR=str(project), PATH=str(bins) + ":" + env["PATH"])
    result = subprocess.run(["bash", str(ROOT / "scripts/install.sh"), "--stage", "python-deps", "--json"], env=env, capture_output=True, text=True)
    expected = 2 if code is None else code
    assert result.returncode == expected, result.stdout + result.stderr
    outcome = json.loads(result.stdout.splitlines()[-1])
    assert outcome["ok"] is (expected == 0)
    if expected:
        assert outcome["reason"] == f"exit code {expected}"
    if code is None:
        assert not (tmp_path / "record").exists()
        return
    record = json.loads((tmp_path / "record").read_text())
    assert record["args"][2:] == ["sync", "--extra", "all", "--locked"]


def test_signal_terminates_child_before_config_cleanup(tmp_path):
    project, uv = fixture(tmp_path)
    uv.write_text(f'''#!{sys.executable}
import os, pathlib, signal, sys, time
if sys.argv[1:] == ['--version']: print('uv 0.12.19'); sys.exit(0)
p = pathlib.Path(sys.argv[2])
pathlib.Path({str(tmp_path / 'ready')!r}).write_text(str(os.getpid()))
def stop(*args):
 pathlib.Path({str(tmp_path / 'saw_config')!r}).write_text(str(p.exists())); sys.exit(0)
signal.signal(signal.SIGTERM, stop)
while True: time.sleep(.05)
''')
    process = subprocess.Popen(command(project, uv), env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 10
    while not (tmp_path / "ready").exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert (tmp_path / "ready").exists()
    process.send_signal(signal.SIGTERM)
    process.communicate(timeout=10)
    assert process.returncode == 143
    assert (tmp_path / "saw_config").read_text() == "True"
    assert not list(project.glob(".ares-reviewed-uv-*"))


@pytest.mark.parametrize("change", ["malformed", "no-policy", "no-age", "competing", "symlink", "missing-python", "relative-uv", "old-uv", "future-uv"])
def test_preflight_boundaries(tmp_path, change):
    project, uv = fixture(tmp_path)
    if change == "malformed":
        (project / "pyproject.toml").write_text("secret-value-123 invalid TOML")
    elif change == "no-policy":
        (project / "pyproject.toml").write_text('[project]\nname="demo"\n')
    elif change == "no-age":
        (project / "pyproject.toml").write_text('[tool.uv]\nexclude-newer-package={}\n')
    elif change == "competing":
        (project / "uv.toml").write_text('exclude-newer=false')
    elif change == "symlink":
        (project / "uv.lock").unlink(); (project / "uv.lock").symlink_to(project / "pyproject.toml")
    elif change == "missing-python":
        (project / "venv/bin/python").unlink()
    elif change == "relative-uv":
        uv = Path("relative-uv")
    else:
        uv.write_text("#!/bin/sh\necho 'uv " + ("0.9.27" if change == "old-uv" else "0.13.0") + "'\n")
    result = subprocess.run(command(project, uv), env=clean_env(), capture_output=True, text=True)
    assert result.returncode == 2
    assert "secret-value-123" not in result.stdout + result.stderr
    assert not (tmp_path / "record").exists()
    assert not list(project.glob(".ares-reviewed-uv-*"))


def test_child_spawn_failure_cleans_config(tmp_path):
    project, uv = fixture(tmp_path)
    # Version probe succeeds then removes its executable before the sync spawn.
    uv.write_text(f'#!{sys.executable}\nimport os\nprint("uv 0.12.19")\nos.unlink(__file__)\n')
    result = subprocess.run(command(project, uv), env=clean_env(), capture_output=True)
    assert result.returncode == 2
    assert not list(project.glob(".ares-reviewed-uv-*"))


def test_exited_leader_descendant_is_stopped(tmp_path):
    project, uv = fixture(tmp_path)
    uv.write_text(f'''#!{sys.executable}
import os, pathlib, signal, sys, time
if sys.argv[1:] == ['--version']: print('uv 0.12.19'); sys.exit(0)
pid = os.fork()
if pid:
 pathlib.Path({str(tmp_path / 'descendant')!r}).write_text(str(pid))
 sys.exit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True: time.sleep(.05)
''')
    result = subprocess.run(command(project, uv), env=clean_env(), capture_output=True, timeout=10)
    assert result.returncode == 0
    pid = int((tmp_path / "descendant").read_text())
    # A reparented zombie is already terminated, even before init reaps it.
    status = Path(f"/proc/{pid}/stat")
    deadline = time.monotonic() + 3
    while status.exists() and status.read_text().split()[2] != "Z" and time.monotonic() < deadline:
        time.sleep(.02)
    assert not status.exists() or status.read_text().split()[2] == "Z"
    assert not list(project.glob(".ares-reviewed-uv-*"))


def test_real_uv_offline_relative_origin_and_user_isolation(tmp_path):
    import zipfile
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not installed; pure and subprocess contract tests still run")
    project, _ = fixture(tmp_path)
    wheels = project / "wheels"; wheels.mkdir()
    with zipfile.ZipFile(wheels / "fixture_package-1.0.0-py3-none-any.whl", "w") as wheel:
        wheel.writestr("fixture_package-1.0.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: fixture-package\nVersion: 1.0.0\n")
        wheel.writestr("fixture_package-1.0.0.dist-info/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        wheel.writestr("fixture_package-1.0.0.dist-info/RECORD", "")
    manifest = '[project]\nname="origin-probe"\nversion="0.0.0"\nrequires-python=">=3.11"\ndependencies=["fixture-package==1.0.0"]\n[project.optional-dependencies]\nall=[]\n[tool.uv]\nexclude-newer="14 days"\nno-index=true\nfind-links=["./wheels"]\n'
    (project / "pyproject.toml").write_text(manifest)
    (project / "uv.lock").unlink()
    env = clean_env(); env.update(UV_OFFLINE="1", UV_PYTHON_DOWNLOADS="never", UV_CACHE_DIR=str(tmp_path / "cache"), HOME=str(tmp_path / "home"), XDG_CONFIG_HOME=str(tmp_path / "xdg"))
    baseline = subprocess.run([uv, "lock", "--python", sys.executable], cwd=project, env=env, capture_output=True, text=True)
    assert baseline.returncode == 0, baseline.stderr
    lock = (project / "uv.lock").read_bytes()
    config = tmp_path / "xdg/uv"; config.mkdir(parents=True)
    (config / "uv.toml").write_text("invalid user TOML!")
    env["UV_NO_CONFIG"] = "1"
    red = subprocess.run([uv, "lock", "--check", "--python", sys.executable], cwd=project, env=env, capture_output=True, text=True)
    assert red.returncode != 0
    for mode in ("--check", "--dry-run"):
        green = subprocess.run(command(project, Path(uv)) + [mode], env=env, capture_output=True, text=True)
        assert green.returncode == 0, green.stderr
    assert (project / "pyproject.toml").read_text() == manifest
    assert (project / "uv.lock").read_bytes() == lock
    assert not list(project.glob(".ares-reviewed-uv-*"))


def test_real_uv_preserves_native_override_and_relative_source(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not installed")
    project, _ = fixture(tmp_path)
    dep = tmp_path / "local-dep"; dep.mkdir()
    (dep / "pyproject.toml").write_text('[project]\nname="local-dep"\nversion="1.0.0"\n')
    manifest = '[project]\nname="native-probe"\nversion="0.0.0"\nrequires-python=">=3.11"\ndependencies=["local-dep>=2"]\n[tool.uv]\nexclude-newer="14 days"\noverride-dependencies=["local-dep==1.0.0"]\nsources={local-dep={path="../local-dep"}}\n'
    (project / "pyproject.toml").write_text(manifest)
    (project / "uv.lock").unlink()
    env = clean_env(); env.update(UV_OFFLINE="1", UV_PYTHON_DOWNLOADS="never", UV_CACHE_DIR=str(tmp_path / "cache"))
    baseline = subprocess.run([uv, "lock", "--python", sys.executable], cwd=project, env=env, capture_output=True, text=True)
    assert baseline.returncode == 0, baseline.stderr
    before = (project / "uv.lock").read_bytes()
    env["UV_NO_CONFIG"] = "1"
    result = subprocess.run(command(project, Path(uv)) + ["--check"], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (project / "uv.lock").read_bytes() == before
    assert (project / "pyproject.toml").read_text() == manifest


@pytest.mark.parametrize("value", ["false", "0", "true", ""])
def test_age_override_never_admitted(tmp_path, value):
    project, uv = fixture(tmp_path)
    env = clean_env(); env["UV_EXCLUDE_NEWER"] = value
    result = subprocess.run(command(project, uv), env=env, capture_output=True)
    assert result.returncode == 2
    assert not (tmp_path / "record").exists()


def test_dependency_url_credentials_rejected_before_child(tmp_path):
    project, uv = fixture(tmp_path)
    data = {'project': {'dependencies': ['demo @ https://user:credential-secret@example.test/pkg']}, 'tool': {'uv': {'exclude-newer': '14 days'}}}
    with pytest.raises(policy.PolicyError, match='Inline URL') as error:
        policy.project_policy(data)
    assert 'credential-secret' not in str(error.value)


@pytest.mark.parametrize("boundary", ["mkstemp", "Popen"])
def test_signal_at_resource_ownership_boundary(tmp_path, monkeypatch, boundary):
    project, uv = fixture(tmp_path)
    env = clean_env()
    monkeypatch.setattr(os, "environ", env)
    children = []
    if boundary == "mkstemp":
        original = policy.tempfile.mkstemp
        def create(*args, **kwargs):
            owned = original(*args, **kwargs)
            os.kill(os.getpid(), signal.SIGTERM)
            return owned
        monkeypatch.setattr(policy.tempfile, "mkstemp", create)
    else:
        original = policy.subprocess.Popen
        def spawn(*args, **kwargs):
            child = original(*args, **kwargs)
            if kwargs.get("start_new_session"):
                children.append(child)
                os.kill(os.getpid(), signal.SIGTERM)
            return child
        monkeypatch.setattr(policy.subprocess, "Popen", spawn)
    assert policy.run(project, str(uv), project / "venv/bin/python") == 143
    assert all(child.poll() is not None for child in children)
    assert not list(project.glob(".ares-reviewed-uv-*"))


@pytest.mark.parametrize("module,name", [(os, "killpg"), (signal, "SIGINT"), (signal, "SIGTERM"), (signal, "SIGHUP"), (signal, "SIGKILL")])
def test_missing_posix_capability_fails_before_execution(tmp_path, monkeypatch, module, name):
    project, uv = fixture(tmp_path)
    monkeypatch.setattr(module, name, None)
    with pytest.raises(policy.PolicyError, match="capabilities are unavailable"):
        policy.run(project, str(uv), project / "venv/bin/python")
    assert not (tmp_path / "record").exists()
    assert not list(project.glob(".ares-reviewed-uv-*"))


@pytest.mark.parametrize("output,expected", [(b"uv 0.12.19 (caf\xc3\xa9)\n", 0), (b"uv 0.12.19 (bad\xff)\n", 2)])
def test_version_probe_explicit_utf8_under_ascii_locale(tmp_path, output, expected):
    project, uv = fixture(tmp_path)
    original = uv.read_text()
    # The version bytes are emitted directly, independent of the child locale.
    uv.write_text(original.replace('print("uv 0.12.19"); sys.exit(0)', f'os.write(1, {output!r}); sys.exit(0)'))
    env = clean_env(); env.update(LC_ALL="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0")
    result = subprocess.run(command(project, uv), env=env, capture_output=True)
    assert result.returncode == expected, result.stderr
    assert b"Traceback" not in result.stderr
    if expected:
        assert not (tmp_path / "record").exists()
    assert not list(project.glob(".ares-reviewed-uv-*"))


def test_unsupported_platform_contract_fails_closed():
    # Platform is explicit input to the capability guard, not a mocked host.
    with pytest.raises(policy.PolicyError, match="requires POSIX"):
        policy.posix_capabilities("nt")
