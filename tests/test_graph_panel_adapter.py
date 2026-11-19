"""Failing-first witnesses for the graph panel adapter (PANEL card, P3).

The adapter must translate ONE graph operation into ONE explicit profile-panel
run through the installed runner's argv contract — never auto-run the full
panel, never rewrite a profile, never substitute a blocked route.
"""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ares_runtime.graph_panel_adapter import (
    GraphPanelAdapter,
    PanelAdmissionError,
    build_panel_argv,
)

REAL_HOME = Path("/home/sikmindz")
RUNNER = REAL_HOME / ".ares/skills/productivity/profile-collaboration/scripts/run_panel.py"


def test_argv_binds_one_explicit_profile_workspace_brief_and_run_id(tmp_path):
    argv = build_panel_argv(
        runner=RUNNER,
        profile="explorer",
        workspace=tmp_path / "work",
        brief="Review the frozen F1 packet.",
        out_dir=tmp_path / "receipts" / "run-1",
        author_family="openai",
    )
    assert argv[0] == sys.executable or argv[0].endswith("python3")
    assert str(RUNNER) in argv
    assert "--profiles" in argv
    assert argv[argv.index("--profiles") + 1] == "explorer"
    assert "--full-panel" not in argv
    assert argv[argv.index("--workspace") + 1] == str(tmp_path / "work")
    assert argv[argv.index("--brief") + 1] == "Review the frozen F1 packet."
    assert argv[argv.index("--out") + 1] == str(tmp_path / "receipts" / "run-1")
    assert argv[argv.index("--author-family") + 1] == "openai"


def test_full_panel_escalation_is_refused_by_the_adapter(tmp_path):
    with pytest.raises(PanelAdmissionError, match="full_panel_not_permitted"):
        build_panel_argv(
            runner=RUNNER, profile="*", workspace=tmp_path, brief="x",
            out_dir=tmp_path / "out", author_family="openai",
        )


def test_unknown_or_retired_profile_is_blocked_before_spawn(tmp_path):
    adapter = GraphPanelAdapter(
        runner=RUNNER,
        policy_root=REAL_HOME / ".ares/specialist-system",
    )
    for bad in ("evidence-synthesizer", "not-a-profile", ""):
        with pytest.raises(PanelAdmissionError):
            adapter.admit(profile=bad, workspace=tmp_path, brief="x",
                          out_dir=tmp_path / "out", author_family="openai")


def test_blocked_runner_exit_is_typed_not_substituted(tmp_path):
    adapter = GraphPanelAdapter(runner=RUNNER, policy_root=REAL_HOME / ".ares/specialist-system")
    completed = subprocess.CompletedProcess(args=[], returncode=3, stdout="", stderr="policy denied")
    with patch("ares_runtime.graph_panel_adapter.subprocess.run", return_value=completed):
        result = adapter.run(profile="explorer", workspace=tmp_path, brief="x",
                             out_dir=tmp_path / "out", author_family="openai")
    assert result["status"] == "blocked"
    assert result["exit_code"] == 3
    assert "substitute" not in json.dumps(result).lower() or result.get("substituted") is False


def test_changed_descriptor_digest_blocks_admission(tmp_path):
    # Tamper with a COPY of the policy root: the declared manifest digest no
    # longer matches the descriptor bytes. Live policy files stay untouched.
    import shutil
    fake_root = tmp_path / "policy"
    shutil.copytree(REAL_HOME / ".ares/specialist-system", fake_root)
    manifest = json.loads((fake_root / "POLICY_MANIFEST.json").read_text())
    descriptor = fake_root / manifest["descriptor_manifest"]["path"]
    descriptor.write_bytes(descriptor.read_bytes() + b"\n# tampered\n")
    adapter = GraphPanelAdapter(runner=RUNNER, policy_root=fake_root)
    with pytest.raises(PanelAdmissionError, match="descriptor"):
        adapter.admit(profile="explorer", workspace=tmp_path, brief="x",
                      out_dir=tmp_path / "out", author_family="openai")


def test_successful_run_returns_receipt_locator_for_canonical_verification(tmp_path):
    adapter = GraphPanelAdapter(runner=RUNNER, policy_root=REAL_HOME / ".ares/specialist-system")
    out = tmp_path / "out"
    out.mkdir()
    (out / "receipt.json").write_text("{}")
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
    with patch("ares_runtime.graph_panel_adapter.subprocess.run", return_value=completed):
        result = adapter.run(profile="explorer", workspace=tmp_path, brief="x",
                             out_dir=out, author_family="openai")
    assert result["status"] == "completed"
    assert result["receipt_dir"] == str(out)
    assert result["verify_argv"][-2:] == ["--receipt", str(out)] or "--receipt" in result["verify_argv"]
