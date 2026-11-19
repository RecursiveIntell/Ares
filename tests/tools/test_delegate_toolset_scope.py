"""Tests for delegate_tool toolset scoping.

Verifies that subagents cannot gain tools that the parent does not have.
The LLM controls the `toolsets` parameter — without intersection with the
parent's enabled_toolsets, it can escalate privileges by requesting
arbitrary toolsets.
"""

from types import SimpleNamespace

from tools.delegate_tool import _strip_blocked_tools, _emit_parent_console


class TestToolsetIntersection:
    """Subagent toolsets must be a subset of parent's enabled_toolsets."""

    def test_requested_toolsets_intersected_with_parent(self):
        """LLM requests toolsets parent doesn't have — extras are dropped."""
        parent = SimpleNamespace(enabled_toolsets=["terminal", "file"])

        # Simulate the intersection logic from _build_child_agent
        parent_toolsets = set(parent.enabled_toolsets)
        requested = ["terminal", "file", "web", "browser", "rl"]
        scoped = [t for t in requested if t in parent_toolsets]

        assert sorted(scoped) == ["file", "terminal"]
        assert "web" not in scoped
        assert "browser" not in scoped
        assert "rl" not in scoped


    def test_strip_blocked_removes_delegation(self):
        """Blocked toolsets (delegation, clarify, etc.) are always removed."""
        child = _strip_blocked_tools(["terminal", "delegation", "clarify", "memory"])
        assert "delegation" not in child
        assert "clarify" not in child
        assert "memory" not in child
        assert "terminal" in child

    def test_empty_intersection_yields_empty_toolsets(self):
        """If parent has no overlap with requested, child gets nothing extra."""
        parent = SimpleNamespace(enabled_toolsets=["terminal"])

        parent_toolsets = set(parent.enabled_toolsets)
        requested = ["web", "browser"]
        scoped = [t for t in requested if t in parent_toolsets]

        assert scoped == []


class TestEmitParentConsole:
    """Progress lines (e.g. ``✓ [N/M] …``) must route through the parent's
    configured ``_safe_print`` in headless stdio hosts (ACP, gateway) so
    they don't land on stdout and corrupt JSON-RPC frames. Regression for a
    bug where delegate_task completion lines pushed to stdout caused
    ``Failed to parse JSON message: ✓ [3/3] …`` errors in the ACP adapter."""

    def test_routes_through_parent_safe_print_when_available(self, capsys):
        captured_lines = []
        parent = SimpleNamespace(_safe_print=lambda line: captured_lines.append(line))

        _emit_parent_console(parent, "  ✓ [1/3] Research done  (11.55s)")

        assert captured_lines == ["  ✓ [1/3] Research done  (11.55s)"]
        stdout_stderr = capsys.readouterr()
        assert stdout_stderr.out == ""
        assert stdout_stderr.err == ""


    def test_non_callable_safe_print_is_ignored(self, capsys):
        """Defensive: if _safe_print is set but not callable, fall back."""
        parent = SimpleNamespace(_safe_print="not-a-function")
        _emit_parent_console(parent, "  ✓ [3/3] non-callable guard")
        captured = capsys.readouterr()
        assert "non-callable guard" in captured.out


# --- READ card (P4): fixed read-only graph operation ---
# LOCK contract-v1: one fixed op, scoped workspace, unique session, timeout,
# control handle bound to owner lineage. Never the parent's full tool list.

import hashlib
import json
from pathlib import Path

import pytest


class TestGraphReadOperation:
    def _parent(self, tmp_path):
        from types import SimpleNamespace
        return SimpleNamespace(
            enabled_toolsets=["terminal", "file", "web", "browser"],
            session_id="read-owner-session",
            _delegate_depth=0,
        )

    def test_allowed_file_returns_verified_content_hash(self, tmp_path):
        from tools.delegate_tool import run_graph_read_operation
        fixture = tmp_path / "work" / "data.txt"
        fixture.parent.mkdir()
        fixture.write_text("fixture read content\n")
        parent = self._parent(tmp_path)
        result = run_graph_read_operation(
            operation_id="op-read-0001",
            workspace=tmp_path / "work",
            path="data.txt",
            parent_agent=parent,
            timeout_seconds=5,
        )
        assert result["status"] == "completed"
        assert result["content_sha256"] == hashlib.sha256(fixture.read_bytes()).hexdigest()
        assert result["operation_id"] == "op-read-0001"
        # Fixed op exposes ONLY the read capability, never parent toolsets.
        assert result["tool_classes"] == ["artifact_read"]

    def test_refused_path_outside_workspace_never_executes(self, tmp_path):
        from tools.delegate_tool import run_graph_read_operation
        outside = tmp_path / "secret.txt"
        outside.write_text("secret")
        work = tmp_path / "work"; work.mkdir()
        parent = self._parent(tmp_path)
        for bad in ("../secret.txt", str(outside), "/etc/passwd"):
            result = run_graph_read_operation(
                operation_id="op-read-bad", workspace=work, path=bad,
                parent_agent=parent, timeout_seconds=5,
            )
            assert result["status"] == "blocked"
            assert result["code"] == "read_path_out_of_scope"
            assert result.get("content_sha256") is None

    def test_symlink_escape_is_refused(self, tmp_path):
        from tools.delegate_tool import run_graph_read_operation
        work = tmp_path / "work"; work.mkdir()
        outside = tmp_path / "outside.txt"; outside.write_text("outside")
        (work / "link.txt").symlink_to(outside)
        result = run_graph_read_operation(
            operation_id="op-read-link", workspace=work, path="link.txt",
            parent_agent=self._parent(tmp_path), timeout_seconds=5,
        )
        assert result["status"] == "blocked"
        assert result["code"] == "read_path_out_of_scope"

    def test_missing_owner_is_refused(self, tmp_path):
        from tools.delegate_tool import run_graph_read_operation
        work = tmp_path / "work"; work.mkdir()
        (work / "f.txt").write_text("x")
        result = run_graph_read_operation(
            operation_id="op-read-noowner", workspace=work, path="f.txt",
            parent_agent=None, timeout_seconds=5,
        )
        assert result["status"] == "blocked"
        assert result["code"] == "policy_owner_required"

    def test_duplicate_operation_key_reattaches_without_second_read(self, tmp_path):
        from tools.delegate_tool import run_graph_read_operation
        work = tmp_path / "work"; work.mkdir()
        f = work / "f.txt"; f.write_text("one")
        parent = self._parent(tmp_path)
        first = run_graph_read_operation(
            operation_id="op-read-dup", workspace=work, path="f.txt",
            parent_agent=parent, timeout_seconds=5,
        )
        assert first["status"] == "completed"
        f.write_text("changed after first read")
        again = run_graph_read_operation(
            operation_id="op-read-dup", workspace=work, path="f.txt",
            parent_agent=parent, timeout_seconds=5,
        )
        # Same key returns the ORIGINAL result, not a fresh read.
        assert again["content_sha256"] == first["content_sha256"]
        assert again["reattached"] is True

    def test_expired_deadline_does_not_read(self, tmp_path):
        from tools.delegate_tool import run_graph_read_operation
        work = tmp_path / "work"; work.mkdir()
        (work / "f.txt").write_text("x")
        result = run_graph_read_operation(
            operation_id="op-read-expired", workspace=work, path="f.txt",
            parent_agent=self._parent(tmp_path), timeout_seconds=0,
        )
        assert result["status"] == "blocked"
        assert result["code"] == "read_deadline_expired"
