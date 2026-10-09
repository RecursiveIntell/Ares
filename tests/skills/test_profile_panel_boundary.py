"""Call the optional panel owners with inert children and synthetic grants."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent.secret_scope import ProfileEnvBoundary

SCRIPT = Path(__file__).resolve().parents[2] / "optional-skills/productivity/profile-collaboration/scripts/run_panel.py"
spec = importlib.util.spec_from_file_location("panel_boundary_under_test", SCRIPT)
panel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(panel)


class PanelBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory()
        self.addCleanup(self.fixture.cleanup)
        self.home = Path(self.fixture.name)
        self.root = self.home / ".ares"
        self.source = self.root / "profiles/controller"
        self.target = self.root / "profiles/public"
        self.target.mkdir(parents=True)
        self.runtime = self.home / "runtime"
        self.runtime.mkdir()
        self.workspace = self.home / "workspace"
        self.workspace.mkdir()
        self.receipts = self.home / "receipts"
        self.calls = []

    def invoke(self, *, target_values=None):
        boundary = ProfileEnvBoundary(self.source, self.target, frozenset({"OPENROUTER_API_KEY", "SOURCE_CUSTOM_TOKEN"}), target_values or {})
        class Child:
            returncode = 0
            def communicate(self, **kwargs):
                return b"advisory output", b""
        def capture(command, **options):
            self.calls.append((command, options))
            return Child()
        with patch.dict(os.environ, {"HOME": str(self.home), "HERMES_HOME": str(self.source), "PATH": "/usr/bin:/bin", "HERMES_TUI": "1", "OPENROUTER_API_KEY": "synthetic-source", "SOURCE_CUSTOM_TOKEN": "synthetic-private", "GH_TOKEN": "synthetic-gh", "BENIGN_TOOL_SETTING": "keep"}, clear=True), patch.object(Path, "home", return_value=self.home), patch("agent.secret_scope.build_profile_env_boundary", return_value=boundary) as builder, patch.object(panel.subprocess, "Popen", side_effect=capture), patch.object(panel, "archive_automation_session", return_value={"outcome": "not_created", "detail": "inert"}):
            result = panel.run_one(profile="public", brief="bounded task", runtime=self.runtime, workspace=self.workspace, receipt_dir=self.receipts, timeout=1)
        self.builder_call = builder.call_args
        return result

    def test_source_credentials_do_not_cross_and_workspace_tools_survive(self):
        result = self.invoke()
        command, options = self.calls[-1]
        env = options["env"]
        for name in ("OPENROUTER_API_KEY", "SOURCE_CUSTOM_TOKEN", "GH_TOKEN", "HERMES_TUI"):
            self.assertNotIn(name, env)
        self.assertEqual(env["BENIGN_TOOL_SETTING"], "keep")
        self.assertEqual(env["HERMES_HOME"], str(self.target))
        self.assertEqual(env["TERMINAL_CWD"], str(self.workspace))
        self.assertEqual(options["cwd"], self.workspace)
        self.assertIn("-P", command)
        self.assertEqual(result["outcome"], "returned")
        self.assertEqual(self.builder_call.kwargs, {"source_home": self.source, "target_home": self.target})

    def test_target_authored_and_explicit_root_grants_are_preserved(self):
        self.invoke(target_values={"OPENROUTER_API_KEY": "synthetic-target"})
        self.assertEqual(self.calls[-1][1]["env"]["OPENROUTER_API_KEY"], "synthetic-target")
        # The canonical boundary contains root values only after explicit opt-in.
        self.invoke(target_values={"OPENROUTER_API_KEY": "synthetic-root-grant"})
        self.assertEqual(self.calls[-1][1]["env"]["OPENROUTER_API_KEY"], "synthetic-root-grant")

    def test_boundary_failure_prevents_worker_effect(self):
        class Child:
            returncode = 0
            def communicate(self, **kwargs):
                return b"", b""
        with patch.object(Path, "home", return_value=self.home), patch("agent.secret_scope.build_profile_env_boundary", side_effect=RuntimeError("unavailable")), patch.object(panel.subprocess, "Popen", return_value=Child()) as spawn, patch.object(panel, "archive_automation_session", return_value={"outcome": "not_created", "detail": "inert"}):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                panel.run_one(profile="public", brief="bounded", runtime=self.runtime, workspace=self.workspace, receipt_dir=self.receipts, timeout=1)
        spawn.assert_not_called()

    def test_archive_helper_receives_no_provider_or_controller_credential(self):
        boundary = ProfileEnvBoundary(self.source, self.target, frozenset({"OPENROUTER_API_KEY", "SOURCE_CUSTOM_TOKEN"}), {"OPENROUTER_API_KEY": "synthetic-target", "SOURCE_CUSTOM_TOKEN": "synthetic-target-private", "TARGET_CUSTOM_TOKEN": "synthetic-target-only"})
        with patch.dict(os.environ, {"HOME": str(self.home), "HERMES_HOME": str(self.source), "OPENROUTER_API_KEY": "synthetic-source", "SOURCE_CUSTOM_TOKEN": "synthetic-private", "GH_TOKEN": "synthetic-gh"}, clear=True), patch("agent.secret_scope.build_profile_env_boundary", return_value=boundary) as builder, patch.object(panel.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"found": false, "archived": false}', "")) as run:
            result = panel.archive_automation_session(runtime=self.runtime, profile_home=self.target, session_id="exact-session")
        env = run.call_args.kwargs["env"]
        for name in ("OPENROUTER_API_KEY", "SOURCE_CUSTOM_TOKEN", "TARGET_CUSTOM_TOKEN", "GH_TOKEN"):
            self.assertNotIn(name, env)
        self.assertEqual(env["PANEL_SESSION_ID"], "exact-session")
        self.assertEqual(result["outcome"], "not_created")
        builder.assert_called_once_with(source_home=self.source, target_home=self.target)

    def test_already_cancelled_panel_has_no_child_effect(self):
        cancelled = threading.Event()
        cancelled.set()
        boundary = ProfileEnvBoundary(self.source, self.target, frozenset(), {})
        with patch.object(Path, "home", return_value=self.home), patch("agent.secret_scope.build_profile_env_boundary", return_value=boundary), patch.object(panel.subprocess, "Popen") as spawn:
            with self.assertRaises(panel.concurrent.futures.CancelledError):
                panel.run_one(profile="public", brief="bounded", runtime=self.runtime, workspace=self.workspace, receipt_dir=self.receipts, timeout=1, panel_cancelled=cancelled)
        spawn.assert_not_called()

    def test_completed_worker_keeps_receipt_when_archive_policy_fails(self):
        boundary = ProfileEnvBoundary(self.source, self.target, frozenset(), {})
        class Child:
            returncode = 0
            def communicate(self, **kwargs):
                return b"completed advisory", b""
        with patch.dict(os.environ, {"HOME": str(self.home), "HERMES_HOME": str(self.source)}, clear=True), patch.object(Path, "home", return_value=self.home), patch("agent.secret_scope.build_profile_env_boundary", side_effect=[boundary, RuntimeError("archive unavailable")]), patch.object(panel.subprocess, "Popen", return_value=Child()), patch.object(panel.subprocess, "run") as archive:
            result = panel.run_one(profile="public", brief="bounded", runtime=self.runtime, workspace=self.workspace, receipt_dir=self.receipts, timeout=1)
        self.assertEqual(result["outcome"], "returned")
        self.assertEqual(result["session_archive_outcome"], "failed")
        self.assertEqual((self.receipts / "profiles/public.stdout.txt").read_text(), "completed advisory")
        archive.assert_not_called()


if __name__ == "__main__":
    unittest.main()
