"""Real-process metadata transactions against disposable profiles only."""
import multiprocessing
import os
from pathlib import Path

import pytest
import yaml

from hermes_cli import profiles

REF_A = "specialist-descriptor:" + "a" * 64
REF_B = "specialist-descriptor:" + "b" * 64


def _writer(home, operation, first, reached, release, results):
    os.environ["HERMES_HOME"] = home
    import utils
    from hermes_cli import profiles as p

    original_write = utils.atomic_yaml_write

    def paused_write(*args, **kwargs):
        reached.set()
        if first:
            assert release.wait(15)
        return original_write(*args, **kwargs)

    if first:
        utils.atomic_yaml_write = paused_write
    else:
        original_try = p._try_profile_metadata_lock

        def observed_try(*args):
            try:
                return original_try(*args)
            except BlockingIOError:
                reached.set()
                raise
        p._try_profile_metadata_lock = observed_try
    try:
        if operation == "cas-a":
            p.set_specialist_descriptor_ref("default", REF_A)
        elif operation == "cas-b":
            p.set_specialist_descriptor_ref("default", REF_B)
        elif operation == "clear":
            p.clear_specialist_descriptor_ref("default", REF_A)
        elif operation == "replace":
            p.set_specialist_descriptor_ref("default", REF_B, expected_current=REF_A)
        elif operation == "ui":
            import tui_gateway.server as srv
            result = srv._methods["profiles.configure"]("configure", {"name": "default", "ui_meta": {"pet": "fox"}})
            assert result["result"]["applied"]["ui_meta"] is True
        elif operation == "description":
            p.write_profile_meta(Path(home), description="updated")
        elif operation == "role":
            p.set_role_contract_ref("default", "writer", "contract:one")
        results.put("success")
    except Exception as exc:
        results.put(str(exc))


def _race(tmp_path, first_operation, second_operation, *, alias=False, initial=None):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "profile.yaml").write_text(yaml.safe_dump(initial or {"description": "before"}), encoding="utf-8")
    second_home = home
    if alias:
        second_home = tmp_path / "alias"
        if alias == "file":
            second_home.mkdir()
            target = tmp_path / "shared-profile.yaml"
            (home / "profile.yaml").rename(target)
            (home / "profile.yaml").symlink_to(target)
            (second_home / "profile.yaml").symlink_to(target)
        else:
            second_home.symlink_to(home, target_is_directory=True)
    ctx = multiprocessing.get_context("spawn")
    first_ready, second_ready, release = (ctx.Event() for _ in range(3))
    results = ctx.Queue()
    first = ctx.Process(target=_writer, args=(str(home), first_operation, True, first_ready, release, results))
    second = ctx.Process(target=_writer, args=(str(second_home), second_operation, False, second_ready, release, results))
    try:
        first.start()
        assert first_ready.wait(15), "first writer did not reach publication"
        second.start()
        assert second_ready.wait(15), "second writer neither contended nor reached publication"
        release.set()
        outcomes = [results.get(timeout=15), results.get(timeout=15)]
        first.join(15)
        second.join(15)
        assert first.exitcode == second.exitcode == 0
        return outcomes, yaml.safe_load((home / "profile.yaml").read_text())
    finally:
        release.set()
        for process in (first, second):
            if process.pid and process.is_alive():
                process.terminate()
                process.join(5)


def test_same_expected_cas_has_exactly_one_success(tmp_path):
    outcomes, metadata = _race(tmp_path, "cas-a", "cas-b")
    assert sorted(outcomes) == ["SPECIALIST_DESCRIPTOR_BINDING_CONFLICT", "success"]
    assert metadata["role_contract_refs"]["specialist-v1"] == REF_A


def test_disjoint_description_and_role_updates_survive(tmp_path):
    outcomes, metadata = _race(tmp_path, "description", "role")
    assert outcomes == ["success", "success"]
    assert metadata["description"] == "updated"
    assert metadata["role_contract_refs"] == {"writer": "contract:one"}


def test_clear_and_set_share_the_expected_value_transaction(tmp_path):
    outcomes, metadata = _race(
        tmp_path, "clear", "replace", initial={"role_contract_refs": {"specialist-v1": REF_A}}
    )
    assert sorted(outcomes) == ["SPECIALIST_DESCRIPTOR_BINDING_CONFLICT", "success"]
    assert "role_contract_refs" not in metadata


def test_gateway_ui_and_description_updates_survive(tmp_path):
    outcomes, metadata = _race(tmp_path, "description", "ui")
    assert outcomes == ["success", "success"]
    assert metadata["description"] == "updated"
    assert metadata["ui_meta"] == {"pet": "fox"}
    assert metadata["_ui_meta_revisions"] == {"pet": 1}


@pytest.mark.linux_only
@pytest.mark.parametrize("alias", [False, True])
def test_posix_replacement_and_directory_alias_share_lock(tmp_path, alias):
    outcomes, metadata = _race(tmp_path, "description", "role", alias=alias)
    assert outcomes == ["success", "success"]
    assert metadata["description"] == "updated"
    assert metadata["role_contract_refs"] == {"writer": "contract:one"}


