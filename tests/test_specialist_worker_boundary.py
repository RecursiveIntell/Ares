"""Behavioral regressions for the admitted specialist worker's launch boundary."""
from __future__ import annotations

import importlib.machinery
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.secret_scope import ProfileEnvBoundary
from ares_runtime import specialist_dispatch as dispatch


class SpecialistWorkerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory()
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name) / "home"
        self.target = self.root / "profiles" / "explorer"
        self.target.mkdir(parents=True)
        self.workspace = Path(self.fixture.name) / "workspace"
        self.workspace.mkdir()
        self.tool_path = Path(self.fixture.name) / "tooling"
        self.tool_path.mkdir()
        (self.tool_path / "project_tool.py").write_text("VALUE = 'project-tool'")
        self.receipts = Path(self.fixture.name) / "receipts"
        self.request = dispatch.ExplicitDispatchRequest({"workspace": str(self.workspace), "brief": "bounded task"})
        self.calls = []

    def invoke(self, *, target_values=None):
        boundary = ProfileEnvBoundary(self.root, self.target, frozenset({"OPENROUTER_API_KEY"}), target_values or {})
        class Child:
            returncode = 0
            def communicate(self):
                return b"trusted output", b""
        def capture(command, **options):
            self.calls.append((command, options))
            return Child()
        with patch.dict(os.environ, {"HOME": str(self.root.parent), "HERMES_HOME": str(self.root), "PATH": "/usr/bin:/bin", "PYTHONPATH": str(self.tool_path), "OPENROUTER_API_KEY": "synthetic-source"}, clear=True), patch("agent.secret_scope.build_profile_env_boundary", return_value=boundary) as builder, patch.object(dispatch.subprocess, "Popen", side_effect=capture):
            result = dispatch._run_profile_worker("explorer", self.request, self.receipts, self.root)
        builder.assert_called_once_with(source_home=self.root, target_home=self.target)
        return result

    def test_workspace_shadow_is_excluded_without_changing_project_workspace(self):
        shadow = self.workspace / "hermes_cli"
        shadow.mkdir()
        (shadow / "__init__.py").write_text("")
        (shadow / "main.py").write_text("raise AssertionError('workspace shadow executed')")
        result = self.invoke()
        command, options = self.calls[0]
        # Resolve the actual emitted module search inputs without launching a CLI.
        search = options["env"].get("PYTHONPATH", "").split(os.pathsep)
        if "-P" not in command:
            search.insert(0, str(options["cwd"]))
        spec = importlib.machinery.PathFinder.find_spec("hermes_cli", search)
        self.assertIsNotNone(spec)
        self.assertNotEqual(Path(spec.origin).parent, shadow)
        self.assertIn("-P", command)
        self.assertEqual(options["cwd"], self.workspace)
        self.assertEqual(command[command.index("--in") + 1], str(self.workspace))
        self.assertEqual(result["outcome"], "returned")

    def test_target_grant_replaces_source_and_target_missing_is_not_borrowed(self):
        self.invoke()
        self.assertNotIn("OPENROUTER_API_KEY", self.calls[-1][1]["env"])
        self.invoke(target_values={"OPENROUTER_API_KEY": "synthetic-target"})
        self.assertEqual(self.calls[-1][1]["env"]["OPENROUTER_API_KEY"], "synthetic-target")

    def test_explicit_project_tool_paths_remain_available(self):
        self.invoke()
        search = self.calls[-1][1]["env"]["PYTHONPATH"].split(os.pathsep)
        spec = importlib.machinery.PathFinder.find_spec("project_tool", search)
        self.assertEqual(Path(spec.origin).parent, self.tool_path)

    def test_unavailable_boundary_prevents_any_launch(self):
        with patch("agent.secret_scope.build_profile_env_boundary", side_effect=RuntimeError("unavailable")), patch.object(dispatch.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                dispatch._run_profile_worker("explorer", self.request, self.receipts, self.root)
        spawn.assert_not_called()

    def test_missing_profile_never_resolves_credentials_or_spawns(self):
        with patch("agent.secret_scope.build_profile_env_boundary") as builder, patch.object(dispatch.subprocess, "Popen") as spawn:
            result = dispatch._run_profile_worker("public", self.request, self.receipts, self.root)
        self.assertEqual(result["error_type"], "PROFILE_HOME_MISSING")
        builder.assert_not_called()
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
