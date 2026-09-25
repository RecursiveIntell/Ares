"""Wheel and psutil-absent import boundaries for run checkpoint support."""
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tomllib
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def isolated(code, source, cwd, *, without_psutil=False):
    # Keep declared dependencies available, but do not execute site .pth files
    # that could silently import an editable source checkout instead of the wheel.
    prefix = "import sys; sys.path.insert(0, sys.argv[1]); sys.path.append(sys.argv[2]);\n"
    if without_psutil:
        prefix += (
            "import importlib.abc\n"
            "class MissingPsutil(importlib.abc.MetaPathFinder):\n"
            " def find_spec(self, fullname, path=None, target=None):\n"
            "  if fullname == 'psutil' or fullname.startswith('psutil.'):\n"
            "   raise ModuleNotFoundError('psutil intentionally unavailable')\n"
            "sys.meta_path.insert(0, MissingPsutil())\n"
        )
    return subprocess.run(
        [sys.executable, "-I", "-S", "-c", prefix + code, str(source), sysconfig.get_path("purelib")],
        cwd=cwd, capture_output=True, text=True, timeout=30,
    )


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory):
    work = tmp_path_factory.mktemp("checkpoint-wheel")
    source = work / "source"
    source.mkdir()
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        if (ROOT / name).is_file():
            shutil.copy2(ROOT / name, source / name)
    # Preserve the real package discovery configuration and all its package roots.
    packages = config["tool"]["setuptools"]["packages"]["find"]["include"]
    for package in sorted({p.split(".")[0] for p in packages}):
        if (ROOT / package).is_dir():
            shutil.copytree(ROOT / package, source / package,
                            ignore=shutil.ignore_patterns("__pycache__", "node_modules", ".venv"))
    modules = set(config["tool"]["setuptools"]["py-modules"])
    # Include the actual source inputs even before the packaging allowlist is repaired.
    modules.update(("scripts.run_checkpoint_claim", "scripts.run_checkpoint_resume"))
    for module in modules:
        relative = Path(*module.split(".")).with_suffix(".py")
        (source / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, source / relative)
    wheels = work / "wheels"
    wheels.mkdir()
    build = subprocess.run(
        [sys.executable, "-c", "import setuptools.build_meta as b; b.build_wheel(__import__('sys').argv[1])", str(wheels)],
        cwd=source, capture_output=True, text=True, timeout=120,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    files = list(wheels.glob("*.whl"))
    assert len(files) == 1
    installed = work / "installed"
    with zipfile.ZipFile(files[0]) as wheel:
        names = set(wheel.namelist())
        wheel.extractall(installed)
    return work, installed, names


def test_wheel_contains_only_required_checkpoint_scripts_modules(built_wheel):
    _, _, names = built_wheel
    assert {p for p in names if p.startswith("scripts/")} == {
        "scripts/run_checkpoint_claim.py",
        "scripts/run_checkpoint_context.py",
        "scripts/run_checkpoint_resume.py",
    }


def test_required_rpc_client_imports_from_extracted_wheel(built_wheel):
    work, installed, _ = built_wheel
    result = isolated(
        "from pathlib import Path; import hermes_state_runs as owner; "
        "from hermes_state import SessionDB; import scripts.run_checkpoint_claim as c; "
        "import scripts.run_checkpoint_resume as r; "
        "assert Path(owner.__file__).is_relative_to(Path(sys.argv[1])); "
        "assert issubclass(SessionDB, owner.SessionRunCustodyMixin); "
        "assert Path(c.__file__).is_relative_to(Path(sys.argv[1])); "
        "assert Path(r.__file__).is_relative_to(Path(sys.argv[1])); "
        "assert callable(c.claim_from_files); assert callable(r.verify_checkpoint_files); print('wheel-import-ok')",
        installed, work,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "wheel-import-ok"


def test_state_scaffold_import_and_basic_store_without_psutil(tmp_path):
    result = isolated(
        "from pathlib import Path; import hermes_state; "
        "assert hermes_state.psutil is None; "
        "d=hermes_state.SessionDB(db_path=Path('state.db')); "
        "d.set_meta('scaffold', 'usable'); assert d.get_meta('scaffold') == 'usable'; d.close(); print('scaffold-ok')",
        ROOT, tmp_path, without_psutil=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "scaffold-ok"


@pytest.mark.parametrize("function", ["_process_identity", "_controller"])
def test_custody_process_inspection_refuses_without_psutil(tmp_path, function):
    code = (
        "import os; import hermes_state_runs as r\n"
        "try:\n"
        f" r.{function}(os.getpid())\n"
        "except r.RunCustodyError as e:\n"
        " assert e.code == 'PROCESS_INSPECTION_UNAVAILABLE'; print(e.code)\n"
        "else:\n"
        " raise AssertionError('missing dependency must not authorize or invent process identity')\n"
    )
    result = isolated(code, ROOT, tmp_path, without_psutil=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "PROCESS_INSPECTION_UNAVAILABLE"
