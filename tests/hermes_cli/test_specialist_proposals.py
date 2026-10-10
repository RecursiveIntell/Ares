"""P04 inert specialist CLI startup and coverage-capture contracts."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import pty
import select
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from ares_runtime.collaboration import canonical_json


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _entrypoint(name: str, args: list[str]) -> list[str]:
    if name == "console":
        # This is the source checkout's console launcher. Its target is the
        # same hermes_cli.main:main entry declared in pyproject.toml.
        return [sys.executable, str(REPO_ROOT / "hermes"), *args]
    if name == "console-x-option":
        return [sys.executable, "-X", "utf8", str(REPO_ROOT / "hermes"), *args]
    if name == "module":
        return [sys.executable, "-m", "hermes_cli.main", *args]
    if name == "module-x-option":
        return [sys.executable, "-X", "utf8", "-m", "hermes_cli.main", *args]
    if name == "installed-console":
        executable = pathlib.Path(sys.executable).parent / (
            "hermes.exe" if os.name == "nt" else "hermes"
        )
        if not executable.is_file():
            pytest.skip("installed Hermes console script is unavailable")
        return [str(executable), *args]
    raise AssertionError(name)


def _make_probe_environment(
    tmp_path: pathlib.Path, *, tui: bool = True
) -> tuple[dict[str, str], pathlib.Path]:
    home = tmp_path / "ambient-home"
    hermes_home = home / ".hermes"
    xdg_config = tmp_path / "xdg-config"
    xdg_cache = tmp_path / "xdg-cache"
    xdg_data = tmp_path / "xdg-data"
    probe_dir = tmp_path / "probe"
    for directory in (home, hermes_home, xdg_config, xdg_cache, xdg_data, probe_dir):
        directory.mkdir(parents=True, exist_ok=True)

    # These markers make accidental ambient resolution observable while
    # remaining synthetic test fixtures. The open/stat tripwires below record
    # any attempt to consult them.
    for base in (hermes_home, home / ".ares"):
        base.mkdir(parents=True, exist_ok=True)
        (base / "config.yaml").write_text(
            "display:\n  interface: tui\n", encoding="utf-8"
        )
        (base / "active_profile").write_text("default\n", encoding="utf-8")

    event_path = tmp_path / "probe-events.jsonl"
    probe_source = f"""\
import atexit
import builtins
import importlib.abc
import io
import json
import os
import socket
import subprocess
import sys

_EVENTS = {str(event_path)!r}
_WATCH_ROOTS = [{str(home)!r}, {str(xdg_config)!r}, {str(xdg_cache)!r}, {str(xdg_data)!r}]
_WATCH_ENV = ("PYTHONUTF8", "PYTHONIOENCODING", "HERMES_HOME", "HERMES_AGENT", "AI_AGENT", "HERMES_TUI")
_ENV_BEFORE = {{key: os.environ.get(key) for key in _WATCH_ENV}}
_OPEN = builtins.open
_IO_OPEN = io.open
_OS_OPEN = os.open
_STAT = os.stat
_LISTDIR = os.listdir
_SCANDIR = os.scandir

def _record(kind, detail=None):
    row = {{"kind": kind, "detail": detail}}
    with _OPEN(_EVENTS, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=True, sort_keys=True) + "\\n")

def _under_watch(path):
    try:
        value = os.path.abspath(os.fsdecode(os.fspath(path)))
        for root in _WATCH_ROOTS:
            if os.path.commonpath((value, root)) == root:
                return True
    except (TypeError, ValueError, OSError):
        pass
    return False

def _watched_open(path, mode):
    if _under_watch(path):
        _record("ambient_open", {{"mode": mode}})

def _open(file, mode="r", *args, **kwargs):
    _watched_open(file, mode)
    return _OPEN(file, mode, *args, **kwargs)

def _io_open(file, mode="r", *args, **kwargs):
    _watched_open(file, mode)
    return _IO_OPEN(file, mode, *args, **kwargs)

def _os_open(file, flags, mode=0o777, *, dir_fd=None):
    if dir_fd is None:
        _watched_open(file, "os.open")
    return _OS_OPEN(file, flags, mode, dir_fd=dir_fd) if dir_fd is not None else _OS_OPEN(file, flags, mode)

def _stat(path, *args, **kwargs):
    if _under_watch(path):
        _record("ambient_stat")
    return _STAT(path, *args, **kwargs)

def _listdir(path="."):
    if _under_watch(path):
        _record("ambient_listdir")
    return _LISTDIR(path)

