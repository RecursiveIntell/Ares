"""Behavioral contract for approved multi-root file pagination on Linux."""
import json
import os
import shutil
import subprocess

import pytest

from tools.file_operations import ShellFileOperations


class ActualShell:
    """Execute the terminal-environment protocol inside the test namespace."""

    is_local = True

    def __init__(self, cwd):
        self.cwd = str(cwd)

    def execute(self, command, cwd=None, timeout=None, stdin_data=None):
        result = subprocess.run(
            ["/usr/bin/bash", "-c", command],
            cwd=cwd or self.cwd,
            input=stdin_data,
            text=True,
            capture_output=True,
            timeout=min(timeout or 20, 20),
            check=False,
        )
        return {"output": result.stdout + result.stderr, "returncode": result.returncode}


def make_roots(tmp_path, sizes):
    roots = []
    expected = []
    for root_index, size in enumerate(sizes):
        root = tmp_path / f"root{root_index}"
        root.mkdir()
        roots.append(root)
        root_files = []
        for index in range(size):
            path = root / f"item{index}.txt"
            path.write_text(f"needle {root_index} {index}\n")
            stamp = 100000 + root_index * 100 + index
            os.utime(path, (stamp, stamp))
            root_files.append(str(path))
        expected.extend(reversed(root_files))
    return roots, expected


def actual_ops(tmp_path, engine, monkeypatch):
    assert shutil.which(engine), f"test prerequisite missing: {engine}"
    ops = ShellFileOperations(ActualShell(tmp_path))
    if engine == "find":
        original = ops._has_command
        monkeypatch.setattr(ops, "_has_command", lambda command: False if command == "rg" else original(command))
    return ops


@pytest.mark.linux_only
@pytest.mark.parametrize("engine", ["rg", "find"])
@pytest.mark.parametrize("sizes", [(3, 3), (0, 4), (2, 0), (1, 3, 2), (0, 0)])
@pytest.mark.parametrize("offset", [0, 1, 3, 6, 20])
@pytest.mark.parametrize("limit", [1, 2, 8])
def test_file_pages_slice_the_combined_result_once(tmp_path, monkeypatch, engine, sizes, offset, limit):
    roots, expected = make_roots(tmp_path, sizes)
    ops = actual_ops(tmp_path, engine, monkeypatch)
    result = ops.search("*.txt", path=" ".join(map(str, roots)), target="files", offset=offset, limit=limit)
    assert result.error is None
    assert result.files == expected[offset:offset + limit]
    assert len(result.files) <= limit
    assert "searched" in (result.warning or "")
    if offset + limit < len(expected):
        assert result.truncated, "a page with known later results must not claim completeness"


@pytest.mark.linux_only
@pytest.mark.parametrize("separator", [",", " "])
def test_requested_path_order_and_missing_path_note_are_preserved(tmp_path, monkeypatch, separator):
    roots, _ = make_roots(tmp_path, (3, 3))
    ops = actual_ops(tmp_path, "rg", monkeypatch)
    reversed_expected = [str(roots[1] / "item2.txt"), str(roots[1] / "item1.txt"), str(roots[1] / "item0.txt"),
                         str(roots[0] / "item2.txt"), str(roots[0] / "item1.txt"), str(roots[0] / "item0.txt")]
    path = separator.join([str(roots[1]), str(tmp_path / "missing"), str(roots[0])])
    result = ops.search("*.txt", path=path, target="files", offset=2, limit=3)
    assert result.error is None
    assert result.files == reversed_expected[2:5]
    assert "skipped missing" in (result.warning or "")


@pytest.mark.linux_only
def test_single_root_pagination_and_literal_space_directory_stay_single_path(tmp_path, monkeypatch):
    root = tmp_path / "literal space directory"
    root.mkdir()
    for index in range(4):
        path = root / f"item{index}.txt"
        path.write_text("needle\n")
        os.utime(path, (100000 + index, 100000 + index))
    ops = actual_ops(tmp_path, "rg", monkeypatch)
    result = ops.search("*.txt", path=str(root), target="files", offset=1, limit=2)
    assert result.error is None
    assert result.files == [str(root / "item2.txt"), str(root / "item1.txt")]
    assert not result.warning


@pytest.mark.linux_only
@pytest.mark.parametrize("offset", [0, 2, 4])
def test_public_search_tool_serializes_the_correct_page(tmp_path, monkeypatch, offset):
    from tools import file_tools

    roots, expected = make_roots(tmp_path, (3, 3))
    ops = actual_ops(tmp_path, "rg", monkeypatch)
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id=None: ops)
    response = file_tools.search_tool("*.txt", path=" ".join(map(str, roots)), target="files", offset=offset, limit=2)
    # The public wire contract is a JSON object followed by an optional hint,
    # not a JSON-only response. Preserve and check both rather than weakening it.
    result, end = json.JSONDecoder().raw_decode(response)
    assert "error" not in result
    assert result.get("files", []) == expected[offset:offset + 2]
    if result.get("truncated"):
        assert f"Use offset={offset + 2}" in response[end:]


@pytest.mark.linux_only
def test_content_search_branch_keeps_its_existing_pagination_calls(tmp_path, monkeypatch):
    from tools.file_operations import SearchResult

    roots, _ = make_roots(tmp_path, (1, 1))
    ops = actual_ops(tmp_path, "rg", monkeypatch)
    calls = []

    def content(pattern, path, file_glob, limit, offset, output_mode, context):
        calls.append((path, limit, offset, output_mode, context))
        return SearchResult(total_count=0)

    monkeypatch.setattr(ops, "_search_content", content)
    result = ops._try_multi_path_search("needle", " ".join(map(str, roots)), "content", None, 2, 3, "content", 0)
    assert result is not None
    assert calls == [(str(root), 2, 3, "content", 0) for root in roots]