@pytest.mark.macos_only
def test_macos_native_metadata_lock(tmp_path):
    outcomes, metadata = _race(tmp_path, "cas-a", "cas-b", alias=True)
    assert sorted(outcomes) == ["SPECIALIST_DESCRIPTOR_BINDING_CONFLICT", "success"]
    assert metadata["role_contract_refs"]["specialist-v1"] == REF_A


@pytest.mark.windows_only
def test_windows_native_metadata_lock(tmp_path):
    outcomes, metadata = _race(tmp_path, "cas-a", "cas-b")
    assert sorted(outcomes) == ["SPECIALIST_DESCRIPTOR_BINDING_CONFLICT", "success"]
    assert metadata["role_contract_refs"]["specialist-v1"] == REF_A


def _hold_lock(home, ready, release):
    from hermes_cli import profiles as p
    with p._profile_metadata_lock(Path(home), 5):
        ready.set()
        assert release.wait(30)


def _timeout_and_death(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold_lock, args=(str(tmp_path), ready, release))
    holder.start()
    try:
        assert ready.wait(15)
        with pytest.raises(TimeoutError, match="PROFILE_METADATA_LOCK_TIMEOUT"):
            profiles.update_profile_metadata(tmp_path, lambda data: data.update(description="blocked"), timeout=0.05)
        assert not (tmp_path / "profile.yaml").exists()
        holder.terminate()
        holder.join(10)
        assert not holder.is_alive()
        profiles.write_profile_meta(tmp_path, description="after process death")
        assert profiles.read_profile_meta(tmp_path)["description"] == "after process death"
    finally:
        if holder.is_alive():
            holder.terminate()
            holder.join(5)


def test_lock_timeout_and_process_death_release(tmp_path):
    _timeout_and_death(tmp_path)


@pytest.mark.windows_only
def test_windows_native_timeout_and_process_death(tmp_path):
    _timeout_and_death(tmp_path)


@pytest.mark.macos_only
def test_macos_native_timeout_and_process_death(tmp_path):
    _timeout_and_death(tmp_path)


@pytest.mark.parametrize("raw", [
    "", "null\n", "false\n", "[]\n", "description: [broken\n",
    "role_contract_refs: []\n", "ui_meta: null\n", "_ui_meta_revisions: []\n",
    "_ui_meta_revisions: {pet: -1}\n", "_ui_meta_revisions: {pet: true}\n",
    "_ui_meta_revisions: {pet: '1'}\n",
])
@pytest.mark.parametrize("writer", ["description", "role", "set", "clear", "ui"])
def test_malformed_metadata_is_preserved(tmp_path, monkeypatch, raw, writer):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "profile.yaml"
    path.write_text(raw, encoding="utf-8")
    if writer == "ui":
        import tui_gateway.server as srv
        result = srv._methods["profiles.configure"]("configure", {"name": "default", "ui_meta": {"pet": "fox"}})
        assert result["result"]["applied"]["ui_meta"] is False
    else:
        with pytest.raises(ValueError, match="PROFILE_METADATA_(INVALID|UNREADABLE)"):
            if writer == "description":
                profiles.write_profile_meta(tmp_path, description="new")
            elif writer == "role":
                profiles.set_role_contract_ref("default", "writer", "ref")
            elif writer == "set":
                profiles.set_specialist_descriptor_ref("default", REF_A)
            else:
                profiles.clear_specialist_descriptor_ref("default", REF_A)
    assert path.read_text(encoding="utf-8") == raw


@pytest.mark.linux_only
@pytest.mark.parametrize("filename,kind", [("profile.yaml", "hardlink"), (".profile.yaml.metadata.lock", "hardlink"), (".profile.yaml.metadata.lock", "symlink")])
def test_file_aliases_are_refused_without_touching_target(tmp_path, filename, kind):
    target = tmp_path / "other"
    target.write_text("description: preserved\n", encoding="utf-8")
    path = tmp_path / filename
    if kind == "symlink":
        path.symlink_to(target)
    else:
        os.link(target, path)
    with pytest.raises(ValueError, match="PROFILE_METADATA_FILE_ALIAS_OR_INVALID"):
        profiles.write_profile_meta(tmp_path, description="bad")
    assert target.read_text(encoding="utf-8") == "description: preserved\n"


