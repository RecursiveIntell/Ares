"""Tests for scripts/ci/classify_changes.py.

Check some common patterns of file modifications and the CI lanes they should run.
We should always fail open. We may run a lane we didn't need, never skip one a
change could have broken.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "classify_changes.py"
_spec = importlib.util.spec_from_file_location("classify_changes", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load classify_changes.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
classify = _mod.classify
ci_review_files = _mod.ci_review_files

DEFAULT = {
    "python": True,
    "python_prod": True,
    "frontend": True,
    "docker": True,
    "docker_meta": True,
    "nix": True,
    "site": True,
    "scan": True,
    "deps": True,
    "uv_lock": True,
    "npm_lock": True,
    "installer": True,
    "rust": True,
    "mcp_catalog": False,
    "ci_review": True,
    "context_continuity": True,
    "current_owner_integration": True,
}


def _lanes(python=False, frontend=False, site=False, scan=False, deps=False, uv_lock=False, npm_lock=False, installer=False, rust=False, mcp_catalog=False, docker_meta=False, ci_review=False, python_prod=None, nix=None, docker=None, context_continuity=False, current_owner_integration=False) -> dict[str, bool]:
    # python_prod tracks python except for tests-only diffs; default it to
    # python so the majority of cases don't need to spell it out.
    #
    # docker and nix are derived: both build the product, so both ride on
    # python_prod and frontend. The image ships the built web assets, and the
    # flake bundles the compiled ui-tui. Pass either explicitly to override.
    _python_prod = python if python_prod is None else python_prod
    _product = _python_prod or frontend
    return {
        "python": python,
        "python_prod": _python_prod,
        "docker": (docker_meta or _product) if docker is None else docker,
        "nix": _product if nix is None else nix,
        "frontend": frontend,
        "docker_meta": docker_meta,
        "site": site,
        "scan": scan,
        "deps": deps,
        "uv_lock": uv_lock,
        "npm_lock": npm_lock,
        "installer": installer,
        "rust": rust,
        "mcp_catalog": mcp_catalog,
        "ci_review": ci_review,
        "context_continuity": context_continuity,
        "current_owner_integration": current_owner_integration,
    }


CASES = {
    "docs-only → nothing heavy": (["README.md", "docs/guide.md"], _lanes()),
    "python source → python": (["run_agent.py"], _lanes(python=True, scan=True)),
    "dep manifest → python": (["pyproject.toml"], _lanes(python=True, scan=True, deps=True, uv_lock=True)),
    "uv.lock → python": (["uv.lock"], _lanes(python=True, uv_lock=True)),
    "ts package → frontend": (["apps/desktop/src/app.tsx"], _lanes(frontend=True)),
    "ui-tui → frontend": (["ui-tui/src/entry.ts"], _lanes(frontend=True)),
    # Lockfile bump shifts every TS package's tree, but not the Python suite.
    "root lockfile → frontend, not python": (["package-lock.json"], _lanes(frontend=True, npm_lock=True)),
    "nested lockfile → npm_lock": (["website/package-lock.json"], _lanes(site=True, npm_lock=True)),
    # A website file the Python suite cannot read stays site-only.
    "website config → site": (["website/docusaurus.config.ts"], _lanes(site=True)),
    # uv lock --check re-resolves against PyPI, so it must stay off for any
    # diff that can't desync the lockfile — a registry blip on a docs PR
    # otherwise shows up as a blocking "uv.lock out of sync" red X.
    "docs → no uv_lock": (
        ["website/docs/developer-guide/plugins/index.md"],
        _lanes(python=True, site=True),
    ),
    "frontend → no uv_lock": (["apps/desktop/src/store/profile.ts"], _lanes(frontend=True)),
    # The published CIMD document is asserted about by the Python suite, so a
    # lone edit there must not skip the lane that would catch a bad edit.
    "cimd document → python + site": (
        ["website/static/oauth/client-metadata.json"],
        _lanes(python=True, site=True),
    ),
    # A new docs page must reach llms.txt, and the generator that puts it there
    # has its own tests. Skipping Python on either is how the index drifted to
    # 53% coverage while every PR stayed green.
    "docs page → python + site": (
        ["website/docs/user-guide/bot-mode.md"],
        _lanes(python=True, site=True),
    ),
    "docs generator → python + site": (
        ["website/scripts/generate-llms-txt.py"],
        _lanes(python=True, scan=True, site=True),
    ),
    # SKILL.md reads like docs, but the skill-doc tests read skills/, so a
    # skill edit must still run Python.
    "skill md → python + site": (["skills/github/SKILL.md"], _lanes(python=True, site=True)),
    "dockerfile → docker meta": (["Dockerfile"], _lanes(docker_meta=True)),
    # Only the flake reads these, so they run nix alone. No Python test opens
    # them, unlike pyproject.toml and uv.lock below.
    "nix module → nix only": (["nix/homeManagerModules.nix"], _lanes(nix=True)),
    "flake.nix → nix only": (["flake.nix"], _lanes(nix=True)),
    "flake.lock → nix only": (["flake.lock"], _lanes(nix=True)),
    # A flake-only file must not mask a Python change beside it.
    "nix + python → both": (["nix/checks.nix", "agent/x.py"], _lanes(python=True, scan=True)),
    # Nine checks run the built binary, so product Python is a nix input even
    # when the diff touches no file under nix/.
    "product python → nix": (["hermes_cli/config.py"], _lanes(python=True, scan=True)),
    # tests/ is not packaged, so the built binary cannot change.
    "tests-only → no nix": (
        ["tests/agent/test_foo.py"],
        _lanes(python=True, python_prod=False, scan=True),
    ),
    # Prose cannot change the closure or the binary.
    "docs-only → no nix": (["README.md"], _lanes()),
    # install.ps1 is a shell script Python never imports, but it's also not
    # provably prose, so python stays on (fail-open) alongside the Windows lane.
    "install.ps1 → installer": (["scripts/install.ps1"], _lanes(python=True, installer=True)),
    "installer test → installer": (
        ["scripts/tests/test-install-ps1-longpath.ps1"],
        _lanes(python=True, installer=True),
    ),
    "python source alone → no installer lane": (["run_agent.py"], _lanes(python=True, scan=True)),
    # `.rs` lives under apps/, so it matches `frontend` too. That lane builds
    # TypeScript and cannot notice a Rust error — before `rust` existed it was
    # the ONLY lane a Rust change ran, and the crate's tests never executed.
    "rust source → rust": (
        ["apps/bootstrap-installer/src-tauri/src/powershell.rs"],
        _lanes(frontend=True, rust=True),
    ),
    "cargo lockfile → rust": (
        ["apps/bootstrap-installer/src-tauri/Cargo.lock"],
        _lanes(frontend=True, rust=True),
    ),
    # Non-.rs files in the crate still change what cargo builds.
    "tauri config → rust": (
        ["apps/bootstrap-installer/src-tauri/tauri.conf.json"],
        _lanes(frontend=True, rust=True),
    ),
    "ts source alone → no rust lane": (
        ["apps/bootstrap-installer/src/main.tsx"],
        _lanes(frontend=True),
    ),
    # Unknown top-level file keeps Python on rather than risk a silent skip.
    "unknown toplevel → python": (["Makefile"], _lanes(python=True)),
    "mixed docs+python → python": (["README.md", "agent/x.py"], _lanes(python=True, scan=True)),
    "mixed docs+frontend → frontend": (["README.md", "apps/x.tsx"], _lanes(frontend=True)),
    # tests-only diffs: pytest lanes stay ON, product jobs (Desktop E2E,
    # Docker) gate on python_prod and skip.
    "tests-only → python without python_prod": (
        ["tests/agent/test_foo.py", "tests/conftest.py"],
        _lanes(python=True, python_prod=False, scan=True),
    ),
    "tests + prod source → both lanes": (
        ["tests/agent/test_foo.py", "agent/x.py"],
        _lanes(python=True, scan=True),
    ),
    # Runner infrastructure is NOT tests-only — a bad runner edit can mask
    # real failures, so it keeps the conservative full lane set.
    "test runner script → python_prod stays on": (
        ["scripts/run_tests_parallel.py"],
        _lanes(python=True, scan=True, context_continuity=True, current_owner_integration=True),
    ),
    # Supply-chain lanes
    ".pth file → scan": (["evil.pth"], _lanes(python=True, scan=True)),
    "setup.py → scan": (["setup.py"], _lanes(python=True, scan=True)),
    "mcp catalog manifest → mcp_catalog": (
        ["optional-mcps/foo/manifest.yaml"],
        _lanes(python=True, mcp_catalog=True),
    ),
    "mcp_catalog.py → mcp_catalog": (
        ["hermes_cli/mcp_catalog.py"],
        _lanes(python=True, scan=True, mcp_catalog=True),
    ),
    # CI-sensitive files require explicit review label.
    "eslint config → ci_review": (
        ["apps/desktop/eslint.config.mjs"],
        _lanes(frontend=True, ci_review=True),
    ),
    "shared eslint config → ci_review": (
        ["eslint.config.shared.mjs"],
        _lanes(python=True, ci_review=True),
    ),
    "ui-tui eslint config → ci_review": (
        ["ui-tui/eslint.config.mjs"],
        _lanes(frontend=True, ci_review=True),
    ),
    "web eslint config → ci_review": (
        ["web/eslint.config.js"],
        _lanes(frontend=True, ci_review=True),
    ),
    "shared package eslint config → ci_review": (
        ["apps/shared/eslint.config.mjs"],
        _lanes(frontend=True, ci_review=True),
    ),
    "bootstrap-installer eslint config → ci_review": (
        ["apps/bootstrap-installer/eslint.config.mjs"],
        _lanes(frontend=True, ci_review=True),
    ),
    "prettier config → ci_review": (
        [".prettierrc"],
        _lanes(python=True, ci_review=True),
    ),
    "workflow yml → ci_review (also fail-open all)": (
        [".github/workflows/typecheck.yml"],
        DEFAULT,
    ),
    "composite action → ci_review (also fail-open all)": (
        [".github/actions/retry/action.yml"],
        DEFAULT,
    ),
    # Normal desktop source doesn't trigger ci_review.
    "desktop src → no ci_review": (
        ["apps/desktop/src/app.tsx"],
        _lanes(frontend=True),
    ),
    # Fail open: CI-config / empty / blank diffs run everything.
    ".github change → all": ([".github/workflows/tests.yml"], DEFAULT),
    "action change → all": ([".github/actions/detect-changes/action.yml"], DEFAULT),
    "empty diff → all": ([], DEFAULT),
    "blank lines → all": (["", "  "], DEFAULT),
}


@pytest.mark.parametrize("files,expected", CASES.values(), ids=CASES.keys())
def test_classify(files, expected):
    assert classify(files) == expected


_REPO = Path(__file__).resolve().parents[2]


def _yaml(rel: str) -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((_REPO / rel).read_text(encoding="utf-8"))


def test_every_lane_reaches_the_composite_action():
    """The action is the one surface every consumer reads, so it must carry all
    of them — ci.yaml, nix.yml and docker.yml each re-export a different subset.
    """
    lanes = set(classify(["run_agent.py"]))
    action_outputs = set(_yaml(".github/actions/detect-changes/action.yml")["outputs"])
    assert lanes - action_outputs == set(), "lane(s) missing from the composite action's outputs"


def test_ci_jobs_only_gate_on_detect_outputs_that_detect_actually_declares():
    """An ``if`` that reads an undeclared output resolves to the empty string.

    The lane then reports "skipping" on every PR, forever, and nothing goes red
    — there is no error for referencing an output a job never declared. That is
    exactly how the ``rust`` lane shipped dead: the classifier emitted it and
    the composite action re-exported it, but ci.yaml's ``detect`` job did not,
    so ``needs.detect.outputs.rust`` was never anything but "".
    """
    ci = _yaml(".github/workflows/ci.yaml")
    declared = set(ci["jobs"]["detect"]["outputs"])

    referenced: set[str] = set()
    for job in ci["jobs"].values():
        for expr in _iter_if_expressions(job):
            referenced.update(re.findall(r"needs\.detect\.outputs\.(\w+)", expr))

    assert referenced, "found no detect-gated jobs — the walk is broken, not the wiring"
    assert referenced - declared == set(), "job(s) gate on an output detect never declares"


def _iter_if_expressions(job: object):
    """Yield every ``if:`` string in a job, including inside its steps."""
    if not isinstance(job, dict):
        return
    if isinstance(cond := job.get("if"), str):
        yield cond
    for step in job.get("steps", []) or []:
        if isinstance(step, dict) and isinstance(cond := step.get("if"), str):
            yield cond


def test_ci_review_files_returns_only_sensitive_paths_sorted_and_unique():
    assert ci_review_files([
        "apps/desktop/src/app.tsx",
        ".github/workflows/ci.yml",
        "apps/desktop/eslint.config.mjs",
        ".github/workflows/ci.yml",
    ]) == [
        ".github/workflows/ci.yml",
        "apps/desktop/eslint.config.mjs",
    ]


@pytest.mark.parametrize("path,continuity,owner", [
    ("ares_runtime/continuity/runtime.py", True, False),
    ("tests/ares_runtime/test_continuity_rollover.py", True, False),
    ("tests/ares_runtime/test_context_native_paired.py", True, False),
    ("plugins/context_engine/_context_governor/__init__.py", True, False),
    ("gateway/run.py", True, False),
    ("tui_gateway/server.py", True, False),
    ("docs/context-continuity/native-external-owner.json", True, False),
    ("docs/context-continuity/native-external-owner.patch", True, False),
    ("ares_runtime/collaboration.py", True, True),
    ("tests/test_ares_collaboration.py", True, True),
    ("ares_runtime/governed_context.py", False, True),
    ("ares_runtime/__init__.py", False, True),
    ("tests/owner_integration/profile_runtime_fixture.rs", False, True),
    ("tests/owner_integration/semantic_memory_revocation_owner.rs", False, True),
    ("tests/ares_runtime/fixtures/profile_runtime_v2_owner.json", False, True),
    ("tests/ares_runtime/test_memory_witness_v2.py", False, True),
    ("tests/ares_runtime/test_policy_basis_v2.py", False, True),
    ("README.md", False, False),
    ("apps/desktop/src/app.tsx", False, False),
])
def test_native_qualification_path_coverage(path, continuity, owner):
    values = classify([path])
    assert values["context_continuity"] is continuity
    assert values["current_owner_integration"] is owner


@pytest.mark.parametrize("paths", [
    [],
    ["", "  "],
    [".github/workflows/ci.yaml"],
    [".github/actions/detect-changes/action.yml"],
    ["scripts/ci/classify_changes.py"],
    ["scripts/ci/evaluate_required_checks.py"],
    ["tests/ci/test_required_checks.py"],
    ["scripts/run_tests.sh"],
    ["scripts/run_tests_parallel.py"],
])
def test_uncertain_diff_and_qualification_infrastructure_select_both_native_owners(paths):
    values = classify(paths)
    assert values["context_continuity"]
    assert values["current_owner_integration"]


def test_mixed_paths_select_each_owner_without_mutating_paths():
    paths = ["README.md", "ares_runtime/continuity/runtime.py", "tests/owner_integration/fixture.rs"]
    original = list(paths)
    values = classify(paths)
    assert values["context_continuity"] and values["current_owner_integration"]
    assert paths == original


@pytest.mark.linux_only
@pytest.mark.parametrize("scenario", ["rename", "cap300", "belowcap299", "failure", "push", "workflow_dispatch"])
def test_composite_action_preserves_native_applicability_with_fake_compare(tmp_path, scenario):
    # Execute the actual composite bash configuration, with an inert compare
    # provider. Real jq applies the requested projection to our JSON fixture.
    import json
    import yaml

    jq = shutil.which("jq")
    assert jq is not None, "jq is required for the compare-projection contract"
    action = yaml.load(
        (_REPO / ".github/actions/detect-changes/action.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fixture = tmp_path / "compare.json"
    call_log = tmp_path / "calls"
    output = tmp_path / "outputs"
    files = [{"filename": "README.md"}]
    if scenario == "rename":
        files[0]["previous_filename"] = "ares_runtime/collaboration.py"
    elif scenario in {"cap300", "belowcap299"}:
        count = 300 if scenario == "cap300" else 299
        files = [{"filename": f"docs/file-{number}.md"} for number in range(count)]
    fixture.write_text(json.dumps({"files": files}), encoding="utf-8")
    gh = bin_dir / "gh"
    gh.write_text(
        '#!/bin/bash\n'
        'printf "call\\n" >> "$FAKE_CALL_LOG"\n'
        'if [ "$FAKE_SCENARIO" = failure ]; then exit 1; fi\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = --jq ]; then shift; exec "$FAKE_JQ" -r "$1" "$FAKE_COMPARE"; fi\n'
        '  shift\n'
        'done\n'
        'exit 2\n',
        encoding="utf-8",
    )
    gh.chmod(0o700)
    sleep = bin_dir / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    sleep.chmod(0o700)
    python = bin_dir / "python3"
    python.symlink_to(sys.executable)
    event = scenario if scenario in {"push", "workflow_dispatch"} else "pull_request"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "EVENT_NAME": event,
        "REPO": "inert/fixture",
        "BASE_SHA": "base",
        "HEAD_SHA": "head",
        "GH_TOKEN": "inert",
        "GITHUB_OUTPUT": str(output),
        "FAKE_COMPARE": str(fixture),
        "FAKE_CALL_LOG": str(call_log),
        "FAKE_SCENARIO": scenario,
        "FAKE_JQ": jq,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        ["/bin/bash", "-c", action["runs"]["steps"][0]["run"]],
        cwd=_REPO, env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    emitted = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    expected = "false" if scenario == "belowcap299" else "true"
    assert emitted["context_continuity"] == expected
    assert emitted["current_owner_integration"] == expected
    calls = call_log.read_text(encoding="utf-8").splitlines() if call_log.exists() else []
    assert len(calls) == (0 if event != "pull_request" else 3 if scenario == "failure" else 1)
