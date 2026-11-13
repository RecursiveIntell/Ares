"""Tests for scripts/ci/check_owner_pin_drift.py.

Each test builds a small owner workspace as a local "origin", takes a shallow
checkout of it (the pin), advances origin's ``main``, and runs the real script
as a subprocess.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "check_owner_pin_drift.py"

PASS, DRIFT, ERROR = 0, 1, 2


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        [
            "git",
            "-c", "user.name=test",
            "-c", "user.email=test@example.invalid",
            "-c", "commit.gpgsign=false",
            "-C", str(cwd),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _crate(root: Path, name: str, extra: str = "") -> None:
    _write(root, f"{name}/Cargo.toml", f'[package]\nname = "{name}"\nversion = "0.1.0"\n{extra}')
    _write(root, f"{name}/src/lib.rs", "// v1\n")


@pytest.fixture
def owner(tmp_path: Path):
    """origin with crates: a -> b (path), a -> d (workspace path), b -> e (dev, not a member); c unrelated."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _write(
        origin,
        "Cargo.toml",
        '[workspace]\nmembers = ["a", "b", "c", "d"]\n\n[workspace.dependencies]\nd = { path = "d" }\n',
    )
    _write(origin, "Cargo.lock", "# lock v1\n")
    _crate(origin, "a", '\n[dependencies]\nb = { path = "../b" }\nd = { workspace = true }\n')
    _crate(origin, "b", '\n[dev-dependencies]\ne = { path = "../e" }\n')
    _crate(origin, "c")
    _crate(origin, "d")
    _crate(origin, "e")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "initial")
    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{origin}", str(checkout)],
        check=True,
        capture_output=True,
    )

    class Owner:
        def advance(self, rel: str, text: str) -> str:
            _write(origin, rel, text)
            _git(origin, "add", "-A")
            _git(origin, "commit", "-q", "-m", f"change {rel}")
            return _git(origin, "rev-parse", "HEAD")

        def run(self, *crate_dirs: str, extra: tuple[str, ...] = ()):
            args = [sys.executable, str(SCRIPT), "--libraries", str(checkout), "--fetch-filter", ""]
            for crate in crate_dirs or ("a",):
                args += ["--crate-dir", crate]
            args += list(extra)
            return subprocess.run(args, capture_output=True, text=True)

        checkout_path = checkout

    return Owner()


def test_pin_equal_to_current_tip_passes(owner):
    result = owner.run()
    assert result.returncode == PASS, result.stderr
    assert "current tip" in result.stdout


def test_change_outside_the_consumed_closure_passes(owner):
    owner.advance("c/src/lib.rs", "// unrelated\n")
    result = owner.run()
    assert result.returncode == PASS, result.stderr
    assert "no consumed path changed" in result.stdout


def test_markdown_only_change_inside_the_closure_passes(owner):
    owner.advance("b/README.md", "# docs only\n")
    result = owner.run()
    assert result.returncode == PASS, result.stderr


def test_markdown_change_does_not_mask_a_source_change(owner):
    owner.advance("b/README.md", "# docs only\n")
    owner.advance("b/src/lib.rs", "// v2\n")
    result = owner.run()
    assert result.returncode == DRIFT
    assert "b/src/lib.rs" in result.stderr
    assert "README.md" not in result.stderr


def test_closure_lists_transitive_and_inherited_path_dependencies(owner):
    result = owner.run()
    assert "(4):" in result.stdout
    for crate in ("a", "b", "d", "e"):
        assert crate in result.stdout.split("):", 1)[1].split("\n", 1)[0]
    assert ", c" not in result.stdout.split("):", 1)[1].split("\n", 1)[0]


def test_change_in_direct_path_dependency_fails(owner):
    tip = owner.advance("b/src/lib.rs", "// v2\n")
    result = owner.run()
    assert result.returncode == DRIFT
    assert "changed since pin: b/src/lib.rs" in result.stderr
    assert tip in result.stderr


def test_change_in_transitive_dev_dependency_fails(owner):
    owner.advance("e/src/lib.rs", "// v2\n")
    result = owner.run()
    assert result.returncode == DRIFT
    assert "e/src/lib.rs" in result.stderr


def test_change_in_workspace_inherited_dependency_fails(owner):
    owner.advance("d/src/lib.rs", "// v2\n")
    result = owner.run()
    assert result.returncode == DRIFT
    assert "d/src/lib.rs" in result.stderr


@pytest.mark.parametrize("rel", ["Cargo.lock", "Cargo.toml"])
def test_workspace_level_resolution_files_fail(owner, rel):
    text = "# lock v2\n" if rel == "Cargo.lock" else (
        '[workspace]\nmembers = ["a", "b", "c", "d"]\nresolver = "2"\n\n'
        '[workspace.dependencies]\nd = { path = "d" }\n'
    )
    owner.advance(rel, text)
    result = owner.run()
    assert result.returncode == DRIFT
    assert rel in result.stderr


def test_unknown_crate_directory_is_an_operational_error(owner):
    result = owner.run("does-not-exist")
    assert result.returncode == ERROR
    assert "does not exist" in result.stderr


def test_unreachable_remote_fails_closed(owner):
    result = owner.run(extra=("--remote", "no-such-remote"))
    assert result.returncode == ERROR
    assert "ERROR:" in result.stderr


def test_missing_checkout_is_an_operational_error(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--libraries", str(tmp_path / "nope"), "--crate-dir", "a"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == ERROR
