"""Only enumerated failure facts may cross the npm diagnostic boundary."""
import json
from pathlib import Path
import runpy

import pytest

api = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/npm_failure_diagnostics.py"))


def test_bounded_tail_and_known_codes_only(tmp_path):
    log = tmp_path / "raw"
    log.write_text("secret-data" * 40000 + "\nnpm ERR! code EINTEGRITY\nnpm error code ESECRET_TOKEN\n")
    value = api["summarize"](log, "root", 1, 3)
    assert value["output_truncated"] is True
    assert value["npm_codes"] == ["EINTEGRITY"]
    assert "secret" not in json.dumps(value).lower()
    assert len(json.dumps(value)) < 1024


def test_collector_revalidates_instead_of_copying_strings(tmp_path):
    safe = api["record"]("tui", 1, 0, ["E401"], False)
    evil = {**safe, "safe_causes": ["Authorization: Bearer secret-token"]}
    extra = {**safe, "raw_output": "private-token"}
    log = tmp_path / "transcript"
    log.write_text("\n".join(api["PREFIX"] + json.dumps(x) for x in [evil, extra, safe]))
    assert api["collect"](log) == [safe]


def test_collector_record_count_bound(tmp_path):
    safe = api["record"]("root", 1, 0, [], False)
    log = tmp_path / "transcript"
    log.write_text((api["PREFIX"] + json.dumps(safe) + "\n") * 50)
    assert len(api["collect"](log)) == 4


@pytest.mark.parametrize("bad", [None, [], {}, "secret", True, -1, 256])
def test_invalid_status_rejected(bad):
    with pytest.raises(ValueError):
        api["record"]("root", bad, 0, [], False)


@pytest.mark.linux_only
def test_symlink_and_fifo_inputs_rejected_without_reading(tmp_path):
    import os
    secret = tmp_path / "secret"
    secret.write_text("npm error code E401\nAuthorization: secret")
    link = tmp_path / "link"
    link.symlink_to(secret)
    with pytest.raises((ValueError, OSError)):
        api["summarize"](link, "root", 1, 0)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError):
        api["summarize"](fifo, "root", 1, 0)


def test_output_collision_does_not_replace_previous_run(tmp_path):
    import subprocess
    import sys
    log = tmp_path / "log"
    log.write_text("")
    output = tmp_path / "artifact"
    output.write_text("previous run")
    result = subprocess.run([sys.executable, api["__file__"], "collect", "--input", str(log),
                             "--output", str(output)], capture_output=True, text=True)
    assert result.returncode == 1
    assert output.read_text() == "previous run"
    assert result.stderr.strip() == "Sanitized npm diagnostics unavailable"


def test_two_invocations_under_reused_parent_have_distinct_current_artifacts(tmp_path):
    import subprocess
    import sys
    parent = tmp_path / "reused-e2e-log-dir"
    directories = []
    for code in [37, 124]:
        made = subprocess.run([sys.executable, api["__file__"], "new-run", "--parent", str(parent)],
                              check=True, capture_output=True, text=True)
        run = Path(made.stdout.strip())
        directories.append(run)
        assert run.parent == parent
        value = api["record"]("root", code, 2, ["ERESOLVE"], False)
        transcript = run / "reinstall.log"
        transcript.write_text(api["PREFIX"] + json.dumps(value) + "\n")
        subprocess.run([sys.executable, api["__file__"], "collect", "--input", str(transcript),
                        "--output", str(run / "npm-diagnostics-reinstall.json")], check=True)
        assert json.loads((run / "npm-diagnostics-reinstall.json").read_text()) == [value]
    assert directories[0] != directories[1]
    assert json.loads((directories[0] / "npm-diagnostics-reinstall.json").read_text())[0]["exit_code"] == 37
    assert json.loads((directories[1] / "npm-diagnostics-reinstall.json").read_text())[0]["exit_code"] == 124
