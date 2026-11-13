"""Standalone bounded-runner tests; explicit optional integration checkout."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

import falsify


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def process(self, source, timeout=2, cap=4096):
        return falsify.bounded_process([sys.executable, "-I", "-c", source], self.root, timeout, cap=cap)

    def test_success(self):
        result = self.process("print('ok')")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["stdout"], b"ok\n")

    def test_failure_is_not_success(self):
        self.assertEqual(self.process("raise SystemExit(7)")["state"], "failed")

    def test_timeout(self):
        self.assertEqual(self.process("import time; time.sleep(20)", 0.1)["state"], "timeout")

    def test_output_is_bounded_during_capture(self):
        result = self.process("import os; os.write(1,b'x'*200000)", cap=1000)
        self.assertEqual(result["state"], "output_limit")
        self.assertEqual(len(result["stdout"]), 1000)

    def test_pipe_held_by_descendant_does_not_hang(self):
        source = "import os,time; p=os.fork(); time.sleep(20) if p==0 else None"
        result = self.process(source, 0.2)
        self.assertEqual(result["state"], "timeout")
        self.assertLess(result["elapsed_ms"], 3000)

    def test_secret_environment_not_inherited(self):
        os.environ["FALSIFY_TEST_SECRET"] = "sensitive"
        try:
            result = self.process("import os; print(os.environ.get('FALSIFY_TEST_SECRET','absent'))")
            self.assertEqual(result["stdout"], b"absent\n")
        finally:
            os.environ.pop("FALSIFY_TEST_SECRET", None)

    def test_path_escape_rejected(self):
        with self.assertRaises(ValueError):
            falsify.inside(self.root, Path(__file__))

    def test_symlink_and_oversized_input_rejected(self):
        path = self.root/"data"
        path.write_bytes(b"x"*20)
        link = self.root/"link"
        link.symlink_to(path)
        with self.assertRaises(ValueError):
            falsify.read_file(link)
        with self.assertRaises(ValueError):
            falsify.read_file(path, 10)

    def test_nonfinite_timeout_rejected(self):
        with self.assertRaises(ValueError):
            self.process("pass", float("nan"))

    def test_bad_backend_missing_files_rejected(self):
        with self.assertRaises(OSError):
            falsify.backend_identity(self.root)

    def test_skill_description_meets_dispatch_limit(self):
        skill = Path(__file__).parent.parent / "SKILL.md"
        frontmatter = skill.read_text(encoding="utf-8").split("---", 2)[1]
        description = next(line.removeprefix("description: ") for line in frontmatter.splitlines()
                           if line.startswith("description: "))
        self.assertLessEqual(len(description), 60)
        self.assertTrue(description.endswith("."))


@unittest.skipUnless(os.environ.get("FALSIFY_TEST_CLAIMLEDGER_ROOT"), "explicit ClaimLedger checkout not selected")
class RealIntegration(unittest.TestCase):
    def test_real_checker_replay_and_identity_failure(self):
        root = Path(os.environ["FALSIFY_TEST_CLAIMLEDGER_ROOT"]).resolve()
        spec = importlib.util.spec_from_file_location("science_test_fixtures", root/"tests/test_science.py")
        fixtures = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(root))
        try:
            spec.loader.exec_module(fixtures)
            with tempfile.TemporaryDirectory() as directory:
                work = Path(directory)
                for name, obj in (("statement", fixtures.declared()), ("problem", fixtures.problem()), ("candidate", fixtures.candidate())):
                    (work/f"{name}.json").write_text(json.dumps(obj))
                args = argparse.Namespace(mode="verify", workspace=work, claimledger_root=root,
                    checker_sha256=falsify.backend_identity(root)["sha256"], python=Path(sys.executable),
                    timeout=10, budget=100, out=work/"result", kernel=None, kernel_sha256=None,
                    statement=work/"statement.json", problem=work/"problem.json", candidate=work/"candidate.json")
                result = falsify.run(args)
                self.assertEqual(result["status"], "checked", result)
                self.assertEqual(result["evidence_verdict"], "finite_problem_infeasible")
                self.assertEqual(result["support_admission"], "not_performed")
                self.assertTrue(result["steps"])
                self.assertTrue(all(step["argv"] and all(type(arg) is str for arg in step["argv"])
                                    for step in result["steps"]))
                with self.assertRaises(FileExistsError):
                    falsify.run(args)
                args.out = work/"mismatch"
                args.checker_sha256 = "0"*64
                with self.assertRaises(ValueError):
                    falsify.run(args)
                self.assertFalse(args.out.exists())
        finally:
            sys.path.remove(str(root))


if __name__ == "__main__":
    unittest.main()
