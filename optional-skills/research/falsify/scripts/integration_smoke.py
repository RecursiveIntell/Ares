#!/usr/bin/env python3
"""Exercise the real compiled kernel -> ClaimLedger -> Ares pipeline on seed cases."""
import argparse
import json
from pathlib import Path
import sys

import falsify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claimledger-root", type=Path, required=True)
    parser.add_argument("--libraries-root", type=Path, required=True)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    corpus_path = args.libraries_root / "receipt-bench/fixtures/falsify/cases.json"
    raw = falsify.read_file(corpus_path)
    corpus = json.loads(raw)
    args.out.mkdir(mode=0o700, parents=False, exist_ok=False)
    checker = falsify.backend_identity(args.claimledger_root)["sha256"]
    kernel = falsify.executable_identity(args.kernel.absolute())
    outcomes = []
    verdicts = {"primal": "finite_problem_feasible", "dual": "finite_problem_infeasible", "unresolved": "unresolved"}
    for case in corpus["cases"]:
        name = case["id"]
        falsify.require(name.replace("-", "").isalnum() and name.isascii(), "invalid fixture name")
        directory = args.out / name
        directory.mkdir(mode=0o700)
        for item in ("statement", "problem"):
            (directory / f"{item}.json").write_text(json.dumps(case[item]), encoding="utf-8")
        run_args = argparse.Namespace(mode="solve", workspace=directory, claimledger_root=args.claimledger_root,
            checker_sha256=checker, python=args.python, timeout=20, budget=case["budget"],
            out=directory / "run", kernel=args.kernel, kernel_sha256=kernel,
            statement=directory / "statement.json", problem=directory / "problem.json")
        report = falsify.run(run_args)
        falsify.require(report["status"] == "checked", f"case failed: {name}")
        falsify.require(report["evidence_verdict"] == verdicts[case["expected_solver_kind"]], "unexpected verdict")
        outcomes.append({"id": name, "verdict": report["evidence_verdict"], "evidence_sha256": report["evidence_sha256"]})
    result = {"schema": "FalsifyIntegrationSmokeV1", "corpus_sha256": falsify.digest(raw),
              "checker_sha256": checker, "kernel_sha256": kernel, "cases": outcomes,
              "scope": "synthetic_finite_matrices", "support_admission": "not_performed"}
    (args.out / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
