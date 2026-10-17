"""Strict, bounded byte-reading tests for profile-owned P02 evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import profiles


def _require_reader():
    reader = getattr(profiles, "read_profile_evidence", None)
    assert callable(reader), "expected P02 profile-owned strict evidence reader"
    return reader


def _manifest(path: str = "profiles/alpha/profile.json", fmt: str = "json"):
    return {
        "roster_root": "profiles",
        "files": [{"file_ref": "file:alpha", "relative_path": path, "format": fmt}],
    }


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "approved"
    (root / "profiles/alpha").mkdir(parents=True)
    return root


def _require_safe_read_support() -> None:
    if (
        not hasattr(profiles.os, "O_NOFOLLOW")
        or not hasattr(profiles.os, "O_DIRECTORY")
        or profiles.os.open not in getattr(profiles.os, "supports_dir_fd", set())
        or profiles.os.stat not in getattr(profiles.os, "supports_dir_fd", set())
        or profiles.os.stat
        not in getattr(profiles.os, "supports_follow_symlinks", set())
        or profiles.os.listdir not in getattr(profiles.os, "supports_fd", set())
    ):
        pytest.skip("explicit no-follow directory-relative reads are unsupported here")


def test_reader_distinguishes_all_file_states_and_binds_exact_bytes(
    tmp_path: Path,
) -> None:
    _require_safe_read_support()
    root = _root(tmp_path)
    (root / "profiles/alpha/profile.json").write_bytes(b"{}")
    (root / "profiles/alpha/empty.json").write_bytes(b"")
    (root / "profiles/alpha/broken.json").write_bytes(b"{bad")
    (root / "profiles/alpha/unreadable.json").mkdir()
    result = _require_reader()(
        root,
        {
            "roster_root": "profiles",
            "files": [
                {
                    "file_ref": "file:empty",
                    "relative_path": "profiles/alpha/profile.json",
                    "format": "json",
                },
                {
                    "file_ref": "file:populated",
                    "relative_path": "profiles/alpha/profile.json",
                    "format": "json",
                },
                {
                    "file_ref": "file:bad",
                    "relative_path": "profiles/alpha/broken.json",
                    "format": "json",
                },
                {
                    "file_ref": "file:zero",
                    "relative_path": "profiles/alpha/empty.json",
                    "format": "json",
                },
                {
                    "file_ref": "file:missing",
                    "relative_path": "profiles/alpha/missing.json",
                    "format": "json",
                },
                {
                    "file_ref": "file:unreadable",
                    "relative_path": "profiles/alpha/unreadable.json",
                    "format": "json",
                },
            ],
        },
    )
    by_ref = {item.file_ref: item for item in result.files}
    assert by_ref["file:empty"].status == "valid_empty"
    assert by_ref["file:populated"].status == "valid_empty"
    assert by_ref["file:bad"].status == "malformed"
    assert by_ref["file:zero"].status == "malformed"
    assert by_ref["file:missing"].status == "missing"
    assert by_ref["file:unreadable"].status == "unreadable"
    assert by_ref["file:empty"].byte_digest.startswith("sha256:")
    assert by_ref["file:missing"].byte_digest is None
    assert by_ref["file:bad"].byte_length == 4


@pytest.mark.parametrize(
    ("relative_path", "expected"),
    [
        ("../outside.json", "PATH_ESCAPE"),
        ("/tmp/outside.json", "PATH_ESCAPE"),
        ("profiles/alpha/.env", "FORBIDDEN_SOURCE_PATH"),
        ("profiles/alpha/.env.local", "FORBIDDEN_SOURCE_PATH"),
        ("profiles/alpha/auth.json", "FORBIDDEN_SOURCE_PATH"),
        ("profiles/alpha/sessions/state.json", "FORBIDDEN_SOURCE_PATH"),
        ("profiles/alpha/config.yaml", "FORBIDDEN_SOURCE_PATH"),
        ("profiles/alpha/config.json", "FORBIDDEN_SOURCE_PATH"),
        ("profiles/alpha/secrets.json", "FORBIDDEN_SOURCE_PATH"),
        ("profiles/alpha/account.json", "FORBIDDEN_SOURCE_PATH"),
        ("profiles/alpha/models/model.json", "FORBIDDEN_SOURCE_PATH"),
    ],
)
def test_reader_refuses_escape_and_sensitive_source_paths(
    tmp_path: Path, relative_path: str, expected: str
) -> None:
    root = _root(tmp_path)
    with pytest.raises(profiles.ProfileEvidenceReadError) as raised:
        _require_reader()(root, _manifest(relative_path))
    assert raised.value.code == expected
    assert str(root) not in str(raised.value)


def test_reader_refuses_symlink_escape_and_hardlinks(tmp_path: Path) -> None:
    _require_safe_read_support()
    root = _root(tmp_path)
    root_alias = tmp_path / "approved-alias"
    root_alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(profiles.ProfileEvidenceReadError) as root_symlink:
        _require_reader()(root_alias, _manifest())
    assert root_symlink.value.code == "SYMLINK_REFUSED"

    outside = tmp_path / "outside.json"
    outside.write_text('{"secret":"sentinel"}', encoding="utf-8")
    (root / "profiles/alpha/link.json").symlink_to(outside)
    with pytest.raises(profiles.ProfileEvidenceReadError) as symlink:
        _require_reader()(root, _manifest("profiles/alpha/link.json"))
    assert symlink.value.code == "SYMLINK_REFUSED"

    hardlink = root / "profiles/alpha/hardlink.json"
    hardlink.hardlink_to(outside)
    with pytest.raises(profiles.ProfileEvidenceReadError) as linked:
        _require_reader()(root, _manifest("profiles/alpha/hardlink.json"))
    assert linked.value.code == "SYMLINK_REFUSED"


def test_reader_manifest_rejects_unknown_fields_duplicate_refs_and_windows_paths(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    cases = [
        {"roster_root": "profiles", "files": [], "include_all": True},
        {
            "roster_root": "profiles",
            "files": [
                {
                    "file_ref": "file:alpha",
                    "relative_path": "profiles/alpha/a.json",
                    "format": "json",
                },
                {
                    "file_ref": "file:alpha",
                    "relative_path": "profiles/alpha/b.json",
                    "format": "json",
                },
            ],
        },
        _manifest("C:\\outside.json"),
        _manifest("profiles\\alpha\\profile.json"),
    ]
    for manifest in cases:
        with pytest.raises(profiles.ProfileEvidenceReadError) as raised:
            _require_reader()(root, manifest)
        assert raised.value.code in {"INVALID_MANIFEST", "PATH_ESCAPE"}
        assert str(root) not in str(raised.value)


def test_reader_supports_empty_yaml_and_rejects_duplicate_yaml_keys(
    tmp_path: Path,
) -> None:
    _require_safe_read_support()
    root = _root(tmp_path)
    (root / "profiles/alpha/empty.yaml").write_text("", encoding="utf-8")
    (root / "profiles/alpha/duplicate.yaml").write_text(
        "x: 1\nx: 2\n", encoding="utf-8"
    )
    alias_lines = ["base: &base [x]"]
    for index in range(1, 30):
        previous = "base" if index == 1 else f"a{index - 1}"
        alias_lines.append(f"level{index}: &a{index} [*{previous}, *{previous}]")
    aliases = "\n".join(alias_lines) + "\n"
    (root / "profiles/alpha/aliases.yaml").write_text(aliases, encoding="utf-8")
    result = _require_reader()(
        root,
        {
            "roster_root": "profiles",
            "files": [
                {
                    "file_ref": "file:empty-yaml",
                    "relative_path": "profiles/alpha/empty.yaml",
                    "format": "yaml",
                },
                {
                    "file_ref": "file:duplicate-yaml",
                    "relative_path": "profiles/alpha/duplicate.yaml",
                    "format": "yaml",
                },
                {
                    "file_ref": "file:aliases-yaml",
                    "relative_path": "profiles/alpha/aliases.yaml",
                    "format": "yaml",
                },
            ],
        },
    )
    assert [item.status for item in result.files] == [
        "valid_empty",
        "malformed",
        "malformed",
    ]


def test_reader_refuses_path_shaped_file_reference(tmp_path: Path) -> None:
    root = _root(tmp_path)
    unsafe_ref = "/tmp/private-profile.json"
    with pytest.raises(profiles.ProfileEvidenceReadError) as raised:
        _require_reader()(
            root,
            {
                "roster_root": "profiles",
                "files": [
                    {
                        "file_ref": unsafe_ref,
                        "relative_path": "profiles/alpha/profile.json",
                        "format": "json",
                    }
                ],
            },
        )
    assert raised.value.code == "INVALID_MANIFEST"
    assert unsafe_ref not in str(raised.value)
    assert unsafe_ref not in getattr(raised.value, "file_ref", "")


def test_reader_opens_fifo_nonblocking_and_records_it_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_safe_read_support()
    if not hasattr(profiles.os, "mkfifo") or not hasattr(profiles.os, "O_NONBLOCK"):
        pytest.skip("nonblocking FIFO fixtures are unavailable here")
    root = _root(tmp_path)
    fifo = root / "profiles/alpha/stream.json"
    profiles.os.mkfifo(fifo)
    reader = _require_reader()
    manifest = {
        "roster_root": "profiles",
        "files": [
            {
                "file_ref": "file:fifo",
                "relative_path": "profiles/alpha/stream.json",
                "format": "json",
            }
        ],
    }
    static_result = reader(root, manifest)
    assert static_result.files[0].status == "unreadable"

    # Model a swap to a FIFO between the no-follow stat and open. The open
    # guard proves the race path uses O_NONBLOCK before fstat rejects it.
    regular = root / "profiles/alpha/regular.json"
    regular.write_text("{}", encoding="utf-8")
    original_open = profiles.os.open
    original_stat = profiles.os.stat
    regular_stat = regular.stat(follow_symlinks=False)
    opens = 0

    def guarded_open(path, flags, *args, **kwargs):
        nonlocal opens
        if path == "stream.json":
            opens += 1
            assert flags & profiles.os.O_NONBLOCK
        return original_open(path, flags, *args, **kwargs)

    def racing_stat(path, *args, **kwargs):
        if path == "stream.json":
            return regular_stat
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(profiles.os, "open", guarded_open)
    monkeypatch.setattr(profiles.os, "stat", racing_stat)
    monkeypatch.setattr(
        profiles.os,
        "supports_dir_fd",
        set(profiles.os.supports_dir_fd) | {guarded_open, racing_stat},
    )
    monkeypatch.setattr(
        profiles.os,
        "supports_follow_symlinks",
        set(profiles.os.supports_follow_symlinks) | {racing_stat},
    )
    result = reader(root, manifest)
    assert opens == 1
    assert result.files[0].status == "unreadable"


def test_reader_stops_roster_enumeration_at_the_declared_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_safe_read_support()
    root = _root(tmp_path)
    original_scandir = profiles.os.scandir
    seen = 0
    scan_fd_seen = None

    class SyntheticEntries:
        def __init__(self, fd: int) -> None:
            self.fd = fd

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal seen
            seen += 1
            return type("Entry", (), {"name": f"entry-{seen}"})()

    def bounded_scandir(path):
        nonlocal scan_fd_seen
        if isinstance(path, int):
            scan_fd_seen = path
            return SyntheticEntries(path)
        return original_scandir(path)

    monkeypatch.setattr(profiles.os, "scandir", bounded_scandir)
    monkeypatch.setattr(
        profiles.os,
        "supports_fd",
        set(profiles.os.supports_fd) | {bounded_scandir},
    )
    with pytest.raises(profiles.ProfileEvidenceReadError) as raised:
        _require_reader()(root, {"roster_root": "profiles", "files": []})
    assert raised.value.code == "ROSTER_UNSAFE_ENTRY"
    assert seen == profiles._PROFILE_EVIDENCE_MAX_ITEMS + 1
    assert scan_fd_seen is not None
    try:
        profiles.os.fstat(scan_fd_seen)
    except OSError:
        pass
    else:
        profiles.os.close(scan_fd_seen)
        pytest.fail("roster enumeration leaked its duplicated descriptor")


def test_reader_rejects_duplicate_json_nonfinite_and_oversized_inputs(
    tmp_path: Path,
) -> None:
    _require_safe_read_support()
    root = _root(tmp_path)
    duplicate = root / "profiles/alpha/duplicate.json"
    duplicate.write_text('{"x":1,"x":2}', encoding="utf-8")
    nonfinite = root / "profiles/alpha/nonfinite.json"
    nonfinite.write_text('{"x":NaN}', encoding="utf-8")
    oversized = root / "profiles/alpha/oversized.json"
    with oversized.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)
    result = _require_reader()(
        root,
        {
            "roster_root": "profiles",
            "files": [
                {
                    "file_ref": "file:duplicate",
                    "relative_path": "profiles/alpha/duplicate.json",
                    "format": "json",
                },
                {
                    "file_ref": "file:nan",
                    "relative_path": "profiles/alpha/nonfinite.json",
                    "format": "json",
                },
            ],
        },
    )
    states = {item.file_ref: item.status for item in result.files}
    assert states == {"file:duplicate": "malformed", "file:nan": "malformed"}
    with pytest.raises(profiles.ProfileEvidenceReadError) as raised:
        _require_reader()(
            root,
            {
                "roster_root": "profiles",
                "files": [
                    {
                        "file_ref": "file:large",
                        "relative_path": "profiles/alpha/oversized.json",
                        "format": "json",
                    }
                ],
            },
        )
    assert raised.value.code == "SOURCE_TOO_LARGE"


def test_reader_refuses_when_no_follow_primitives_are_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    monkeypatch.setattr(profiles.os, "supports_dir_fd", set())
    with pytest.raises(profiles.ProfileEvidenceReadError) as raised:
        _require_reader()(root, {"roster_root": "profiles", "files": []})
    assert raised.value.code == "SAFE_READ_UNSUPPORTED"


def test_tolerant_profile_listing_reader_keeps_legacy_empty_fallback(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "legacy"
    profile.mkdir()
    (profile / "profile.yaml").write_text("description: [broken\n", encoding="utf-8")
    assert profiles.read_profile_meta(profile) == {
        "description": "",
        "description_auto": False,
        "display_name": "",
    }
