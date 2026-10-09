"""Direct import-publication coupling with inert ZIP members and errno."""
import errno
import io
import os
import stat
import zipfile
from pathlib import Path

import pytest
from hermes_cli import backup

def archive():
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as zipped:
        zipped.writestr("config.yaml", b"NEW-complete\n")
    raw.seek(0)
    return zipfile.ZipFile(raw)

@pytest.mark.parametrize("kind", ["exdev", "ebusy"])
def test_extract_fallback_failure_preserves_old_target_and_symlink(tmp_path, monkeypatch, kind):
    tmp_path = tmp_path / "publication"
    tmp_path.mkdir()
    target, link = tmp_path / "real.txt", tmp_path / "config.yaml"
    old = b"OLD-complete\n"
    target.write_bytes(old)
    link.symlink_to(target)
    monkeypatch.setattr("utils.os.replace", lambda *args: (_ for _ in ()).throw(OSError(errno.EXDEV if kind == "exdev" else errno.EBUSY, "injected rename")))
    def copyfile(src, dst, **kwargs):
        Path(dst).write_bytes(b"NEW")
        raise OSError(errno.ENOSPC, "injected copy")
    import shutil
    original_copyfileobj = shutil.copyfileobj
    def copyfileobj(src, dst, *args, **kwargs):
        if isinstance(src, zipfile.ZipExtFile):
            return original_copyfileobj(src, dst, *args, **kwargs)
        dst.write(b"NEW")
        raise OSError(errno.ENOSPC, "injected copy")
    monkeypatch.setattr("utils.shutil.copyfile", copyfile)
    monkeypatch.setattr("utils.shutil.copyfileobj", copyfileobj)
    with archive() as zipped, pytest.raises(OSError):
        backup._extract_member_atomically(zipped, "config.yaml", link, 0o640)
    print({"pre": old, "post": target.read_bytes(), "kind": kind})
    assert target.read_bytes() == old and link.is_symlink()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.yaml", "real.txt"]

@pytest.mark.parametrize("publication", ["rename", "exdev"])
def test_extract_success_preserves_mode_and_owner_order(tmp_path, monkeypatch, publication):
    tmp_path = tmp_path / "publication"
    tmp_path.mkdir()
    target = tmp_path / "config.yaml"
    target.write_bytes(b"OLD-complete\n")
    target.chmod(0o6640)
    if publication == "exdev":
        real_replace = os.replace
        renames = []
        def replace(src, dst):
            renames.append((src, dst))
            if len(renames) == 1:
                raise OSError(errno.EXDEV, "injected first rename")
            return real_replace(src, dst)
        monkeypatch.setattr("utils.os.replace", replace)
    calls = []
    monkeypatch.setattr(backup, "_preserve_file_owner", lambda path: (123, 456))
    monkeypatch.setattr(backup, "_restore_file_owner", lambda path, owner: calls.append(("owner", owner)))
    restore_mode = backup._restore_file_mode
    def mode(path, value):
        calls.append(("mode", value))
        restore_mode(path, value)
    monkeypatch.setattr(backup, "_restore_file_mode", mode)
    with archive() as zipped:
        backup._extract_member_atomically(zipped, "config.yaml", target, 0o600)
    assert target.read_bytes() == b"NEW-complete\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert calls == [("owner", (123, 456)), ("mode", 0o640)]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.yaml"]
