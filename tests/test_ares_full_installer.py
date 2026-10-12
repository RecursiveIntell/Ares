"""Disposable, offline behavior tests for the canonical full installer.

Run directly with unittest or through pytest. Real Bash, files, symlinks and Git
are exercised; runtime/build/provider boundaries are fixtures, not qualification
of a packaged or installed Ares. No network or system package manager is invoked.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

INSTALLER = Path(os.environ.get("ARES_INSTALLER_UNDER_TEST", Path(__file__).resolve().parents[1] / "install.sh"))
SOURCE = INSTALLER.read_text(encoding="utf-8")
PROGRAMS = re.findall(r"<<'PY'\n([\s\S]*?)\nPY", SOURCE)


def program(marker):
    matches = [text for text in PROGRAMS if marker in text]
    if len(matches) != 1:
        raise AssertionError(f"Expected one production program for {marker!r}")
    return matches[0]


def section(start, end):
    return SOURCE.split(start, 1)[1].split(end, 1)[0]


def environment(home, **extra):
    env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(home), "LC_ALL": "C.UTF-8"}
    env.update({key: str(value) for key, value in extra.items()})
    return env


def shell(code, env, *args):
    return subprocess.run(["bash", "-c", code, "--", *args], env=env, text=True,
                          capture_output=True, timeout=15, check=False)


def executable(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n" + content, encoding="utf-8")
    path.chmod(0o755)


@unittest.skipUnless(shutil.which("bash") and os.name == "posix", "POSIX Bash installer")
class InstallerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ares-installer-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env = environment(self.root)

    def cli(self, *args, env=None):
        return subprocess.run(["bash", str(INSTALLER), *args], env=env or self.env,
                              text=True, capture_output=True, timeout=15, check=False)

    def preamble(self):
        return SOURCE.split("help() {", 1)[0]

    def test_help_plan_no_writes(self):
        for args in [("--help",), ("--plan",), ("--plan", "--minimal", "--no-desktop", "--no-gateway", "--skip-setup")]:
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(list(self.root.iterdir()), [])

    def test_option_is_not_a_value(self):
        for option in ["--home", "--bin-dir", "--branch", "--with-recursive-agent-source"]:
            with self.subTest(option=option):
                result = self.cli(option, "-h", "--plan")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("needs a value", result.stderr)

    def test_unsafe_paths_refuse_before_writes(self):
        for value in ["/", str(self.root), str(self.root / "bad\npath"), str(self.root / "bad:path")]:
            with self.subTest(value=value):
                result = self.cli("--plan", "--home", value)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(list(self.root.iterdir()), [])

    def test_spaces_unicode_supported(self):
        result = self.cli("--plan", "--home", str(self.root / "données with spaces"), "--bin-dir", str(self.root / "launchers with spaces"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("données with spaces", result.stdout)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_sourcing_leaves_caller_alive(self):
        result = shell('source "$1" --plan; status=$?; printf "CALLER_ALIVE:%s\\n" "$status"', self.env, str(INSTALLER))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CALLER_ALIVE:2", result.stdout)

    def test_explicit_error_recovers(self):
        code = self.preamble() + '\nCONFIG_PENDING=true\nrestore_configuration() { printf restored > "$HOME/restored"; }\ndie "fixture rejection"\n'
        result = shell(code, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / "restored").exists(), result.stderr)

    def test_nested_function_failure_recovers(self):
        code = self.preamble() + '\nCONFIG_PENDING=true\nrestore_configuration() { printf restored > "$HOME/restored"; }\nfail_inside() { false; printf false_success; }\nfail_inside\n'
        result = shell(code, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / "restored").exists(), result.stderr)
        self.assertNotIn("false_success", result.stdout)

    def test_substitution_recovers_once(self):
        code = self.preamble() + '\nCONFIG_PENDING=true\nrestore_configuration() { printf R >> "$HOME/restored"; }\nvalue="$(false)"\n'
        result = shell(code, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "restored").read_text(), "R")

    def test_owned_cargo_precedence(self):
        own, ambient = self.root / "owned", self.root / ".cargo"
        executable(own / "bin/cargo", "printf owned")
        executable(ambient / "bin/cargo", "printf ambient")
        line = next(line for line in SOURCE.splitlines() if line.startswith("export PATH=") and "$ENHANCEMENTS/bin" in line)
        env = environment(self.root, CARGO_HOME=own, UV_INSTALL_DIR=self.root / "uv", ARES_BIN_DIR=self.root / "launchers", ENHANCEMENTS=self.root / "enhancements")
        result = shell('export PATH="$CARGO_HOME/bin:$PATH"\n' + line + '\ncommand cargo', env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "owned")

    def recursive(self):
        src = self.root / "recursive-source"
        executable(src / "scripts/install-hermes-plugin.sh", "exit 42\n")
        env = environment(self.root, TEST_SOURCE=src, TEST_HOME=self.root / "ares", TEST_PYTHON=sys.executable)
        code = self.preamble() + '\nRECURSIVE_AGENT_SOURCE="$TEST_SOURCE"\nARES_HOME="$TEST_HOME"\nPYTHON="$TEST_PYTHON"\n' + section("STEP=recursive-agent\n", "STEP=provider\n")
        return code, env

    def test_recursive_payload_failure_fatal(self):
        code, env = self.recursive()
        (self.root / "ares").mkdir()
        self.assertNotEqual(shell(code, env).returncode, 0)

    def test_existing_incomplete_recursive_payload_preserved(self):
        code, env = self.recursive()
        payload = self.root / "ares/plugins/recursive-agent-native"
        payload.mkdir(parents=True)
        (payload / "operator.txt").write_text("preserve")
        self.assertNotEqual(shell(code, env).returncode, 0)
        self.assertEqual((payload / "operator.txt").read_text(), "preserve")

    def test_recursive_payload_symlink_preserved(self):
        code, env = self.recursive()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "sentinel").write_text("preserve")
        payload = self.root / "ares/plugins/recursive-agent-native"
        payload.parent.mkdir(parents=True)
        payload.symlink_to(outside, target_is_directory=True)
        self.assertNotEqual(shell(code, env).returncode, 0)
        self.assertEqual((outside / "sentinel").read_text(), "preserve")

    def test_skip_setup_does_not_swallow_gateway_failure(self):
        bins = self.root / "bin"
        executable(bins / "systemctl", "exit 0")
        executable(bins / "python", "exit 42")
        code = self.preamble() + f'\nSETUP=false\nOS=Linux\nRUNTIME_PYTHON="{bins}/python"\nARES_SOURCE="$HOME/source"\n' + section("STEP=gateway\n", "STEP=verification\n")
        result = shell(code, environment(self.root, PATH=f"{bins}:{self.env['PATH']}"))
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_embedded_python_compiles(self):
        for index, text in enumerate(PROGRAMS):
            with self.subTest(index=index):
                compile(text, f"install.sh:program{index}", "exec")

    def configuration(self, initial=None, save_result=None):
        home = self.root / "ares"
        home.mkdir(exist_ok=True)
        (home / "config.yaml").write_text("original-config")
        (home / ".env").write_text("TOKEN=private-fixture-sentinel\n")
        source = self.root / "source"
        skill = source / "optional-skills/example"
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text("new skill")
        modules = {name: types.ModuleType(name) for name in ["hermes_cli", "hermes_cli.config"]}
        config = modules["hermes_cli.config"]
        config.load_config = lambda: copy.deepcopy(initial or {})
        def save(value):
            if save_result is not False:
                (home / "config.yaml").write_text(json.dumps(value))
            return save_result
        config.save_config = save
        env = environment(self.root, ARES_HOME=home, ARES_INSTALL_SOURCE=source, ARES_INSTALL_EXTRAS="true", ARES_INSTALL_BACKUP=home / "backup")
        return home, modules, env

    def test_refused_config_save_stops_writes(self):
        home, modules, env = self.configuration(save_result=False)
        with patch.dict(sys.modules, modules), patch.dict(os.environ, env, clear=True), self.assertRaises(SystemExit):
            exec(program("from hermes_cli.config import"), {})
        self.assertEqual((home / ".env").read_text(), "TOKEN=private-fixture-sentinel\n")

    def test_custom_stack_plugin_preserved(self):
        home, modules, env = self.configuration()
        custom = home / "plugins/ares-full-stack"
        custom.mkdir(parents=True)
        (custom / "__init__.py").write_text("operator-owned-code")
        with patch.dict(sys.modules, modules), patch.dict(os.environ, env, clear=True), self.assertRaises(SystemExit):
            exec(program("from hermes_cli.config import"), {})
        self.assertEqual((custom / "__init__.py").read_text(), "operator-owned-code")
        self.assertEqual((home / "config.yaml").read_text(), "original-config")

    def test_symlinked_config_is_not_overwritten(self):
        home, modules, env = self.configuration()
        outside = self.root / "outside-config"
        outside.write_text("external-config")
        (home / "config.yaml").unlink()
        (home / "config.yaml").symlink_to(outside)
        with patch.dict(sys.modules, modules), patch.dict(os.environ, env, clear=True), self.assertRaises(SystemExit):
            exec(program("from hermes_cli.config import"), {})
        self.assertEqual(outside.read_text(), "external-config")

    def test_recovery_restores_all_managed_paths(self):
        home, modules, env = self.configuration()
        with patch.dict(sys.modules, modules), patch.dict(os.environ, env, clear=True):
            exec(program("from hermes_cli.config import"), {})
            self.assertTrue((home / "skills/ares/example/SKILL.md").exists())
            with patch.object(sys, "argv", ["restore", env["ARES_INSTALL_BACKUP"]]):
                exec(program("manifest = backup/'files.json'"), {})
        self.assertEqual((home / "config.yaml").read_text(), "original-config")
        self.assertEqual((home / ".env").read_text(), "TOKEN=private-fixture-sentinel\n")
        for name in ["skills/ares", "skills/.ares-install-manifest.json", "plugins/ares-full-stack"]:
            self.assertFalse((home / name).exists())

    def test_recovery_refuses_changed_runtime(self):
        home, modules, env = self.configuration()
        with patch.dict(sys.modules, modules), patch.dict(os.environ, env, clear=True):
            exec(program("from hermes_cli.config import"), {})
            updated = (home / "config.yaml").read_bytes()
            current = home / "runtime/current"
            current.parent.mkdir(exist_ok=True)
            target = home / "new-runtime"
            target.mkdir()
            current.symlink_to(target, target_is_directory=True)
            with patch.object(sys, "argv", ["restore", env["ARES_INSTALL_BACKUP"]]), self.assertRaises(SystemExit):
                exec(program("manifest = backup/'files.json'"), {})
        self.assertEqual((home / "config.yaml").read_bytes(), updated)
        self.assertTrue((home / "backup/files.json").is_file())

    def test_existing_complete_recursive_payload_preserved(self):
        code, env = self.recursive()
        payload = self.root / "ares/plugins/recursive-agent-native"
        payload.mkdir(parents=True)
        names = ["__init__.py", "client.py", "schema.py", "plugin.yaml", "pyproject.toml"]
        for name in names:
            (payload / name).write_text("operator content")
        result = shell(code, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all((payload / name).read_text() == "operator content" for name in names))

    def test_partial_payload_does_not_become_installed(self):
        code, env = self.recursive()
        script = Path(env["TEST_SOURCE"]) / "scripts/install-hermes-plugin.sh"
        executable(script, 'mkdir -p "$HERMES_HOME/plugins/recursive-agent-native"; printf partial > "$HERMES_HOME/plugins/recursive-agent-native/__init__.py"; exit 42')
        (self.root / "ares").mkdir()
        self.assertNotEqual(shell(code, env).returncode, 0)
        self.assertFalse((self.root / "ares/plugins/recursive-agent-native").exists())

    def test_complete_staged_payload_publishes_manifest(self):
        code, env = self.recursive()
        script = Path(env["TEST_SOURCE"]) / "scripts/install-hermes-plugin.sh"
        executable(script, 'mkdir -p "$HERMES_HOME/plugins/recursive-agent-native"; for name in __init__.py client.py schema.py plugin.yaml pyproject.toml; do printf content > "$HERMES_HOME/plugins/recursive-agent-native/$name"; done')
        (self.root / "ares").mkdir()
        result = shell(code, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        base = self.root / "ares/plugins"
        expected = sorted(["__init__.py", "client.py", "schema.py", "plugin.yaml", "pyproject.toml"])
        self.assertEqual((base / "recursive-agent-native.manifest").read_text().splitlines(), expected)
        self.assertEqual(sorted(path.name for path in (base / "recursive-agent-native").iterdir()), expected)

    def test_occupied_lock_is_not_stolen(self):
        home = self.root / "ares"
        lock = home / ".full-install.lock"
        lock.mkdir(parents=True)
        (lock / "pid").write_text("existing-owner")
        result = self.cli("--home", str(home), "--skip-setup")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Another full installer", result.stderr)
        self.assertEqual((lock / "pid").read_text(), "existing-owner")
        self.assertEqual(sorted(path.name for path in home.iterdir()), [".full-install.lock"])

    def test_owned_lock_cleans_up(self):
        code = self.preamble() + '\nARES_HOME="$HOME/ares"\n' + section("STEP=preflight\n", "STEP=prerequisites\n")
        result = shell(code, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "ares/.full-install.lock").exists())

    def test_wrong_uv_binary_refused(self):
        bins = self.root / "tools"
        executable(bins / "curl", 'printf "#!/bin/sh\\nexit 0\\n" > "${@: -1}"')
        executable(bins / "uv", 'printf "uv 0.13.0\\n"')
        code = self.preamble() + '\nTEMP_DIR="$HOME/temp"; mkdir "$TEMP_DIR"; UV_INSTALL_DIR="$HOME/tools"\n' + section("STEP=toolchains\n", 'export CARGO_BUILD_JOBS=')
        result = shell(code, environment(self.root, PATH=f"{bins}:{self.env['PATH']}"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("version does not match", result.stderr)

    def test_published_git_advance_and_local_ahead_refusal(self):
        real_git = shutil.which("git")
        if not real_git:
            self.skipTest("git executable unavailable")
        upstream, checkout = self.root / "upstream", self.root / "checkout"
        def git(*args):
            result = subprocess.run([real_git, *map(str, args)], env=self.env, text=True, capture_output=True, timeout=15, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()
        git("init", "-b", "main", upstream)
        git("-C", upstream, "config", "user.email", "fixture@example.invalid")
        git("-C", upstream, "config", "user.name", "Fixture")
        (upstream / "tracked").write_text("initial")
        git("-C", upstream, "add", ".")
        git("-C", upstream, "commit", "-m", "initial")
        git("clone", upstream, checkout)
        git("-C", checkout, "config", "user.email", "fixture@example.invalid")
        git("-C", checkout, "config", "user.name", "Fixture")
        bins = self.root / "transport"
        executable(bins / "git", 'if [[ "$3" == remote && "$4" == get-url ]]; then printf "https://github.com/RecursiveIntell/Ares.git\\n"; exit; fi\nexec "$TEST_GIT" "$@"')
        env = environment(self.root, PATH=f"{bins}:{self.env['PATH']}", TEST_GIT=real_git, CHECKOUT=checkout)
        code = self.preamble() + "\ncheckout() {" + section("checkout() {", "\n\nSTEP=source") + '\ncheckout Ares "$CHECKOUT" main\n'
        (upstream / "tracked").write_text("remote advance")
        git("-C", upstream, "commit", "-am", "remote")
        result = shell(code, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git("-C", checkout, "rev-parse", "HEAD"), git("-C", upstream, "rev-parse", "HEAD"))
        (checkout / "local").write_text("keep local commit")
        git("-C", checkout, "add", ".")
        git("-C", checkout, "commit", "-m", "local")
        before = git("-C", checkout, "rev-parse", "HEAD")
        self.assertNotEqual(shell(code, env).returncode, 0)
        self.assertEqual(git("-C", checkout, "rev-parse", "HEAD"), before)
        self.assertEqual((checkout / "local").read_text(), "keep local commit")

    def test_doctor_failure_and_source_mismatch_preserve_latest(self):
        home = self.root / "ares"
        records = home / "install-receipts"
        records.mkdir(parents=True)
        previous = b'{"phase":"complete","previous":"preserved"}\n'
        (records / "latest.json").write_bytes(previous)
        attempt = records / "attempt.json"
        revision = "a" * 40
        selected = home / "runtime/releases" / revision / "source"
        selected.mkdir(parents=True)
        current = home / "runtime/current"
        current.symlink_to(selected, target_is_directory=True)
        attempt.write_text(json.dumps({"phase": "built_pending_activation", "ares_revision": revision}))
        log = home / "doctor.log"
        labels = ["active runtime", "stable Python", "selected release tree", "Ares runtime imports", "SQLite runtime", "Context Governor strict probe", "MCP readiness", "runtime process coherence", "Ares gateway"]
        log.write_text("\n".join(("FAIL" if name == "MCP readiness" else "PASS") + " " + name + ": fixture" for name in labels))
        env = environment(self.root, ARES_HOME=home, ARES_BIN_DIR=home / "bin", ARES_INSTALL_RECEIPT=attempt, ARES_INSTALL_DOCTOR_LOG=log, ARES_INSTALL_DOCTOR_STATUS="1", ARES_INSTALL_GATEWAY_STARTED="true", ARES_INSTALL_MODIFY_PATH="false", ARES_INSTALL_SETUP="false", ARES_INSTALL_RECURSIVE_AGENT_STATUS="disabled")
        with patch.dict(os.environ, env, clear=True), self.assertRaises(SystemExit):
            exec(program("Required Ares runtime health checks"), {})
        self.assertEqual((records / "latest.json").read_bytes(), previous)
        log.write_text(log.read_text().replace("FAIL MCP readiness", "PASS MCP readiness"))
        env["ARES_INSTALL_DOCTOR_STATUS"] = "0"
        other = home / "other-runtime"
        other.mkdir()
        current.unlink()
        current.symlink_to(other, target_is_directory=True)
        with patch.dict(os.environ, env, clear=True), self.assertRaises(SystemExit):
            exec(program("Required Ares runtime health checks"), {})
        self.assertEqual((records / "latest.json").read_bytes(), previous)
        current.unlink()
        current.symlink_to(selected, target_is_directory=True)
        with patch.dict(os.environ, env, clear=True):
            exec(program("Required Ares runtime health checks"), {})
        self.assertEqual(attempt.read_bytes(), (records / "latest.json").read_bytes())
        self.assertEqual(json.loads(attempt.read_text())["phase"], "complete")

    def test_uv_sanitizer_keeps_internal_pin(self):
        sanitizer = 'log "Using installer-owned uv settings;' + section('log "Using installer-owned uv settings;', '# Provision owned')
        code = self.preamble() + "\n" + sanitizer + '\nprintf "%s" "${ARES_UV_VERSION:-missing}"\n[[ -z "${UV_INSECURE_HOST:-}" && -z "${UV_PROJECT:-}" ]]\n'
        result = shell(code, environment(self.root, UV_INSECURE_HOST="untrusted.invalid", UV_PROJECT="/other-project"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"\d+\.\d+\.\d+$")


    def test_gateway_identity_failure_is_not_hidden_by_export(self):
        broken = self.root / "broken-python"
        executable(broken, "exit 42")
        start = re.search(r"(?:export )?ARES_GATEWAY_UNIT_PATH=", SOURCE).start()
        chunk = SOURCE[start:SOURCE.index("STEP=managed-runtime", start)]
        code = self.preamble() + '\nBOOTSTRAP_PYTHON="$TEST_PYTHON"\n' + chunk + '\nprintf FALSE_SUCCESS\n'
        result = shell(code, environment(self.root, TEST_PYTHON=broken))
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("FALSE_SUCCESS", result.stdout)

    def test_physical_path_alias_does_not_bypass_separator_check(self):
        target = self.root / "bad:path"
        target.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(target, target_is_directory=True)
        result = self.cli("--plan", "--home", str(alias))
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(list(target.iterdir()), [])

    def test_safe_physical_alias_remains_supported(self):
        target = self.root / "safe data"
        target.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(target, target_is_directory=True)
        result = self.cli("--plan", "--home", str(alias))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(target), result.stdout)
        self.assertEqual(list(target.iterdir()), [])

    def test_gateway_identity_success_still_exports_value(self):
        fake = self.root / "identity-python"
        executable(fake, 'cat >/dev/null; printf "/fixture/gateway.service\\n"')
        start = re.search(r"(?:export )?ARES_GATEWAY_UNIT_PATH=", SOURCE).start()
        chunk = SOURCE[start:SOURCE.index("STEP=managed-runtime", start)]
        code = self.preamble() + '\nBOOTSTRAP_PYTHON="$TEST_PYTHON"\n' + chunk + "\nbash -c 'printf \"%s\" \"$ARES_GATEWAY_UNIT_PATH\"'\n"
        result = shell(code, environment(self.root, TEST_PYTHON=fake))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "/fixture/gateway.service")


if __name__ == "__main__":
    unittest.main()
