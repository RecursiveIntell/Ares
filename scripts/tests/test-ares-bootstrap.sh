#!/usr/bin/env bash
# Exercise the real bootstrap control flow without network, installs, or services.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python3 - "$ROOT" <<'PY'
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

root = Path(sys.argv[1])
real_python = sys.executable

def executable(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)

cases = [
    ("managed-default", False, False, False),
    ("managed-options", False, True, False),
    ("system-options", True, True, False),
    ("managed-install-failure", False, True, True),
    ("system-install-failure", True, True, True),
]
for label, system, opt_out, fail in cases:
    with tempfile.TemporaryDirectory(prefix="ares-bootstrap-") as temporary:
        home = Path(temporary)
        checkout = home / "source checkout"
        (checkout / ".git").mkdir(parents=True)
        package = checkout / "ares_runtime"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "local_runtime.py").write_text(
            "import json, os, pathlib, sys\n"
            "pathlib.Path(os.environ['PROBE_RECEIPT']).write_text(json.dumps({"
            "'argv': sys.argv[1:], 'home': os.environ.get('ARES_HOME'), "
            "'bin': os.environ.get('ARES_BIN_DIR')}), encoding='utf-8')\n",
            encoding="utf-8",
        )
        bin_dir = home / "bin"
        bin_dir.mkdir()
        python_wrapper = (
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            'if [[ "${1:-}" == "-m" && "${2:-}" == "pip" ]]; then\n'
            '  [[ "${PROBE_FAIL_INSTALL:-0}" != "1" ]] || exit 23\n'
            '  printf "%s\\n" "$*" > "$PROBE_INSTALL_LOG"\n  exit 0\nfi\n'
            'export PYTHONPATH="$PROBE_CHECKOUT"\n'
            f'exec {shlex.quote(real_python)} "$@"\n'
        )
        executable(bin_dir / "python3", python_wrapper)
        executable(bin_dir / "git", "#!/usr/bin/env bash\nexit 0\n")
        executable(bin_dir / "uv", """#!/usr/bin/env bash
set -euo pipefail
[[ "${PROBE_FAIL_INSTALL:-0}" != "1" ]] || exit 23
[[ "$PWD" == "$PROBE_CHECKOUT" ]]
printf '%s\n' "$*" > "$PROBE_INSTALL_LOG"
mkdir -p .venv/bin
cp "$PROBE_PYTHON_WRAPPER" .venv/bin/python
# Deliberately no .venv/bin/ares: the package does not declare that script.
""")
        receipt = home / "setup.json"
        install_log = home / "install.log"
        data = home / "isolated data"
        launcher = home / "launchers"
        environment = {
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "HOME": str(home),
            "PROBE_CHECKOUT": str(checkout),
            "PROBE_RECEIPT": str(receipt),
            "PROBE_INSTALL_LOG": str(install_log),
            "PROBE_PYTHON_WRAPPER": str(bin_dir / "python3"),
            "PROBE_FAIL_INSTALL": "1" if fail else "0",
        }
        argv = ["bash", str(root / "install.sh"), "--dir", str(checkout),
                "--hermes-home", str(data), "--ares-bin-dir", str(launcher)]
        if system:
            argv.append("--no-venv")
        if opt_out:
            argv.extend(["--no-desktop", "--no-gateway"])
        result = subprocess.run(argv, cwd=home, env=environment, text=True,
                                capture_output=True, timeout=15)
        if fail:
            assert result.returncode != 0, (label, result.stdout, result.stderr)
            assert not receipt.exists(), f"{label}: setup ran after failed install"
        else:
            assert result.returncode == 0, (label, result.stdout, result.stderr)
            observed = json.loads(receipt.read_text(encoding="utf-8"))
            expected_args = ["setup", "--source", str(checkout)]
            if opt_out:
                expected_args += ["--no-desktop", "--no-gateway"]
            assert observed == {"argv": expected_args, "home": str(data), "bin": str(launcher)}, observed
            install_args = install_log.read_text(encoding="utf-8").strip()
            assert install_args == (f"-m pip install -e {checkout}[all]" if system else "sync --locked --extra all"), install_args
            assert not (checkout / ".venv/bin/ares").exists()
        print(f"PASS {label}")
PY
