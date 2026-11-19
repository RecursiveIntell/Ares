"""Negative witnesses for graph-browser origin and file-scope validation."""
from types import SimpleNamespace

from tools.graph_browser_operation import GraphBrowserOperation


def _parent():
    return SimpleNamespace(session_id="browser-policy-owner")


def _read(op, allowed_origins, url):
    page_requests = []
    op._page_ws = lambda *args, **kwargs: page_requests.append(url) or "ws://127.0.0.1:1/devtools"
    result = op.read_page(
        {"allowed_origins": allowed_origins, "port": 1},
        url=url,
        selector="#marker",
        parent_agent=_parent(),
        timeout_seconds=1,
    )
    return result, page_requests


def test_origin_allowlist_rejects_hostname_suffix_bypass():
    op = GraphBrowserOperation(chromium="not-started")

    result, page_requests = _read(op, ["https://example.com"], "https://example.com.evil.test/")

    assert result == {"status": "blocked", "code": "browser_origin_not_allowed"}
    assert page_requests == []


def test_origin_allowlist_rejects_userinfo_host_spoof():
    op = GraphBrowserOperation(chromium="not-started")

    result, page_requests = _read(op, ["https://example.com"], "https://example.com@evil.test/")

    assert result == {"status": "blocked", "code": "browser_origin_not_allowed"}
    assert page_requests == []


def test_file_allowlist_rejects_sibling_directory(tmp_path):
    allowed_dir = tmp_path / "allowed"
    sibling_dir = tmp_path / "allowed-sibling"
    allowed_dir.mkdir()
    sibling_dir.mkdir()
    secret = sibling_dir / "secret.html"
    secret.write_text("not in the authorized directory")
    op = GraphBrowserOperation(chromium="not-started")

    result, page_requests = _read(op, [allowed_dir.as_uri()], secret.as_uri())

    assert result == {"status": "blocked", "code": "browser_origin_not_allowed"}
    assert page_requests == []
