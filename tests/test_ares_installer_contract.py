"""Run the canonical installer's hermetic contract battery in the per-file lane."""

from pathlib import Path
import subprocess

import pytest


@pytest.mark.linux_only
def test_installer_contract_battery() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["bash", str(root / "scripts/tests/test-ares-installer-contract.sh")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_managed_runtime_embedded_python_imports_sys() -> None:
    """Require sys in the actual toolchain identity builder, not merely in other heredocs."""
    import ast

    source = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
    marker = 'STEP=managed-runtime\n"$BOOTSTRAP_PYTHON" - <<\'PY\'\n'
    assert marker in source
    embedded = source.split(marker, 1)[1].split("\nPY\n", 1)[0]
    ast.parse(embedded)
    assert "sys.version" in embedded
    imports = [node for node in ast.walk(ast.parse(embedded)) if isinstance(node, ast.Import)]
    assert any(alias.name == "sys" for node in imports for alias in node.names)
