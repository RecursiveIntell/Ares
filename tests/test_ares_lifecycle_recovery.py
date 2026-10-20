"""Inert filesystem and process-seam regression gates for local Ares lifecycle."""
from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ares_runtime.local_runtime import AresLocalPaths, AresLocalRuntime, AresLocalRuntimeError


class LifecycleRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = AresLocalPaths(self.root/"state", self.root/"data",
                                   self.root/"home", self.root/"bin/ares",
                                   self.root/"units/offline.service")
        self.runtime = AresLocalRuntime(self.paths)
        self.runtime._ensure_layout()
        self.subprocess_guard = patch("subprocess.run", side_effect=AssertionError("real subprocess forbidden"))
        self.subprocess_guard.start()
        self.addCleanup(self.subprocess_guard.stop)
        for revision in ("a"*40, "b"*40, "c"*40):
            self.release(revision)
        self.runtime._atomic_link(self.paths.current_link, self.runtime._release_source("b"*40).resolve())
        self.runtime._atomic_link(self.paths.previous_link, self.runtime._release_source("a"*40).resolve())

    def release(self, revision, *, complete=True):
        source = self.runtime._release_dir(revision)/"source"
        source.mkdir(parents=True, exist_ok=True)
        python = self.runtime._python_for(source)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("inert interpreter, never executed")
        record = {"revision": revision, "source": "inert-source", "installed_at": 1}
        if complete:
            record["runtime_binding"] = {"schema": "AresLocalRuntimeBindingV1", "source": str(source.resolve()), "controller_contract": 1}
        self.runtime._atomic_json(source.parent/"release.json", record)
        return source

    def pair(self, runtime=None):
        runtime = runtime or self.runtime
        return runtime.active_release()[0], runtime.previous_release()[0]

    def test_interrupted_rollback_recovers_original_pair_and_repeats(self):
        original = self.runtime._atomic_link
        injected = False
        def interrupt(path, target):
            nonlocal injected
            original(path, target)
            if not injected and path == self.paths.current_link and target == self.runtime._release_source("a"*40).resolve():
                injected = True
                raise KeyboardInterrupt("after current pointer publication")
        with patch.object(self.runtime, "_atomic_link", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.runtime.rollback()
        reopened = AresLocalRuntime(self.paths)
        self.assertEqual(self.pair(reopened), ("b"*40, "a"*40))
        self.assertEqual(reopened.rollback(), "a"*40)
        self.assertEqual(self.pair(reopened), ("a"*40, "b"*40))
        self.assertEqual(reopened.rollback(), "b"*40)

    def test_interrupted_activation_preserves_older_rollback_target(self):
        original = self.runtime._atomic_link
        injected = False
        def interrupt(path, target):
            nonlocal injected
            original(path, target)
            if not injected and path == self.paths.previous_link and target == self.runtime._release_source("b"*40).resolve():
                injected = True
                raise KeyboardInterrupt("after previous pointer publication")
        with patch.object(self.runtime, "_atomic_link", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.runtime._activate("c"*40)
        self.assertEqual(self.pair(AresLocalRuntime(self.paths)), ("b"*40, "a"*40))

    def test_reopen_recovers_durable_pending_transition(self):
        self.runtime._atomic_json(self.paths.data_root/"release-transition.json",
                                  {"schema": "AresLocalReleaseTransitionV1", "current": "b"*40, "previous": "a"*40})
        self.runtime._atomic_link(self.paths.current_link, self.runtime._release_source("a"*40).resolve())
        self.assertEqual(self.pair(AresLocalRuntime(self.paths)), ("b"*40, "a"*40))
        self.assertFalse((self.paths.data_root/"release-transition.json").exists())

    def test_malformed_pending_transition_refuses_launch_without_deleting_bytes(self):
        record = self.paths.data_root/"release-transition.json"
        record.write_text('{"schema":"unknown","current":"b","previous":null}')
        before = record.read_bytes()
        with self.assertRaises(AresLocalRuntimeError):
            AresLocalRuntime(self.paths).active_release()
        self.assertEqual(record.read_bytes(), before)
        self.assertTrue(self.runtime._release_source("b"*40).is_dir())

    def test_status_reports_recovered_pair_from_one_controller_entry(self):
        self.runtime._atomic_json(self.paths.data_root/"release-transition.json",
                                  {"schema":"AresLocalReleaseTransitionV1","current":"b"*40,"previous":"a"*40})
        self.runtime._atomic_link(self.paths.current_link, self.runtime._release_source("a"*40).resolve())
        with patch.object(self.runtime, "_read_config", return_value={"remote":"inert","branch":"main"}), patch.object(self.runtime, "_systemctl", return_value=False):
            result = self.runtime.status()
        self.assertEqual(result[:2], ["active: "+"b"*40, "previous: "+"a"*40])

    def test_failed_recovery_retains_journal_and_reports_uncertainty(self):
        journal = self.paths.data_root/"release-transition.json"
        self.runtime._atomic_json(journal, {"schema":"AresLocalReleaseTransitionV1","current":"b"*40,"previous":"a"*40})
        self.runtime._atomic_link(self.paths.current_link, self.runtime._release_source("a"*40).resolve())
        with patch.object(self.runtime, "_atomic_link", side_effect=OSError("inert disk failure")):
            with self.assertRaises(AresLocalRuntimeError):
                self.runtime.active_release()
        self.assertTrue(journal.exists())
        self.assertEqual(self.pair(AresLocalRuntime(self.paths)), ("b"*40, "a"*40))

    def test_ordinary_pointer_error_restores_exact_pair(self):
        original = self.runtime._atomic_link
        injected = False
        def fail(path, target):
            nonlocal injected
            original(path, target)
            if not injected and path == self.paths.current_link and target == self.runtime._release_source("a"*40).resolve():
                injected = True
                raise OSError("inert failure after write")
        with patch.object(self.runtime, "_atomic_link", side_effect=fail):
            with self.assertRaises(OSError):
                self.runtime.rollback()
        self.assertEqual(self.pair(), ("b"*40, "a"*40))

    def test_missing_final_binding_is_not_a_complete_release(self):
        source = self.release("c"*40, complete=False)
        with self.assertRaises(AresLocalRuntimeError):
            self.runtime._require_complete_release(source, desktop=False)

    def test_wrong_final_binding_is_not_a_complete_release(self):
        source = self.release("c"*40)
        record = json.loads((source.parent/"release.json").read_text())
        record["runtime_binding"]["source"] = str(self.paths.staging_dir/"deleted/source")
        self.runtime._atomic_json(source.parent/"release.json", record)
        with self.assertRaises(AresLocalRuntimeError):
            self.runtime._require_complete_release(source, desktop=False)

    def test_interrupted_finalization_is_rebuilt_before_reuse(self):
        import shutil
        shutil.rmtree(self.runtime._release_dir("c"*40))
        refreshes = []
        builds = []
        def run(command, **kwargs):
            if command[:2] == ["git", "clone"]:
                Path(command[-1]).mkdir(parents=True)
            return subprocess.CompletedProcess(command, 0, "", "")
        def build(source, *, desktop):
            builds.append(source)
            python = self.runtime._python_for(source)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("inert interpreter")
        def interrupted(source):
            refreshes.append(source)
            raise KeyboardInterrupt("before final binding")
        with patch.object(self.runtime, "_run", side_effect=run), patch.object(self.runtime, "_build_runtime", side_effect=build), patch.object(self.runtime, "_refresh_moved_editable_install", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.runtime._materialize("inert-source", "c"*40, desktop=False)
        self.assertEqual(self.pair(), ("b"*40, "a"*40))
        with patch.object(self.runtime, "_run", side_effect=run), patch.object(self.runtime, "_build_runtime", side_effect=build), patch.object(self.runtime, "_refresh_moved_editable_install", side_effect=lambda source: refreshes.append(source)):
            self.runtime._materialize("inert-source", "c"*40, desktop=False)
        self.assertEqual(len(refreshes), 2)
        self.assertEqual(len(builds), 2)
        self.runtime._require_complete_release(self.runtime._release_source("c"*40), desktop=False)
        self.runtime._activate("c"*40)
        self.assertEqual(self.pair(), ("c"*40, "b"*40))

    def test_complete_release_reuse_never_builds_or_rebinds(self):
        with patch.object(self.runtime, "_run", side_effect=AssertionError("reuse cannot acquire source")), patch.object(self.runtime, "_refresh_moved_editable_install", side_effect=AssertionError("immutable rebind")):
            self.runtime._materialize("unused", "c"*40, desktop=False)
        self.assertEqual(self.pair(), ("b"*40, "a"*40))

    def test_binding_from_an_older_controller_is_not_complete(self):
        source = self.release("c"*40)
        record = json.loads((source.parent/"release.json").read_text())
        record["runtime_binding"]["controller_contract"] = 0
        self.runtime._atomic_json(source.parent/"release.json", record)
        with self.assertRaises(AresLocalRuntimeError):
            self.runtime._require_complete_release(source, desktop=False)

    def test_active_incomplete_release_is_not_repaired_in_place(self):
        source = self.runtime._release_source("b"*40)
        self.runtime._python_for(source).unlink()
        before = (source.parent/"release.json").read_bytes()
        with patch.object(self.runtime, "_run", side_effect=AssertionError("source acquisition forbidden")):
            with self.assertRaises(AresLocalRuntimeError):
                self.runtime._materialize("unused", "b"*40, desktop=False)
        self.assertEqual((source.parent/"release.json").read_bytes(), before)
        self.assertEqual(self.pair(), ("b"*40, "a"*40))

    def test_incomplete_previous_cannot_be_selected_for_rollback(self):
        self.runtime._python_for(self.runtime._release_source("a"*40)).unlink()
        with self.assertRaises(AresLocalRuntimeError):
            self.runtime.rollback()
        self.assertEqual(self.pair(), ("b"*40, "a"*40))

    def test_uncertain_retirement_reports_complete_new_pair_truthfully(self):
        original = self.runtime._retire_release_transition
        def uncertain():
            original()
            raise OSError("inert retirement fsync uncertainty")
        with patch.object(self.runtime, "_retire_release_transition", side_effect=uncertain):
            with self.assertRaisesRegex(AresLocalRuntimeError, "retirement is uncertain"):
                self.runtime._activate("c"*40)
        self.assertEqual(self.pair(), ("c"*40, "b"*40))

    def test_first_activation_interrupt_restores_absent_pointer_pair(self):
        self.paths.current_link.unlink()
        self.paths.previous_link.unlink()
        original = self.runtime._atomic_link
        injected = False
        def interrupt(path, target):
            nonlocal injected
            original(path, target)
            if not injected and path == self.paths.current_link:
                injected = True
                raise KeyboardInterrupt("first activation interrupted")
        with patch.object(self.runtime, "_atomic_link", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.runtime._activate("c"*40)
        self.assertFalse(self.paths.current_link.is_symlink())
        self.assertFalse(self.paths.previous_link.is_symlink())

    def upstream_seams(self):
        def run(command, **kwargs):
            if command[:2] == ["git", "clone"]:
                Path(command[-1]).mkdir(parents=True)
            return subprocess.CompletedProcess(command, 0, "", "")
        def output(source, *args):
            if args == ("rev-parse", "FETCH_HEAD"):
                return "c"*40
            if args[0] == "merge-base":
                return "c"*40
            raise AssertionError(("unexpected git query", args))
        def cached(command, **kwargs):
            self.assertEqual(command[0], "git")
            self.assertIn("--cached", command)
            return subprocess.CompletedProcess(command, 0, "", "")
        return run, output, cached

    def upstream(self):
        return self.runtime._materialize_upstream_candidate(
            downstream_remote="inert-downstream", downstream_revision="d"*40,
            upstream_remote="inert-upstream", upstream_branch="main",
            upstream_revision="c"*40, desktop=False,
        )

    def test_upstream_finalization_interrupt_cannot_be_reused_without_binding(self):
        import shutil
        shutil.rmtree(self.runtime._release_dir("c"*40))
        run, output, cached = self.upstream_seams()
        refreshes = []
        def build(source, *, desktop):
            python = self.runtime._python_for(source)
            python.parent.mkdir(parents=True)
            python.write_text("inert interpreter")
        def interrupt(source):
            refreshes.append(source)
            raise KeyboardInterrupt("candidate finalization interrupted")
        with patch.object(self.runtime, "_run", side_effect=run), patch.object(self.runtime, "_git_output", side_effect=output), patch("subprocess.run", side_effect=cached), patch.object(self.runtime, "_build_runtime", side_effect=build), patch.object(self.runtime, "_refresh_moved_editable_install", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.upstream()
        with patch.object(self.runtime, "_run", side_effect=run), patch.object(self.runtime, "_git_output", side_effect=output), patch("subprocess.run", side_effect=cached), patch.object(self.runtime, "_build_runtime", side_effect=build), patch.object(self.runtime, "_refresh_moved_editable_install", side_effect=lambda source: refreshes.append(source)):
            self.assertEqual(self.upstream(), "c"*40)
        self.assertEqual(len(refreshes), 2)
        self.runtime._require_complete_release(self.runtime._release_source("c"*40), desktop=False)
        self.assertEqual(self.pair(), ("b"*40, "a"*40))

    def test_upstream_incomplete_existing_candidate_refuses_without_mutation(self):
        source = self.release("c"*40, complete=False)
        record = json.loads((source.parent/"release.json").read_text())
        record.update(upstream_revision="c"*40, downstream_revision="d"*40)
        self.runtime._atomic_json(source.parent/"release.json", record)
        before = (source.parent/"release.json").read_bytes()
        run, output, cached = self.upstream_seams()
        with patch.object(self.runtime, "_run", side_effect=run), patch.object(self.runtime, "_git_output", side_effect=output), patch("subprocess.run", side_effect=cached), patch.object(self.runtime, "_build_runtime", side_effect=AssertionError("existing candidate cannot be rebuilt")):
            with self.assertRaises(AresLocalRuntimeError):
                self.upstream()
        self.assertEqual((source.parent/"release.json").read_bytes(), before)
        self.assertEqual(self.pair(), ("b"*40, "a"*40))

    def service_case(self, action, *, active, enabled="disabled", fail_restart=False, bad_state=False):
        self.paths.unit_path.parent.mkdir(parents=True, exist_ok=True)
        self.paths.unit_path.write_text("inert unit")
        calls = []
        state = {"active": active}
        def systemctl(*args, required=True):
            calls.append(args)
            if args[0] == "restart":
                if fail_restart and required:
                    raise AresLocalRuntimeError("inert restart failure")
                state["active"] = True
            if args[0] == "is-active":
                return state["active"]
            return True
        def run(command, **kwargs):
            calls.append(tuple(str(x) for x in command))
            text = "unexpected output" if bad_state else ("LoadState=loaded\nActiveState="+("active" if state["active"] else "inactive")+"\nUnitFileState="+enabled+"\n")
            return subprocess.CompletedProcess(command, 0, text, "")
        with patch.object(self.runtime, "_systemctl", side_effect=systemctl), patch.object(self.runtime, "_run", side_effect=run), patch.object(self.runtime, "_systemd_environment", return_value={}), patch.object(self.runtime, "_install_gateway_unit"), patch.object(self.runtime, "_read_config", return_value={"remote":"inert","branch":"main","upstream_remote":"inert","upstream_branch":"main"}), patch.object(self.runtime, "_remote_revision", return_value="d"*40), patch.object(self.runtime, "_materialize_upstream_candidate", return_value="c"*40), patch("ares_runtime.local_runtime.time.sleep"):
            if fail_restart or bad_state:
                with self.assertRaises(AresLocalRuntimeError):
                    getattr(self.runtime, action)(**({"desktop":False} if action == "update" else {}))
            else:
                getattr(self.runtime, action)(**({"desktop":False} if action == "update" else {}))
        return calls

    def test_stopped_gateway_stays_stopped_on_update(self):
        calls = self.service_case("update", active=False)
        self.assertFalse(any(c[0] in {"restart","start","enable"} for c in calls), calls)
        self.assertEqual(self.pair(), ("c"*40, "b"*40))

    def test_stopped_gateway_stays_stopped_on_rollback(self):
        calls = self.service_case("rollback", active=False)
        self.assertFalse(any(c[0] in {"restart","start","enable"} for c in calls), calls)
        self.assertEqual(self.pair(), ("a"*40, "b"*40))

    def test_running_disabled_gateway_restarts_without_enabling(self):
        calls = self.service_case("update", active=True, enabled="disabled")
        self.assertEqual(sum(c[0] == "restart" for c in calls), 1)
        self.assertFalse(any(c[0] in {"enable","disable"} for c in calls), calls)

    def test_failed_running_handoff_restores_code_and_prior_running_state(self):
        calls = self.service_case("rollback", active=True, enabled="enabled", fail_restart=True)
        self.assertEqual(self.pair(), ("b"*40, "a"*40))
        self.assertEqual(sum(c[0] == "restart" for c in calls), 2)
        self.assertFalse(any(c[0] in {"enable","disable"} for c in calls), calls)

    def test_unknown_service_state_refuses_before_selection(self):
        self.service_case("update", active=False, bad_state=True)
        self.assertEqual(self.pair(), ("b"*40, "a"*40))

    def test_interrupted_running_handoff_restores_code_before_propagating_interrupt(self):
        self.paths.unit_path.parent.mkdir(parents=True)
        self.paths.unit_path.write_text("inert unit")
        calls = []
        def systemctl(*args, required=True):
            calls.append(args)
            if args[0] == "restart" and required:
                raise KeyboardInterrupt("handoff interrupted")
            return True
        with patch.object(self.runtime, "_gateway_state", return_value=(True, "enabled"), create=True), patch.object(self.runtime, "_systemctl", side_effect=systemctl), patch("ares_runtime.local_runtime.time.sleep"):
            with self.assertRaises(KeyboardInterrupt):
                self.runtime.rollback()
        self.assertEqual(self.pair(), ("b"*40, "a"*40))
        self.assertEqual(sum(c[0] == "restart" for c in calls), 2)

    def test_service_recovery_failure_is_not_reported_as_restored(self):
        self.paths.unit_path.parent.mkdir(parents=True)
        self.paths.unit_path.write_text("inert unit")
        def systemctl(*args, required=True):
            if args[0] == "restart" and required:
                raise AresLocalRuntimeError("initial handoff failed")
            return False
        with patch.object(self.runtime, "_gateway_state", return_value=(True, "enabled"), create=True), patch.object(self.runtime, "_systemctl", side_effect=systemctl):
            with self.assertRaisesRegex(AresLocalRuntimeError, "gateway recovery is unresolved"):
                self.runtime.rollback()
        self.assertEqual(self.pair(), ("b"*40, "a"*40))

    def post01_selection_snapshot(self):
        selections = [(str(path.readlink()), path.lstat().st_ino)
                      for path in (self.paths.current_link, self.paths.previous_link)]
        entries = []
        for revision in ("a"*40, "b"*40):
            root = self.runtime._release_dir(revision)
            for path in [root, *sorted(root.rglob("*"))]:
                stat = path.lstat()
                entries.append((str(path.relative_to(self.paths.data_root)),
                                stat.st_dev, stat.st_ino,
                                path.read_bytes() if path.is_file() else None))
        quarantine = self.paths.data_root/"quarantine"
        inventory = [(str(path.relative_to(self.paths.data_root)), path.lstat().st_ino)
                     for path in ([quarantine, *sorted(quarantine.rglob("*"))]
                                  if quarantine.exists() else [])]
        return selections, entries, inventory

    def post01_incomplete_previous(self, kind):
        source = self.release("a"*40)
        (source/"immutable-source.txt").write_text("previous source bytes")
        record = json.loads((source.parent/"release.json").read_text())
        if kind == "missing-binding":
            record.pop("runtime_binding")
        elif kind == "wrong-binding":
            record["runtime_binding"]["source"] = str(self.paths.staging_dir/"gone/source")
        elif kind == "older-binding":
            record["runtime_binding"]["controller_contract"] = 0
        elif kind == "missing-python":
            self.runtime._python_for(source).unlink()
        elif kind != "desktop-only":
            raise AssertionError(kind)
        self.runtime._atomic_json(source.parent/"release.json", record)
        return source

    def post01_forbid_materialization_effects(self, stack):
        calls = []
        for name in ("_run", "_build_runtime", "_refresh_moved_editable_install",
                     "_record_final_runtime_binding", "_seed_agent_home",
                     "_provision_context_governor_key", "_activate", "_write_config",
                     "_install_launcher", "_install_gateway_unit", "_handoff_gateway", "_systemctl"):
            def forbidden(*args, _name=name, **kwargs):
                calls.append(_name)
                raise AresLocalRuntimeError("forbidden effect: "+_name)
            stack.enter_context(patch.object(self.runtime, name, side_effect=forbidden))
        return calls

    def post01_previous_materialization_cases(self, *, public_setup):
        checkout = self.root/"source-checkout"
        checkout.mkdir(exist_ok=True)
        for kind in ("missing-binding", "wrong-binding", "older-binding", "missing-python", "desktop-only"):
            with self.subTest(entry="setup" if public_setup else "materialize", kind=kind):
                self.post01_incomplete_previous(kind)
                before = self.post01_selection_snapshot()
                with ExitStack() as stack:
                    calls = self.post01_forbid_materialization_effects(stack)
                    if public_setup:
                        def git_output(source, *args):
                            self.assertEqual(source, checkout.resolve())
                            answers = {
                                ("rev-parse", "--is-inside-work-tree"):"true",
                                ("rev-parse", "HEAD"):"a"*40,
                                ("remote", "get-url", "origin"):"inert-source",
                                ("symbolic-ref", "--quiet", "--short", "HEAD"):"main",
                            }
                            return answers[args]
                        stack.enter_context(patch.object(self.runtime, "_git_output", side_effect=git_output))
                    with self.assertRaises(AresLocalRuntimeError):
                        if public_setup:
                            self.runtime.setup(checkout, desktop=kind == "desktop-only",
                                               gateway=False, seed_from=self.root/"inert-seed")
                        else:
                            self.runtime._materialize("inert-source", "a"*40,
                                                      desktop=kind == "desktop-only")
                self.assertEqual(calls, [], "refusal must precede every acquisition/build/mutation seam")
                self.assertEqual(self.post01_selection_snapshot(), before)

    def test_post01_materialize_preserves_incomplete_previous_all_variants(self):
        self.post01_previous_materialization_cases(public_setup=False)

    def test_post01_setup_preserves_incomplete_previous_all_variants(self):
        self.post01_previous_materialization_cases(public_setup=True)

    def test_post01_editable_refresh_refuses_both_selected_identities(self):
        for revision in ("a"*40, "b"*40):
            with self.subTest(revision=revision):
                source = self.runtime._release_source(revision)
                before = self.post01_selection_snapshot()
                calls = []
                def forbidden(source):
                    calls.append(source)
                    raise AresLocalRuntimeError("forbidden editable synchronization")
                with patch.object(self.runtime, "_sync_python_runtime", side_effect=forbidden):
                    with self.assertRaises(AresLocalRuntimeError):
                        self.runtime._refresh_moved_editable_install(source)
                self.assertEqual(calls, [])
                self.assertEqual(self.post01_selection_snapshot(), before)

    def test_post01_binding_metadata_refuses_both_selected_identities(self):
        for revision in ("a"*40, "b"*40):
            with self.subTest(revision=revision):
                before = self.post01_selection_snapshot()
                with self.assertRaises(AresLocalRuntimeError):
                    self.runtime._record_final_runtime_binding(self.runtime._release_source(revision))
                self.assertEqual(self.post01_selection_snapshot(), before)

    def test_post01_unselected_incomplete_release_still_restages(self):
        source = self.release("c"*40, complete=False)
        (source/"old-source.txt").write_text("unselected original bytes")
        old_record = (source.parent/"release.json").read_bytes()
        old_source_inode = source.stat().st_ino
        before = self.post01_selection_snapshot()
        builds, bindings = [], []
        def run(command, **kwargs):
            if command[:2] == ["git", "clone"]:
                fresh = Path(command[-1])
                fresh.mkdir(parents=True)
                (fresh/"new-source.txt").write_text("inert replacement bytes")
            return subprocess.CompletedProcess(command, 0, "", "")
        def build(fresh, *, desktop):
            self.assertFalse(desktop)
            builds.append(fresh)
            python = self.runtime._python_for(fresh)
            python.parent.mkdir(parents=True)
            python.write_text("inert interpreter")
        with patch.object(self.runtime, "_run", side_effect=run), patch.object(self.runtime, "_build_runtime", side_effect=build), patch.object(self.runtime, "_sync_python_runtime", side_effect=lambda fresh: bindings.append(fresh)):
            self.runtime._materialize("inert-source", "c"*40, desktop=False)
        final = self.runtime._release_source("c"*40)
        self.runtime._require_complete_release(final, desktop=False)
        self.assertEqual(len(builds), 1)
        self.assertEqual(bindings, [final.resolve()])
        self.assertEqual((final/"new-source.txt").read_text(), "inert replacement bytes")
        self.assertFalse((final/"old-source.txt").exists())
        quarantines = list((self.paths.data_root/"quarantine/incomplete-releases").iterdir())
        self.assertEqual(len(quarantines), 1)
        self.assertEqual((quarantines[0]/"release.json").read_bytes(), old_record)
        self.assertEqual((quarantines[0]/"source").stat().st_ino, old_source_inode)
        self.assertEqual((quarantines[0]/"source/old-source.txt").read_text(), "unselected original bytes")
        self.assertEqual(self.post01_selection_snapshot()[:2], before[:2])

    def test_post01_complete_previous_reuse_remains_immutable(self):
        before = self.post01_selection_snapshot()
        with ExitStack() as stack:
            calls = self.post01_forbid_materialization_effects(stack)
            self.runtime._materialize("unused", "a"*40, desktop=False)
        self.assertEqual(calls, [])
        self.assertEqual(self.post01_selection_snapshot(), before)


if __name__ == "__main__":
    unittest.main()