def test_directory_identity_is_rechecked_before_publication(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    def replace_directory(metadata):
        home.rename(tmp_path / "old")
        home.mkdir()
        metadata["description"] = "wrong incarnation"
    with pytest.raises(ValueError, match="PROFILE_METADATA_IDENTITY_CHANGED"):
        profiles.update_profile_metadata(home, replace_directory)
    assert not (home / "profile.yaml").exists()
    assert not (tmp_path / "old" / "profile.yaml").exists()


def test_metadata_replacement_during_callback_is_refused(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text("description: original\n")
    def replace_file(metadata):
        replacement = tmp_path / "replacement"
        replacement.write_text("description: externally replaced\n")
        os.replace(replacement, path)
        metadata["description"] = "stale"
    with pytest.raises(ValueError, match="PROFILE_METADATA_IDENTITY_CHANGED"):
        profiles.update_profile_metadata(tmp_path, replace_file)
    assert path.read_text() == "description: externally replaced\n"


@pytest.mark.linux_only
def test_waiting_writer_refuses_replaced_lock_file(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    original_try = profiles._try_profile_metadata_lock
    contended = threading.Event()
    def observed_try(*args):
        try:
            return original_try(*args)
        except BlockingIOError:
            contended.set()
            raise
    monkeypatch.setattr(profiles, "_try_profile_metadata_lock", observed_try)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with profiles._profile_metadata_lock(tmp_path, 5):
            future = executor.submit(profiles.write_profile_meta, tmp_path, description="wrong lock")
            assert contended.wait(10)
            replacement = tmp_path / "replacement-lock"
            replacement.touch()
            os.replace(replacement, tmp_path / ".profile.yaml.metadata.lock")
        with pytest.raises(ValueError, match="PROFILE_METADATA_IDENTITY_CHANGED"):
            future.result(timeout=10)
    assert not (tmp_path / "profile.yaml").exists()


@pytest.mark.linux_only
def test_missing_backend_fails_explicitly(tmp_path, monkeypatch):
    import builtins
    original_import = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("backend unavailable")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", unavailable)
    with pytest.raises(RuntimeError, match="PROFILE_METADATA_LOCK_UNSUPPORTED"):
        profiles.write_profile_meta(tmp_path, description="must not write")
    assert not (tmp_path / "profile.yaml").exists()


def test_thread_writers_share_the_file_transaction(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    barrier = threading.Barrier(2)
    def set_reference(reference):
        barrier.wait(timeout=10)
        try:
            profiles.set_specialist_descriptor_ref("default", reference)
            return "success"
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(set_reference, [REF_A, REF_B]))
    assert sorted(outcomes) == ["SPECIALIST_DESCRIPTOR_BINDING_CONFLICT", "success"]


def test_generic_role_setter_keeps_unconditional_semantics(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    profiles.set_specialist_descriptor_ref("default", REF_A)
    profiles.set_role_contract_ref("default", "specialist-v1", REF_B)
    assert profiles.get_specialist_descriptor_ref("default") == REF_B


@pytest.mark.linux_only
@pytest.mark.parametrize("operations", [("cas-a", "cas-b"), ("description", "role")])
def test_distinct_profile_file_aliases_share_canonical_target(tmp_path, operations):
    outcomes, metadata = _race(tmp_path, *operations, alias="file")
    if operations[0] == "cas-a":
        assert sorted(outcomes) == ["SPECIALIST_DESCRIPTOR_BINDING_CONFLICT", "success"]
    else:
        assert outcomes == ["success", "success"]
        assert metadata["description"] == "updated"
        assert metadata["role_contract_refs"] == {"writer": "contract:one"}
    assert (tmp_path / "profile" / "profile.yaml").is_symlink()
    assert (tmp_path / "alias" / "profile.yaml").is_symlink()


@pytest.mark.linux_only
def test_file_alias_retarget_while_waiting_is_refused(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    profile = tmp_path / "profile"
    profile.mkdir()
    target = tmp_path / "target.yaml"
    target.write_text("description: original\n")
    replacement = tmp_path / "other.yaml"
    replacement.write_text("description: other\n")
    alias = profile / "profile.yaml"
    alias.symlink_to(target)
    original_try = profiles._try_profile_metadata_lock
    contended = threading.Event()
    def observed_try(*args):
        try:
            return original_try(*args)
        except BlockingIOError:
            contended.set()
            raise
    monkeypatch.setattr(profiles, "_try_profile_metadata_lock", observed_try)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with profiles._profile_metadata_lock(profile, 5):
            future = executor.submit(profiles.write_profile_meta, profile, description="stale")
            assert contended.wait(10)
            alias.unlink()
            alias.symlink_to(replacement)
        with pytest.raises(ValueError, match="PROFILE_METADATA_IDENTITY_CHANGED"):
            future.result(timeout=10)
    assert target.read_text() == "description: original\n"
    assert replacement.read_text() == "description: other\n"


def test_gateway_write_failure_does_not_report_uncommitted_revisions(tmp_path, monkeypatch):
    import tui_gateway.server as srv
    import utils
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    def failed_write(*args, **kwargs):
        raise OSError("simulated publication failure")
    monkeypatch.setattr(utils, "atomic_yaml_write", failed_write)
    result = srv._methods["profiles.configure"]("configure", {"name": "default", "ui_meta": {"pet": "fox"}})
    applied = result["result"]["applied"]
    assert applied["ui_meta"] is False
    assert "ui_meta_revisions" not in applied
    assert not (tmp_path / "profile.yaml").exists()
