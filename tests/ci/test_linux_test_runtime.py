"""Linux test execution uses the operator-approved Python 3.14 lane."""
from pathlib import Path
import shlex

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("job_name", ["test", "e2e"])
def test_linux_test_jobs_install_and_sync_python314(job_name):
    # BaseLoader preserves GitHub's `on` key and scalar expression text.
    workflow = yaml.load(
        (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    job = workflow["jobs"][job_name]
    commands = [
        shlex.split(command)
        for step in job["steps"]
        for command in [step.get("run", step.get("with", {}).get("command", ""))]
        if command.startswith("uv ")
    ]
    installs = [command for command in commands if command[:3] == ["uv", "python", "install"]]
    syncs = [command for command in commands if command[:2] == ["uv", "sync"]]
    assert installs == [["uv", "python", "install", "3.14"]]
    assert len(syncs) == 1
    sync = syncs[0]
    assert "--locked" in sync
    assert sync[sync.index("--python") + 1] == "3.14"
    assert job.get("continue-on-error", "false") == "false"
    runner = next(step for step in job["steps"] if step.get("name", "").startswith("Run "))
    assert runner.get("continue-on-error", "false") == "false"
    assert "source .venv/bin/activate" in runner["run"]
