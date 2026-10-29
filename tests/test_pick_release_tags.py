"""Behavior tests for ``scripts/sandbox/pick-release-tags.sh``.

The install/update E2E narrows its ``hermes update`` route to release tags
whose Git creator date is on or after a cutoff date. That filter decides which
legs run, so it is asserted against a real temporary Git repository with
controlled tag dates rather than by reading the script's source.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PICK = REPO_ROOT / "scripts" / "sandbox" / "pick-release-tags.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="needs bash and git",
)


def _git(repo: Path, *args: str, date: str | None = None) -> None:
    env = None
    if date is not None:
        import os

        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture()
def tagged_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    for tag, date in (
        ("v2026.1.5", "2026-01-05T12:00:00"),
        ("v2026.3.10", "2026-03-10T12:00:00"),
        ("v2026.6.19", "2026-06-19T12:00:00"),
    ):
        _git(repo, "commit", "--allow-empty", "-m", tag, date=date)
        _git(repo, "tag", "-a", tag, "-m", tag, date=date)
    # Non-release tags must never enter the matrix.
    _git(repo, "tag", "backup/2026-01-01")
    return repo


def _pick(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(PICK), "--repo", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_unfiltered_picks_evenly_spaced_release_tags(tagged_repo: Path) -> None:
    proc = _pick(tagged_repo, "--count", "3")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ["v2026.1.5", "v2026.3.10", "v2026.6.19"]


def test_since_date_excludes_earlier_releases(tagged_repo: Path) -> None:
    proc = _pick(tagged_repo, "--count", "5", "--since-date", "2026-03-10")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ["v2026.3.10", "v2026.6.19"]


def test_since_date_with_no_eligible_release_emits_empty_matrix(tagged_repo: Path) -> None:
    proc = _pick(tagged_repo, "--count", "5", "--since-date", "2026-10-29")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == []
    assert "no release tags with a Git creator date on or after" in proc.stderr


def test_malformed_since_date_is_rejected(tagged_repo: Path) -> None:
    proc = _pick(tagged_repo, "--since-date", "2026-3-1")
    assert proc.returncode != 0
    assert "YYYY-MM-DD" in proc.stderr


def test_no_tags_without_filter_still_fails(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    _git(repo, "init", "-q")
    proc = _pick(repo, "--count", "3")
    assert proc.returncode != 0
    assert "no release tags found" in proc.stderr
