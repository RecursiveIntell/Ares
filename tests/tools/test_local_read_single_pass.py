"""Local read transport contracts, with complete pre-repair ReadResult goldens.

These fixtures were recorded through ShellFileOperations + LocalEnvironment
before the transport change. They deliberately preserve wc/cut pagination
quirks, including LF counts and the numbered trailing empty line.
"""

from dataclasses import asdict
import json
import os
from pathlib import Path
import unicodedata
from unittest.mock import patch

import pytest

from tools.environments.local import LocalEnvironment
import tools.file_operations as file_operations
from tools.file_operations import ReadResult, ShellFileOperations


# These goldens were witnessed on Linux; the resource contract also uses
# Linux /proc and ru_maxrss units. Windows keeps the legacy implementation.
pytestmark = pytest.mark.linux_only


CASES = [
    ("utf8", "notes.txt", "alpha\nbêta\n你好\n".encode(), 1, 2000),
    ("crlf", "notes.txt", b"alpha\r\nbeta\r\n", 1, 2000),
    ("bom", "notes.txt", b"\xef\xbb\xbfalpha\nbeta\n", 1, 2000),
    ("bom_page", "notes.txt", b"\xef\xbb\xbfalpha\nbeta\n", 2, 1),
    ("empty", "notes.txt", b"", 1, 2000),
    ("blank", "notes.txt", b"\n", 1, 2000),
    ("no_final", "notes.txt", b"alpha\nbeta", 1, 2000),
    ("single_no_final", "notes.txt", b"alpha", 1, 2000),
    ("offset", "notes.txt", b"a\nb\nc\nd\n", 2, 2),
    ("beyond", "notes.txt", b"a\nb\n", 8, 2),
    ("beyond_no_final", "notes.txt", b"a\nb", 2, 2),
    ("beyond_zero_lfs", "notes.txt", b"a", 2, 2),
    ("normalized", "notes.txt", b"a\nb\n", 0, 0),
    ("limit_cap", "notes.txt", b"x\n" * 2002, 1, 9000),
    ("long_ascii", "notes.txt", b"a" * 100000 + b"\nend", 1, 2000),
    ("long_unicode", "notes.txt", ("😀" * 6000 + "\nend\n").encode(), 1, 2000),
    ("sample_cut", "notes.txt", ("a" * 999 + "你\n").encode(), 1, 2000),
    ("stored_replacement", "notes.txt", "ok � stored\n".encode(), 1, 2000),
    ("invalid_sample", "notes.txt", b"caf\xe9" * 100 + b"\n", 1, 2000),
    ("invalid_tail", "notes.txt", b"a" * 1100 + b"\xff\n", 1, 2000),
    ("nul", "notes.txt", b"abc\x00def\n", 1, 2000),
    ("binary_extension", "data.zip", b"PK\x03\x04abc", 1, 2000),
    ("image", "image.png", b"\x89PNG\r\n\x1a\n", 1, 2000),
    ("utf16le", "notes.txt", "\ufeffhello\n世界\n".encode("utf-16-le"), 1, 2000),
    ("utf16be", "notes.txt", "\ufeffhello\r\nworld\r\n".encode("utf-16-be"), 1, 2000),
    ("utf16_bomless", "notes.txt", "hello\nworld".encode("utf-16-le"), 1, 2000),
    ("utf16_page", "notes.txt", "\ufeffa\nb\nc\n".encode("utf-16-le"), 2, 1),
    ("utf16_beyond", "notes.txt", "\ufeffhello\n".encode("utf-16-le"), 10, 1),
    (
        "utf16_long",
        "notes.txt",
        ("\ufeff" + "😀" * 10000 + "\r\nend").encode("utf-16-le"),
        1,
        2000,
    ),
    ("utf16_malformed", "notes.txt", b"\xff\xfeh\x00i\x00\xff", 1, 2000),
    ("lone_cr", "notes.txt", b"alpha\rbeta\n", 1, 2000),
    ("fences", "notes.txt", b"alpha\n'\x07__HERMES_FENCE_abc__\x07'\nbeta\n", 1, 2000),
    (
        "utf16_fences",
        "notes.txt",
        ("\ufeff" + "'__HERMES_FENCE_abc__'" * 200 + "z" * 2100 + "\n").encode(
            "utf-16-le"
        ),
        1,
        2000,
    ),
    (
        "utf16_chunk_crlf",
        "notes.txt",
        ("\ufeff" + "x" * 499 + "y" * 32767 + "\r\nend\n").encode("utf-16-le"),
        1,
        2000,
    ),
    ("utf8_chunk_lf", "notes.txt", b"x" * (1000 + 65536 - 1) + b"\nnext\n", 2, 1),
]

