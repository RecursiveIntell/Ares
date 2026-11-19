"""BROWSER card (P4): isolated read-only browser operation.

Two disposable Chromium contexts via CDP; operation is read-only. Context A
reads its marker; context B cannot. No live login, no real submission.
"""
import json
import shutil
from pathlib import Path

import pytest

from tools.graph_browser_operation import (
    GraphBrowserOperation,
    BrowserOperationError,
)

CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")
pytestmark = pytest.mark.skipif(CHROMIUM is None, reason="chromium not installed")


def _parent():
    from types import SimpleNamespace
    return SimpleNamespace(session_id="browser-owner", enabled_toolsets=["browser"], _delegate_depth=0)


@pytest.fixture
def pages(tmp_path):
    a = tmp_path / "a.html"
    b = tmp_path / "b.html"
    a.write_text("<html><body><div id='marker'>MARKER-A-SECRET</div></body></html>")
    b.write_text("<html><body><div id='marker'>MARKER-B-PUBLIC</div></body></html>")
    return a, b


def test_context_a_reads_marker_b_cannot(tmp_path, pages):
    a, b = pages
    op = GraphBrowserOperation(chromium=CHROMIUM)
    with op.context("ctx-A", allowed_origins=["file://"]) as ctx_a, \
         op.context("ctx-B", allowed_origins=["file://"]) as ctx_b:
        result_a = op.read_page(ctx_a, url=a.as_uri(), selector="#marker",
                                parent_agent=_parent(), timeout_seconds=20)
        assert result_a["status"] == "completed"
        assert "MARKER-A-SECRET" in result_a["text"]
        # Context B has its own storage/cookies; it never saw A's page.
        result_b = op.read_page(ctx_b, url=b.as_uri(), selector="#marker",
                                parent_agent=_parent(), timeout_seconds=20)
        assert "MARKER-B-PUBLIC" in result_b["text"]
        assert "MARKER-A-SECRET" not in result_b["text"]
        # B cannot read A's context-scoped artifact key either.
        assert op.read_context_state(ctx_b, "ctx-A") is None


def test_disallowed_origin_is_refused(tmp_path, pages):
    a, _ = pages
    op = GraphBrowserOperation(chromium=CHROMIUM)
    with op.context("ctx-origin", allowed_origins=["file://" + str(tmp_path)]) as ctx:
        result = op.read_page(ctx, url="https://example.com/", selector="#x",
                              parent_agent=_parent(), timeout_seconds=10)
        assert result["status"] == "blocked"
        assert result["code"] == "browser_origin_not_allowed"


def test_missing_owner_refused(tmp_path, pages):
    a, _ = pages
    op = GraphBrowserOperation(chromium=CHROMIUM)
    with op.context("ctx-noowner", allowed_origins=["file://"]) as ctx:
        result = op.read_page(ctx, url=a.as_uri(), selector="#marker",
                              parent_agent=None, timeout_seconds=10)
        assert result["status"] == "blocked"
        assert result["code"] == "policy_owner_required"


def test_cancellation_closes_context(tmp_path, pages):
    a, _ = pages
    op = GraphBrowserOperation(chromium=CHROMIUM)
    ctx = op.open_context("ctx-cancel", allowed_origins=["file://"])
    op.read_page(ctx, url=a.as_uri(), selector="#marker",
                 parent_agent=_parent(), timeout_seconds=20)
    state = op.cancel_context(ctx)
    assert state["cleanup_state"] == "closed"
    assert not Path(ctx["user_data_dir"]).exists() or state["process_exited"]


def test_mutating_operation_class_is_blocked(tmp_path, pages):
    a, _ = pages
    op = GraphBrowserOperation(chromium=CHROMIUM)
    with op.context("ctx-mut", allowed_origins=["file://"]) as ctx:
        result = op.submit_form(ctx, url=a.as_uri(), selector="form",
                                parent_agent=_parent(), timeout_seconds=10)
        assert result["status"] == "blocked"
        assert result["code"] == "browser_mutation_class_unqualified"