def _scandir(path="."):
    if _under_watch(path):
        _record("ambient_scandir")
    return _SCANDIR(path)

builtins.open = _open
io.open = _io_open
os.open = _os_open
os.stat = _stat
os.listdir = _listdir
os.scandir = _scandir
os.supports_dir_fd = set(os.supports_dir_fd) | {{_os_open, _stat}}
os.supports_follow_symlinks = set(os.supports_follow_symlinks) | {{_stat}}
os.supports_fd = set(os.supports_fd) | {{_listdir, _scandir}}

class _ImportProbe(importlib.abc.MetaPathFinder):
    _watched = {{
        "hermes_cli.main", "hermes_cli._startup_fast", "hermes_cli._early_recovery",
        "hermes_cli.config", "hermes_cli.env_loader", "hermes_cli.stdio",
    }}
    def find_spec(self, fullname, path=None, target=None):
        if fullname in self._watched:
            _record("ambient_import", fullname)
        return None

sys.meta_path.insert(0, _ImportProbe())

class _StreamProbe:
    def __init__(self, stream):
        self._stream = stream
    @property
    def encoding(self):
        return "ascii"
    def reconfigure(self, **kwargs):
        _record("stdio_reconfigure", sorted(kwargs))
        return self._stream.reconfigure(**kwargs)
    def __getattr__(self, name):
        return getattr(self._stream, name)

sys.stdout = _StreamProbe(sys.stdout)
sys.stderr = _StreamProbe(sys.stderr)

def _blocked_process(*args, **kwargs):
    _record("process_spawn")
    raise RuntimeError("process spawn blocked by P04 test probe")

subprocess.Popen = _blocked_process
os.system = _blocked_process

_REAL_SOCKET = socket.socket
class _SocketProbe(_REAL_SOCKET):
    def connect(self, *args, **kwargs):
        _record("network_socket")
        raise RuntimeError("network blocked by P04 test probe")
    def connect_ex(self, *args, **kwargs):
        _record("network_socket")
        raise RuntimeError("network blocked by P04 test probe")

socket.socket = _SocketProbe

def _record_environment_changes():
    after = {{key: os.environ.get(key) for key in _WATCH_ENV}}
    changed = {{key: [ _ENV_BEFORE[key], after[key] ] for key in _WATCH_ENV if _ENV_BEFORE[key] != after[key]}}
    if changed:
        _record("environment_change", changed)