# GOLDEN_RESULTS
GOLDEN = {
    "utf8": {
        "content": "1|alpha\n2|bêta\n3|你好\n4|",
        "total_lines": 3,
        "file_size": 19,
        "truncated": False,
        "hint": None,
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "crlf": {
        "content": "1|alpha\r\n2|beta\r\n3|",
        "total_lines": 2,
        "file_size": 13,
        "truncated": False,
        "hint": None,
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "bom": {
        "content": "1|alpha\n2|beta\n3|",
        "total_lines": 2,
        "file_size": 14,
        "truncated": False,
        "hint": None,
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "bom_page": {
        "content": "2|beta\n3|",
        "total_lines": 2,
        "file_size": 14,
        "truncated": False,
        "hint": None,
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "empty": {
        "content": "",
        "total_lines": 0,
        "file_size": 0,
        "truncated": False,
        "hint": "File is empty (0 bytes).",
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "blank": {
        "content": "1|\n2|",
        "total_lines": 1,
        "file_size": 1,
        "truncated": False,
        "hint": None,
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "no_final": {
        "content": "1|alpha\n2|beta",
        "total_lines": 1,
        "file_size": 10,
        "truncated": False,
        "hint": None,
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "single_no_final": {
        "content": "1|alpha",
        "total_lines": 0,
        "file_size": 5,
        "truncated": False,
        "hint": None,
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "offset": {
        "content": "2|b\n3|c\n4|",
        "total_lines": 4,
        "file_size": 8,
        "truncated": True,
        "hint": "Use offset=4 to continue reading (showing 2-3 of 4 lines)",
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "beyond": {
        "content": "",
        "total_lines": 2,
        "file_size": 4,
        "truncated": False,
        "hint": "Note: offset 8 is beyond the end of the file (2 lines total). Retry with offset <= 2.",
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "beyond_no_final": {
        "content": "",
        "total_lines": 1,
        "file_size": 3,
        "truncated": False,
        "hint": "Note: offset 2 is beyond the end of the file (1 lines total). Retry with offset <= 1.",
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "beyond_zero_lfs": {
        "content": "2|",
        "total_lines": 0,
        "file_size": 1,
        "truncated": False,
        "hint": None,
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "normalized": {
        "content": "1|a\n2|",
        "total_lines": 2,
        "file_size": 4,
        "truncated": True,
        "hint": "Use offset=2 to continue reading (showing 1-1 of 2 lines)",
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "limit_cap": {
        "content": "\n".join(f"{i}|x" for i in range(1, 2001)) + "\n2001|",
        "total_lines": 2002,
        "file_size": 4004,
        "truncated": True,
        "hint": "Use offset=2001 to continue reading (showing 1-2000 of 2002 lines)",
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "long_ascii": {
        "content": "1|" + "a" * 2000 + "... [truncated]\n2|end",
        "total_lines": 1,
        "file_size": 100004,
        "truncated": False,
        "hint": None,
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "long_unicode": {
        "content": "1|" + "😀" * 2000 + "... [truncated]\n2|end\n3|",
        "total_lines": 2,
        "file_size": 24005,
        "truncated": False,
        "hint": None,
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "sample_cut": {
        "content": "1|" + "a" * 999 + "你\n2|",
        "total_lines": 1,
        "file_size": 1003,
        "truncated": False,
        "hint": None,
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "stored_replacement": {
        "content": "1|ok � stored\n2|",
        "total_lines": 1,
        "file_size": 14,
        "truncated": False,
        "hint": None,
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "invalid_sample": {
        "content": "",
        "total_lines": 0,
        "file_size": 401,
        "truncated": False,
        "hint": None,
        "is_binary": True,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": "Binary file (unknown binary, 401 bytes) — cannot display as text.",
        "similar_files": [],
    },
    "invalid_tail": {
        "content": "1|" + "a" * 1100 + "�\n2|",
        "total_lines": 1,
        "file_size": 1102,
        "truncated": False,
        "hint": None,
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "nul": {
        "content": "",
        "total_lines": 0,
        "file_size": 8,
        "truncated": False,
        "hint": None,
        "is_binary": True,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": "Binary file (unknown binary, 8 bytes) — cannot display as text.",
        "similar_files": [],
    },
    "binary_extension": {
        "content": "",
        "total_lines": 0,
        "file_size": 7,
        "truncated": False,
        "hint": None,
        "is_binary": True,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": "Binary file (ZIP archive (also docx/xlsx/jar/apk), 7 bytes) — cannot display as text.",
        "similar_files": [],
    },
    "image": {
        "content": "",
        "total_lines": 0,
        "file_size": 8,
        "truncated": False,
        "hint": "Image file detected. Automatically redirected to vision_analyze tool. Use vision_analyze with this file path to inspect the image contents.",
        "is_binary": True,
        "is_image": True,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "utf16le": {
        "content": "1|hello\n2|世界\n3|",
        "total_lines": 3,
        "file_size": 20,
        "truncated": False,
        "hint": "Transcoded from UTF-16-LE to UTF-8 for display. Text edits via patch/write_file would re-encode as UTF-8.",
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "utf16be": {
        "content": "1|hello\n2|world\n3|",
        "total_lines": 3,
        "file_size": 30,
        "truncated": False,
        "hint": "Transcoded from UTF-16-BE to UTF-8 for display. Text edits via patch/write_file would re-encode as UTF-8.",
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "utf16_bomless": {
        "content": "1|hello\n2|world",
        "total_lines": 2,
        "file_size": 22,
        "truncated": False,
        "hint": "Transcoded from UTF-16-LE to UTF-8 for display. Text edits via patch/write_file would re-encode as UTF-8.",
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "utf16_page": {
        "content": "2|b",
        "total_lines": 4,
        "file_size": 14,
        "truncated": True,
        "hint": "Transcoded from UTF-16-LE to UTF-8 for display. Text edits via patch/write_file would re-encode as UTF-8. Use offset=3 to continue reading (showing 2-2 of 4 lines)",
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "utf16_beyond": {
        "content": "10|",
        "total_lines": 2,
        "file_size": 14,
        "truncated": False,
        "hint": "Transcoded from UTF-16-LE to UTF-8 for display. Text edits via patch/write_file would re-encode as UTF-8.",
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "utf16_long": {
        "content": "1|" + "😀" * 2000 + "... [truncated]\n2|end",
        "total_lines": 2,
        "file_size": 40012,
        "truncated": False,
        "hint": "Transcoded from UTF-16-LE to UTF-8 for display. Text edits via patch/write_file would re-encode as UTF-8.",
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "utf16_malformed": {
        "content": "1|hi�",
        "total_lines": 1,
        "file_size": 7,
        "truncated": False,
        "hint": "Transcoded from UTF-16-LE to UTF-8 for display. Text edits via patch/write_file would re-encode as UTF-8.",
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "lone_cr": {
        "content": "1|alpha\rbeta\n2|",
        "total_lines": 1,
        "file_size": 11,
        "truncated": False,
        "hint": None,
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "fences": {
        "content": "1|alpha\n2|beta\n3|",
        "total_lines": 3,
        "file_size": 36,
        "truncated": False,
        "hint": None,
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "utf16_fences": {
        "content": "1|" + "z" * 2000 + "... [truncated]\n2|",
        "total_lines": 2,
        "file_size": 13004,
        "truncated": False,
        "hint": "Transcoded from UTF-16-LE to UTF-8 for display. Text edits via patch/write_file would re-encode as UTF-8.",
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "utf16_chunk_crlf": {
        "content": "1|" + "x" * 499 + "y" * 1501 + "... [truncated]\n2|end\n3|",
        "total_lines": 3,
        "file_size": 66546,
        "truncated": False,
        "hint": "Transcoded from UTF-16-LE to UTF-8 for display. Text edits via patch/write_file would re-encode as UTF-8.",
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
    "utf8_chunk_lf": {
        "content": "2|next\n3|",
        "total_lines": 2,
        "file_size": 66541,
        "truncated": False,
        "hint": None,
        "is_binary": False,
        "is_image": False,
        "base64_content": None,
        "mime_type": None,
        "dimensions": None,
        "error": None,
        "similar_files": [],
    },
}


@pytest.fixture
def local_ops(tmp_path):
    env = LocalEnvironment(cwd=str(tmp_path), timeout=15)
    try:
        yield ShellFileOperations(env)
    finally:
        env.cleanup()


@pytest.mark.parametrize(
    "name,filename,payload,offset,limit", CASES, ids=[c[0] for c in CASES]
)
def test_complete_golden_result(
    local_ops, tmp_path, name, filename, payload, offset, limit
):
    (tmp_path / filename).write_bytes(payload)
    assert asdict(local_ops.read_file(filename, offset, limit)) == GOLDEN[name]


def test_common_local_read_has_one_backend_launch(local_ops, tmp_path):
    (tmp_path / "notes.txt").write_bytes(b"alpha\nbeta\n")
    with patch.object(
        local_ops.env, "_run_bash", wraps=local_ops.env._run_bash
    ) as launches:
        result = local_ops.read_file("notes.txt")
    assert result == ReadResult(
        content="1|alpha\n2|beta\n3|", total_lines=2, file_size=11
    )
    assert launches.call_count == 1


def test_every_fixture_uses_one_local_operation(local_ops, tmp_path):
    for name, filename, payload, offset, limit in CASES:
        (tmp_path / filename).write_bytes(payload)
        with patch.object(
            local_ops.env, "execute", wraps=local_ops.env.execute
        ) as calls:
            result = local_ops.read_file(filename, offset, limit)
        assert asdict(result) == GOLDEN[name]
        assert calls.call_count == 1, name
        timeout = calls.call_args.kwargs["timeout"]
        assert 0 < timeout <= local_ops.env.timeout


@pytest.mark.parametrize(
    "spelling", ["relative", "absolute", "tilde", "symlink", "hostile"]
)
def test_paths_follow_live_backend_cwd(local_ops, tmp_path, monkeypatch, spelling):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "notes.txt").write_text("wrong\n")
    (new / "notes.txt").write_text("current\n")
    local_ops.env.execute("true", cwd=str(old))
    assert local_ops.env.execute("true", cwd=str(new))["returncode"] == 0
    assert local_ops.env.cwd == str(new)
    path = "notes.txt"
    if spelling == "absolute":
        path = str(new / path)
    elif spelling == "tilde":
        # Use the backend's own HOME, as _expand_path already does.
        home = Path(local_ops.env.execute('echo "$HOME"')["output"].strip())
        assert home.is_relative_to(Path(os.environ["HOME"])) or home.is_relative_to(
            Path(os.environ["HERMES_HOME"])
        )
        (home / "local-read-notes.txt").write_text("current\n")
        path = "~/local-read-notes.txt"
    elif spelling == "symlink":
        (new / "alias.txt").symlink_to("notes.txt")
        path = "alias.txt"
    elif spelling == "hostile":
        path = "a 'quote'; $(touch injected) `touch injected`\n.txt"
        (new / path).write_text("current\n")
    assert local_ops.read_file(path) == ReadResult(
        content="1|current\n2|",
        total_lines=1,
        file_size=8,
    )
    assert not (new / "injected").exists()


def test_unicode_missing_and_similar_paths_keep_complete_results(local_ops, tmp_path):
    actual = unicodedata.normalize("NFD", "café\u202fnotes.txt")
    requested = "café notes.txt"
    (tmp_path / actual).write_text("hello\n")
    assert local_ops.read_file(requested) == ReadResult(
        content="1|hello\n2|",
        total_lines=1,
        file_size=6,
        hint=(
            f"Note: '{requested}' not found byte-for-byte; resolved to "
            f"the unicode-equivalent file '{actual}' (invisible "
            "encoding difference: NFC/NFD or special space/quote characters)."
        ),
    )
    (tmp_path / "AGENTS.md").write_text("instructions\n")
    assert local_ops.read_file("AGENT.md") == ReadResult(
        error="File not found: AGENT.md",
        similar_files=["./AGENTS.md"],
    )
    (tmp_path / "broken.txt").symlink_to("missing-target")
    assert local_ops.read_file("broken.txt").error == "File not found: broken.txt"


def test_remote_transport_keeps_backend_host_behavior(tmp_path):
    # A separate transport owns its filesystem. It is intentionally not a
    # LocalEnvironment; no SSH/network service is needed for this contract.
    host, remote = tmp_path / "host", tmp_path / "remote"
    host.mkdir()
    remote.mkdir()
    (host / "notes.txt").write_text("wrong host\n")
    (remote / "notes.txt").write_text("remote\n")
    backend = LocalEnvironment(cwd=str(remote), timeout=15)

    class RemoteTransport:
        cwd = str(host)
        is_local = False

        def execute(self, command, **kwargs):
            kwargs["cwd"] = str(remote)
            return backend.execute(command, **kwargs)

    try:
        env = RemoteTransport()
        ops = ShellFileOperations(env)
        with patch.object(env, "execute", wraps=env.execute) as calls:
            result = ops.read_file("notes.txt")
        assert result == ReadResult(content="1|remote\n2|", total_lines=1, file_size=7)
        assert calls.call_count > 1  # the existing remote shell owner remains in use
    finally:
        backend.cleanup()


@pytest.mark.linux_only
def test_special_files_are_rejected_without_opening(local_ops, tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    (tmp_path / "fifo-link").symlink_to(fifo)
    for filename in ("pipe", "fifo-link", "."):
        assert local_ops.read_file(filename) == local_ops._not_regular_error(filename)


@pytest.mark.linux_only
def test_socket_file_is_rejected_without_opening(local_ops, tmp_path):
    import socket

    sock = socket.socket(socket.AF_UNIX)
    try:
        try:
            sock.bind(str(tmp_path / "socket"))
        except PermissionError:
            pytest.skip(
                "Sandbox denies AF_UNIX bind; controller owns real socket guard check"
            )
        assert local_ops.read_file("socket") == local_ops._not_regular_error("socket")
    finally:
        sock.close()


def _instrument_scanner(monkeypatch, setup):
    """Inject faults in the real child process, without mocking read results."""
    monkeypatch.setattr(
        file_operations,
        "_LOCAL_READ_SCRIPT",
        setup + "\n" + file_operations._LOCAL_READ_SCRIPT,
    )


def test_short_os_reads_still_collect_the_declared_binary_sample(
    local_ops, tmp_path, monkeypatch
):
    (tmp_path / "notes.txt").write_bytes(b"a" * 100 + b"\x00" + b"b" * 100)
    _instrument_scanner(
        monkeypatch,
        """
import os
original_read = os.read
def short_read(fd, count):
    return original_read(fd, min(count, 17))
os.read = short_read
""",
    )
    assert local_ops.read_file("notes.txt") == ReadResult(
        is_binary=True,
        file_size=201,
        error="Binary file (unknown binary, 201 bytes) — cannot display as text.",
    )


@pytest.mark.parametrize(
    "mutation", ["append", "truncate", "rewrite", "replace", "retarget"]
)
def test_mid_read_mutation_never_returns_mixed_success(
    local_ops, tmp_path, monkeypatch, mutation
):
    target = tmp_path / "notes.txt"
    target.write_bytes(b"a\n" * 100000)
    path = target
    if mutation == "retarget":
        path = tmp_path / "alias.txt"
        path.symlink_to(target)
    replacement = tmp_path / "replacement.txt"
    replacement.write_bytes(b"b\n" * 100000)
    actions = {
        "append": f"with open({str(target)!r}, 'ab') as f: f.write(b'new\\n')",
        "truncate": f"os.truncate({str(target)!r}, 12)",
        "rewrite": f"with open({str(target)!r}, 'r+b') as f: f.write(b'z')",
        "replace": f"os.replace({str(replacement)!r}, {str(target)!r})",
        "retarget": f"os.unlink({str(path)!r}); os.symlink({str(replacement)!r}, {str(path)!r})",
    }
    setup = f"""
import os
original_read = os.read
mutated = False
def racing_read(fd, count):
    global mutated
    chunk = original_read(fd, count)
    if not mutated:
        mutated = True
        {actions[mutation]}
    return chunk
os.read = racing_read
"""
    _instrument_scanner(monkeypatch, setup)
    result = local_ops.read_file(str(path))
    assert result == ReadResult(
        error=f"File changed while reading: {path}. Retry the read."
    )


@pytest.mark.linux_only
def test_regular_to_fifo_race_does_not_block(local_ops, tmp_path, monkeypatch):
    target = tmp_path / "notes.txt"
    target.write_bytes(b"hello\n")
    _instrument_scanner(
        monkeypatch,
        f"""
import os
original_open = os.open
def racing_open(path, flags):
    os.unlink(path)
    os.mkfifo(path)
    return original_open(path, flags)
os.open = racing_open
""",
    )
    result = local_ops.read_file(str(target))
    assert result == ReadResult(
        error=f"File changed while reading: {target}. Retry the read."
    )


@pytest.mark.parametrize(
    "fault", ["read_error", "permission", "timeout", "bad_protocol"]
)
def test_incomplete_scan_has_no_success_or_exact_count(
    local_ops, tmp_path, monkeypatch, fault
):
    (tmp_path / "notes.txt").write_bytes(b"x\n" * 100000)
    setups = {
        "read_error": """
import os
original_read = os.read
calls = 0
def broken_read(fd, count):
    global calls
    calls += 1
    if calls > 1: raise OSError('injected read failure')
    return original_read(fd, count)
os.read = broken_read
""",
        "permission": """
import os
def denied_open(path, flags):
    raise PermissionError('injected permission denial')
os.open = denied_open
""",
        "timeout": "import time; time.sleep(30)",
        "bad_protocol": "import sys; print('{incomplete'); sys.exit(0)",
    }
    if fault == "timeout":
        local_ops.env.timeout = 1
    _instrument_scanner(monkeypatch, setups[fault])
    with patch.object(
        local_ops.env, "_run_bash", wraps=local_ops.env._run_bash
    ) as launches:
        result = local_ops.read_file("notes.txt")
    assert result.error and result.error.startswith("Failed to read file:")
    assert result.content == "" and result.total_lines == 0 and result.file_size == 0
    assert not result.truncated and not result.is_binary
    assert launches.call_count == 1
    if fault == "timeout":
        assert "timed out" in result.error
    if fault == "permission":
        assert "permission denial" in result.error


@pytest.mark.linux_only
@pytest.mark.parametrize("shape", ["single_line", "many_lines"])
def test_400mb_scan_is_bounded_and_counts_only_completed_scan(
    local_ops, tmp_path, monkeypatch, shape, record_property
):
    size = 400 * 1024 * 1024
    p = tmp_path / "large.txt"
    block = b"x" * (1024 * 1024) if shape == "single_line" else b"x\n" * (512 * 1024)
    with p.open("wb") as f:
        for _ in range(400):
            f.write(block)
    # Python 3.14 reserves >128 MiB of virtual address space at startup on
    # this host. Bound additional VM and trace actual Python allocations.
    # A whole-file/whole-line allocation cannot pass. No timing SLA asserted.
    metrics = tmp_path / "resource.json"
    _instrument_scanner(
        monkeypatch,
        f"""
import atexit, base64, codecs, json, os, resource, tracemalloc
with open('/proc/self/statm') as f:
    baseline_vm = int(f.read().split()[0]) * os.sysconf('SC_PAGE_SIZE')
ceiling = baseline_vm + 128 * 1024 * 1024
resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))
tracemalloc.start()
def save_resources():
    peak = tracemalloc.get_traced_memory()[1]
    with open({str(metrics)!r}, 'w') as f:
        json.dump({{'baseline_vm': baseline_vm, 'vm_ceiling': ceiling,
                   'peak_python_bytes': peak, 'max_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}}, f)
atexit.register(save_resources)
""",
    )
    with patch.object(
        local_ops.env, "_run_bash", wraps=local_ops.env._run_bash
    ) as launches:
        result = local_ops.read_file("large.txt", offset=1, limit=1)
    if shape == "single_line":
        expected = ReadResult(
            content="1|" + "x" * 2000 + "... [truncated]", total_lines=0, file_size=size
        )
    else:
        expected = ReadResult(
            content="1|x\n2|",
            total_lines=size // 2,
            file_size=size,
            truncated=True,
            hint=f"Use offset=2 to continue reading (showing 1-1 of {size // 2} lines)",
        )
    assert result == expected, result.error
    assert launches.call_count == 1
    measurements = json.loads(metrics.read_text())
    assert measurements["peak_python_bytes"] < 8 * 1024 * 1024
    assert measurements["max_rss_kib"] < 128 * 1024
    record_property("resource_" + shape, json.dumps(measurements, sort_keys=True))
    print("RESOURCE", shape, json.dumps(measurements, sort_keys=True))
    if shape == "many_lines":
        last = size // 2
        with patch.object(
            local_ops.env, "_run_bash", wraps=local_ops.env._run_bash
        ) as launches:
            result = local_ops.read_file("large.txt", offset=last, limit=1)
        assert result == ReadResult(
            content=f"{last}|x\n{last + 1}|", total_lines=last, file_size=size
        )
        assert launches.call_count == 1
        measurements = json.loads(metrics.read_text())
        assert measurements["peak_python_bytes"] < 8 * 1024 * 1024
        assert measurements["max_rss_kib"] < 128 * 1024
        record_property("resource_last_page", json.dumps(measurements, sort_keys=True))
        print("RESOURCE", "last_page", json.dumps(measurements, sort_keys=True))


def test_configured_limits_and_utf16_rescue_ceiling(local_ops, tmp_path, monkeypatch):
    from tools import tool_output_limits

    monkeypatch.setattr(
        tool_output_limits,
        "_cached_limits",
        {"max_lines": 2, "max_line_length": 3, "max_bytes": 50000},
    )
    (tmp_path / "notes.txt").write_text("😀" * 100 + "\nsecond\nthird\n")
    result = local_ops.read_file("notes.txt", limit=9000)
    assert result == ReadResult(
        content="1|😀😀😀... [truncated]\n2|sec... [truncated]\n3|",
        total_lines=3,
        file_size=414,
        truncated=True,
        hint="Use offset=3 to continue reading (showing 1-2 of 3 lines)",
    )
    p = tmp_path / "too-large-utf16.txt"
    p.write_bytes(b"\xff\xfe" + b"a\x00" * (local_ops._UTF16_MAX_BYTES // 2))
    result = local_ops.read_file(str(p))
    assert result == ReadResult(
        is_binary=True,
        file_size=p.stat().st_size,
        error="Binary file (unknown binary, 10.0 MB) — cannot display as text.",
    )


def test_file_tool_guards_redaction_and_task_accounting(
    local_ops, tmp_path, monkeypatch
):
    import tools.file_tools as ft
    import tools.terminal_tool as tt
    from hermes_constants import get_hermes_home

    task = "local-read-transport-contract"
    monkeypatch.setattr(ft, "_get_file_ops", lambda task_id: local_ops)
    monkeypatch.setattr(tt, "_task_env_overrides", {task: {"cwd": str(tmp_path)}})
    dummy_secret = "sk-localReadFixture123456789012345"
    (tmp_path / "notes.txt").write_text("safe\n" + dummy_secret + "\n")
    with patch.object(local_ops.env, "execute", wraps=local_ops.env.execute) as calls:
        result = json.loads(ft.read_file_tool("notes.txt", task_id=task))
        assert calls.call_count == 1
        assert dummy_secret not in result["content"]
        assert "1|safe" in result["content"]
        assert ("notes.txt", 1, 2000) in ft._read_tracker[task]["read_history"]
        resolved = str(tmp_path / "notes.txt")
        assert resolved in ft._read_tracker[task]["read_timestamps"]
        calls.reset_mock()
        assert json.loads(ft.read_file_tool("notes.txt", task_id=task))["dedup"]
        assert calls.call_count == 0
        protected = get_hermes_home() / "auth.json"
        protected.write_text('{"fixture": "dummy"}')
        (tmp_path / ".env").write_text("DUMMY=fixture\n")
        (tmp_path / "credential-link").symlink_to(protected)
        for path in (str(protected), ".env", "credential-link"):
            blocked = json.loads(ft.read_file_tool(path, task_id=task))
            assert "denied" in blocked["error"].lower()
        assert calls.call_count == 0
    ft._read_tracker.pop(task, None)
