"""Flat attachment references expose complete bytes without clobbering peers."""
from pathlib import Path
import stat

import pytest

from tui_gateway import server
from tests.tui_gateway.test_attachment_staging_integrity import (
    staging, data_url, image_bytes, rpc, upload,
)


def files(root):
    return {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_file_publication_is_flat_and_preserves_multisuffix_collisions(staging):
    home, _ = staging
    first = rpc("file.attach", "a", name="bundle.tar.gz", data_url=data_url(b"first", "application/octet-stream"))
    second = rpc("file.attach", "b", name="bundle.tar.gz", data_url=data_url(b"second", "application/octet-stream"))
    assert "error" not in first and "error" not in second
    one, two = (Path(x["result"]["path"]) for x in (first, second))
    assert one == home / "attachments" / "bundle.tar.gz"
    assert two.parent == one.parent and two != one
    assert two.name.endswith(".tar.gz")
    assert one.read_bytes() == b"first" and two.read_bytes() == b"second"
    assert not list((home / "attachments").glob("stage-*"))


@pytest.mark.linux_only
def test_regular_and_dangling_symlink_collisions_are_not_overwritten(staging):
    home, _ = staging
    root = home / "attachments"
    root.mkdir()
    regular = root / "kept.txt"
    regular.write_bytes(b"existing")
    dangling = root / "dangling.txt"
    dangling.symlink_to(root / "missing-target")
    before_link = dangling.lstat()
    for name in (regular.name, dangling.name):
        response = rpc("file.attach", "a", name=name, data_url=data_url(b"new", "text/plain"))
        assert "error" not in response, response
        result = Path(response["result"]["path"])
        assert result.parent == root and result.name != name
        assert result.read_bytes() == b"new"
    assert regular.read_bytes() == b"existing"
    assert dangling.is_symlink() and dangling.lstat().st_ino == before_link.st_ino
    assert not (root / "missing-target").exists()


@pytest.mark.parametrize("error", [OSError("controlled link refusal"), FileExistsError("controlled collision")])
def test_refused_publication_cleans_only_its_stage(staging, monkeypatch, error):
    home, sessions = staging
    kept = upload("a", image_bytes("red"))
    before = files(home)
    calls = []

    def refuse(source, target, **kwargs):
        calls.append((source, target))
        raise error

    monkeypatch.setattr(server.os, "link", refuse)
    response = rpc("file.attach", "b", name="refused.txt", data_url=data_url(b"new", "text/plain"))
    assert response.get("error"), response
    assert 1 <= len(calls) <= 32, "collision retries must remain bounded"
    assert files(home) == before
    assert kept.read_bytes() == image_bytes("red")
    assert sessions["b"]["attached_images"] == []
    assert not list((home / "attachments").glob("stage-*"))


@pytest.mark.linux_only
def test_stage_cleanup_failure_does_not_undo_published_bytes(staging, monkeypatch):
    home, sessions = staging
    original_unlink = Path.unlink
    attempts = []

    def retain_stage(path, *args, **kwargs):
        if path.parent.name.startswith("stage-"):
            attempts.append(path)
            raise OSError("controlled post-publication cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", retain_stage)
    path = upload("a", image_bytes("red"))
    assert attempts, "post-publication cleanup injection did not fire"
    assert path.parent == home / "images"
    assert path.read_bytes() == image_bytes("red")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sessions["a"]["attached_images"] == [str(path)]
    assert all(stage.read_bytes() == path.read_bytes() for stage in attempts)


def test_pdf_batch_pages_remain_private_until_batch_admission(staging):
    home, sessions = staging
    path = server._queue_attached_image(sessions["a"], image_bytes("red"), ".png", prefix="pdf_p1", queue=False)
    assert path.parent.parent == home / "images"
    assert path.parent.name.startswith("stage-")
    assert sessions["a"]["attached_images"] == []
    assert path.read_bytes() == image_bytes("red")


def test_stage_from_another_profile_cannot_be_published(staging):
    home, _ = staging
    owner = home / "attachments"
    stage = server._new_staged_attachment_path(owner, "owned.txt")
    stage.write_bytes(b"owned bytes")
    other = home.parent / "different-profile"
    other.mkdir()
    with pytest.raises(ValueError, match="selected profile root"):
        server._publish_staged_attachment(stage, other)
    assert stage.read_bytes() == b"owned bytes"
    assert list(other.iterdir()) == []


@pytest.mark.linux_only
def test_symlink_stage_cannot_publish_a_different_source(staging):
    home, _ = staging
    root = home / "attachments"
    stage = server._new_staged_attachment_path(root, "bad.txt")
    source = home / "not-an-upload.txt"
    source.write_bytes(b"not admitted")
    stage.symlink_to(source)
    with pytest.raises(ValueError, match="selected profile root"):
        server._publish_staged_attachment(stage, root)
    assert stage.is_symlink()
    assert source.read_bytes() == b"not admitted"
    assert not (root / "bad.txt").exists()
