"""The entry-point minor-line guard agrees with declared package support.

All probes execute on the admitted interpreter. Synthetic version metadata is
not evidence of running Python 3.11--3.13 or of legacy interpreter behaviour.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tomllib

from packaging.specifiers import SpecifierSet
import pytest

from hermes_bootstrap import UnsupportedPythonRuntimeError, require_supported_python

ROOT = Path(__file__).resolve().parents[1]
REQUIRES = SpecifierSet(tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["requires-python"])


@pytest.mark.parametrize("version", [(3, minor, 0) for minor in range(10, 17)] + [(4, 0, 0)])
def test_runtime_guard_matches_requires_python_for_stable_minor_lines(version):
    admitted = REQUIRES.contains(".".join(map(str, version)))
    before = dict(os.environ)
    if admitted:
        assert require_supported_python(version) is None
    else:
        with pytest.raises(UnsupportedPythonRuntimeError, match="HERMES_PYTHON_UNSUPPORTED"):
            require_supported_python(version)
    assert dict(os.environ) == before


@pytest.mark.parametrize("version", [(), (3,), "3.14", (3, True), (3, 14.0), (None, 14)])
def test_runtime_guard_rejects_unqualified_version_metadata(version):
    with pytest.raises(UnsupportedPythonRuntimeError, match="unqualified version metadata"):
        require_supported_python(version)


def test_real_interpreter_is_admitted():
    assert REQUIRES.contains(".".join(map(str, sys.version_info[:3])))
    assert require_supported_python() is None


def test_import_rejects_synthetic_unsupported_metadata_before_bootstrap_effects():
    unsupported = next((3, minor, 0) for minor in range(10, 17)
                       if not REQUIRES.contains(f"3.{minor}.0"))
    program = f"""
import os, sys
actual = tuple(sys.version_info[:3])
assert actual == {tuple(sys.version_info[:3])!r}, actual
assert 'hermes_bootstrap' not in sys.modules
before = dict(os.environ)
# Inject data for the admission boundary, not a different interpreter.
sys.version_info = {unsupported!r} + ('final', 0)
try:
    import hermes_bootstrap
except RuntimeError as error:
    assert 'HERMES_PYTHON_UNSUPPORTED' in str(error), repr(error)
    assert dict(os.environ) == before
    print('rejected-before-bootstrap')
else:
    raise AssertionError('unsupported metadata reached entry-point bootstrap')
"""
    result = subprocess.run([sys.executable, "-c", program], cwd=ROOT,
                            capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "rejected-before-bootstrap"