atexit.register(_record_environment_changes)
"""
    (probe_dir / "sitecustomize.py").write_text(probe_source, encoding="utf-8")

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "HERMES_HOME": str(hermes_home),
        "XDG_CONFIG_HOME": str(xdg_config),
        "XDG_CACHE_HOME": str(xdg_cache),
        "XDG_DATA_HOME": str(xdg_data),
        "TMPDIR": str(tmp_path),
        "PYTHONPATH": os.pathsep.join((str(probe_dir), str(REPO_ROOT))),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONCOLORS": "0",
        "NO_COLOR": "1",
        "LANG": "C",
        "LC_ALL": "C",
        "TERM": "xterm-256color",
        "ARES_P04_STERILE_PROBE": "1",
    }
    if tui:
        env["HERMES_TUI"] = "1"
    return env, event_path


def _read_events(event_path: pathlib.Path) -> list[dict[str, object]]:
    if not event_path.exists():
        return []
    return [
        json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()
    ]


def _tree_digest(root: pathlib.Path) -> list[tuple[str, int, str]]:
    if not root.exists():
        return []
    rows = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            rows.append((rel, -1, "symlink"))
        elif path.is_file():
            raw = path.read_bytes()
            rows.append((rel, len(raw), hashlib.sha256(raw).hexdigest()))
        elif path.is_dir():
            rows.append((rel, 0, "directory"))
    return rows


@contextmanager
def _recovery_markers() -> Iterator[dict[pathlib.Path, bytes]]:
    marker_contents = {
        REPO_ROOT / ".update-incomplete": b"synthetic P04 recovery marker\n",
        REPO_ROOT / ".lazy-refresh-incomplete": b"synthetic P04 lazy marker\n",
    }
    assert all(not path.exists() and not path.is_symlink() for path in marker_contents)
    for path, raw in marker_contents.items():
        path.write_bytes(raw)
    try:
        yield marker_contents
    finally:
        for path in marker_contents:
            path.unlink(missing_ok=True)
        (REPO_ROOT / ".update-incomplete.lock").unlink(missing_ok=True)


def _run_pty(
    command: list[str], env: dict[str, str], *, timeout: float = 8.0
) -> subprocess.CompletedProcess[str]:
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )
    os.close(slave_fd)
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout
    timed_out = False
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master_fd], [], [], 0.05)
        if ready:
            try:
                chunk = os.read(master_fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        if proc.poll() is not None:
            # Drain any bytes already queued in the PTY before returning.
            while True:
                ready, _, _ = select.select([master_fd], [], [], 0)
                if not ready:
                    break
                try:
                    chunk = os.read(master_fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            break
    else:
        timed_out = True
    if timed_out:
        proc.kill()
    try:
        returncode = proc.wait(timeout=2)
    finally:
        os.close(master_fd)
    text = b"".join(chunks).decode("utf-8", errors="replace")
    if timed_out:
        text += "\nP04 test subprocess timed out\n"
    return subprocess.CompletedProcess(command, returncode, stdout=text, stderr="")


def _role_fact() -> dict[str, object]:
    return {
        "semantic_role_id": "role.existing-research",
        "descriptor_status": "valid",
        "descriptor_ref": "descriptor:existing-research",
        "binding_status": "unbound",
        "binding_ref": None,
        "index_status": "unindexed",
        "enabled_state": "disabled",
        "availability": "unknown",
        "semantic_coverage": "not_covered",
    }


def _scope(root: pathlib.Path) -> dict[str, object]:
    source_dir = root / "sources"
    profile_dir = root / "profiles" / "alpha"
    source_dir.mkdir(parents=True)
    profile_dir.mkdir(parents=True)
    source_ref = "source:profile-registry"
    global_ref = "file:profile-registry"
    global_path = "sources/profile_registry.json"
    profile_ref = "file:alpha:profile-registry"
    profile_path = "profiles/alpha/profile_registry.json"
    (root / global_path).write_bytes(
        canonical_json({"profiles": ["alpha"], "kind": "profile_registry"})
    )
    (root / profile_path).write_bytes(
        canonical_json({"id": "alpha", "kind": "profile_registry"})
    )
    return {
        "schema_version": "1.0.0",
        "scope_ref": "scope:synthetic-p04",
        "description": "One synthetic profile registry explicitly rooted in this fixture.",
        "source_revision": "synthetic-rev-p04",
        "cutoff": "synthetic-cutoff-p04",
        "approved_root": str(root),
        "roster_root": "profiles",
        "roster_source_ref": source_ref,
        "sources": [
            {
                "source_ref": source_ref,
                "source_kind": "profile_registry",
                "revision": "synthetic-r1",
                "cutoff": "synthetic-cutoff-p04",
                "file_ref": global_ref,
                "relative_path": global_path,
                "profile_ids_field": "profiles",
            }
        ],
        "profile_files": [
            {
                "profile_id": "alpha",
                "source_ref": source_ref,
                "file_ref": profile_ref,
                "relative_path": profile_path,
                "format": "json",
            }
        ],
        "source_profile_applicability": [
            {
                "profile_id": "alpha",
                "source_ref": source_ref,
                "applicability": "required",
                "file_ref": profile_ref,
                "basis": "Reviewed synthetic applicability for this source/profile pair.",
                "basis_refs": [source_ref],
            }
        ],
        "profile_assessments": [
            {
                "profile_id": "alpha",
                "roles": [_role_fact()],
                "exclusions": ["No absence inference from names or keywords."],
            }
        ],
    }


@pytest.mark.linux_only
@pytest.mark.parametrize(
    "entrypoint",
    [
        "console",
        "console-x-option",
        "installed-console",
        "module",
        "module-x-option",
    ],
)
def test_specialist_help_is_inert_before_utf8_recovery_profile_and_tui_startup(
    tmp_path: pathlib.Path, entrypoint: str
) -> None:
    env, event_path = _make_probe_environment(tmp_path, tui=True)
    ambient_roots = [
        pathlib.Path(env["HOME"]),
        pathlib.Path(env["XDG_CONFIG_HOME"]),
        pathlib.Path(env["XDG_CACHE_HOME"]),
        pathlib.Path(env["XDG_DATA_HOME"]),
    ]
    before = {path: _tree_digest(path) for path in ambient_roots}
    with _recovery_markers() as markers:
        result = _run_pty(_entrypoint(entrypoint, ["specialists", "--help"]), env)
        assert result.returncode == 0, result.stdout
        assert "capture-coverage" in result.stdout
        assert "replay" in result.stdout
        help_commands = {
            line.strip().split()[0]
            for line in result.stdout.splitlines()
            if line.startswith("  ") and line.strip()
        }
        assert not ({"create", "run"} & help_commands)
        assert "P04 test subprocess timed out" not in result.stdout
        assert "\x1b[?1049h" not in result.stdout
        assert "\x1b[2J" not in result.stdout
        assert {path: _tree_digest(path) for path in ambient_roots} == before
        assert {path: path.read_bytes() for path in markers} == markers
        assert not (REPO_ROOT / ".update-incomplete.lock").exists()

    events = _read_events(event_path)
    forbidden = {
        "ambient_open",
        "ambient_stat",
        "ambient_listdir",
        "ambient_scandir",
        "stdio_reconfigure",
        "environment_change",
        "process_spawn",
        "network_socket",
    }
    assert not [event for event in events if event["kind"] in forbidden], events
    forbidden_imports = {
        "hermes_cli.main",
        "hermes_cli._startup_fast",
        "hermes_cli._early_recovery",
        "hermes_cli.config",
        "hermes_cli.env_loader",
        "hermes_cli.stdio",
    }
    assert not [
        event
        for event in events
        if event["kind"] == "ambient_import" and event["detail"] in forbidden_imports
    ], events


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--model", "specialists", "--help"], "Hermes Agent"),
        (["--mod", "specialists", "--help"], "Hermes Agent"),
        (["--continue", "specialists", "--help"], "Hermes Agent"),
        (["-p", "specialists", "--help"], "Hermes Agent"),
    ],
)
def test_option_values_named_specialists_remain_values(
    tmp_path: pathlib.Path, args: list[str], expected: str
) -> None:
    env, _ = _make_probe_environment(tmp_path, tui=False)
    profile_home = pathlib.Path(env["HERMES_HOME"])
    (profile_home / "profiles" / "specialists").mkdir(parents=True)
    (pathlib.Path(env["HOME"]) / ".ares" / "profiles" / "specialists").mkdir(
        parents=True
    )
    result = subprocess.run(
        _entrypoint("module", args),
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout
    assert "capture-coverage" not in result.stdout


def test_non_hermes_program_import_does_not_consume_specialists_argument(
    tmp_path: pathlib.Path,
) -> None:
    env, event_path = _make_probe_environment(tmp_path, tui=False)
    ambient_roots = [
        pathlib.Path(env["HOME"]),
        pathlib.Path(env["XDG_CONFIG_HOME"]),
        pathlib.Path(env["XDG_CACHE_HOME"]),
        pathlib.Path(env["XDG_DATA_HOME"]),
    ]
    before = {path: _tree_digest(path) for path in ambient_roots}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import hermes_cli; print('ordinary-import-survived')",
            "specialists",
            "--help",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "ordinary-import-survived" in result.stdout
    assert "capture-coverage" not in result.stdout
    assert {path: _tree_digest(path) for path in ambient_roots} == before
    events = _read_events(event_path)
    assert not [
        event
        for event in events
        if event["kind"]
        in {"ambient_open", "ambient_stat", "ambient_listdir", "ambient_scandir"}
    ], events
    assert not [
        event
        for event in events
        if event["kind"] == "ambient_import" and event["detail"] == "hermes_cli.main"
    ], events


@pytest.mark.parametrize(
    ("prefix", "expected_flag"),
    [
        (["--model", "fictional-model"], "--model"),
        (["--mod", "fictional-model"], "--model"),
        (["--continue", "synthetic-session"], "--continue"),
        (["-p", "specialists"], "-p"),
    ],
)
@pytest.mark.parametrize(
    "entrypoint", ["console", "console-x-option", "installed-console", "module"]
)
def test_real_specialist_command_after_global_option_value_refuses_before_startup(
    tmp_path: pathlib.Path, prefix: list[str], expected_flag: str, entrypoint: str
) -> None:
    env, event_path = _make_probe_environment(tmp_path, tui=True)
    with _recovery_markers() as markers:
        result = _run_pty(
            _entrypoint(
                entrypoint,
                [*prefix, "specialists", "show", "--proposal", "missing.json"],
            ),
            env,
        )
        assert result.returncode != 0
        assert expected_flag in result.stdout
        assert "unsupported" in result.stdout.lower()
        assert "P04 test subprocess timed out" not in result.stdout
        assert {path: path.read_bytes() for path in markers} == markers
        assert not (REPO_ROOT / ".update-incomplete.lock").exists()

    events = _read_events(event_path)
    assert not [
        event
        for event in events
        if event["kind"]
        in {
            "ambient_open",
            "ambient_stat",
            "ambient_listdir",
            "ambient_scandir",
            "stdio_reconfigure",
            "environment_change",
            "process_spawn",
            "network_socket",
        }
    ], events
    assert not [event for event in events if event["kind"] == "ambient_import"], events


@pytest.mark.linux_only
def test_capture_coverage_uses_only_the_explicit_scope_and_immutable_output(
    tmp_path: pathlib.Path,
) -> None:
    env, _ = _make_probe_environment(tmp_path, tui=False)
    approved_root = tmp_path / "approved-root"
    approved_root.mkdir()
    scope = _scope(approved_root)
    scope_file = tmp_path / "scope.json"
    scope_file.write_bytes(canonical_json(scope))
    output = tmp_path / "coverage.json"
    command = _entrypoint(
        "module",
        [
            "specialists",
            "capture-coverage",
            "--scope",
            str(scope_file),
            "--out",
            str(output),
        ],
    )

    first = subprocess.run(
        command, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30
    )
    assert first.returncode == 0, first.stderr
    first_bytes = output.read_bytes()
    assert json.loads(first_bytes)["schema_version"] == "1.0.0"
    assert output.stat().st_mode & 0o077 == 0

    second = subprocess.run(
        command, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30
    )
    assert second.returncode == 0, second.stderr
    assert output.read_bytes() == first_bytes

    output.chmod(0o644)
    non_private_duplicate = subprocess.run(
        command, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30
    )
    assert non_private_duplicate.returncode != 0
    assert output.read_bytes() == first_bytes
    assert output.stat().st_mode & 0o077 == 0o044
    output.chmod(0o600)
    private_duplicate = subprocess.run(
        command, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30
    )
    assert private_duplicate.returncode == 0, private_duplicate.stderr
    assert output.read_bytes() == first_bytes

    conflict = tmp_path / "conflict.json"
    conflict.write_bytes(b"preserve-existing-bytes\n")
    conflict_run = subprocess.run(
        _entrypoint(
            "module",
            [
                "specialists",
                "capture-coverage",
                "--scope",
                str(scope_file),
                "--out",
                str(conflict),
            ],
        ),
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert conflict_run.returncode != 0
    assert conflict.read_bytes() == b"preserve-existing-bytes\n"


@pytest.mark.parametrize("entrypoint", ["installed-console"])
def test_invalid_specialist_arguments_do_not_echo_absolute_paths(
    tmp_path: pathlib.Path, entrypoint: str
) -> None:
    env, _ = _make_probe_environment(tmp_path, tui=False)
    secret_path = tmp_path / "sensitive-user-path-sentinel"
    result = subprocess.run(
        _entrypoint(
            entrypoint,
            [
                "specialists",
                "show",
                "--proposal",
                str(tmp_path / "proposal.json"),
                "--unexpected",
                str(secret_path),
            ],
        ),
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "INVALID_ARGUMENTS" in result.stderr
    assert str(secret_path) not in result.stdout + result.stderr


@pytest.mark.linux_only
def test_capture_refuses_symlinks_and_missing_inputs_without_creating_output(
    tmp_path: pathlib.Path,
) -> None:
    env, _ = _make_probe_environment(tmp_path, tui=False)
    approved_root = tmp_path / "approved-root"
    approved_root.mkdir()
    scope = _scope(approved_root)
    scope_file = tmp_path / "scope.json"
    scope_file.write_bytes(canonical_json(scope))
    input_link = tmp_path / "scope-link.json"
    input_link.symlink_to(scope_file)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"untouched\n")
    output_link = tmp_path / "coverage-link.json"
    output_link.symlink_to(outside)

    for scope_arg, out_arg in [
        (str(input_link), str(tmp_path / "input-link-output.json")),
        (str(scope_file), str(output_link)),
        (str(tmp_path / "missing-scope.json"), str(tmp_path / "missing-output.json")),
    ]:
        result = subprocess.run(
            _entrypoint(
                "module",
                [
                    "specialists",
                    "capture-coverage",
                    "--scope",
                    scope_arg,
                    "--out",
                    out_arg,
                ],
            ),
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0
        assert (
            not pathlib.Path(out_arg).exists()
            if out_arg != str(output_link)
            else output_link.is_symlink()
        )
    assert outside.read_bytes() == b"untouched\n"


@pytest.mark.parametrize("command", ["create", "run", "activate"])
def test_cli_rejects_activation_and_unknown_commands(
    tmp_path: pathlib.Path, command: str
) -> None:
    env, _ = _make_probe_environment(tmp_path, tui=False)
    output_dir = tmp_path / "private-output"
    result = subprocess.run(
        _entrypoint("module", ["specialists", command]),
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert not output_dir.exists()
