"""The executable -m entrypoint must use the canonical compute-host owner.

No host loop, provider, service or live home: intercept the canonical main in a
fresh process. --help makes the old duplicate-namespace path exit instead of
blocking, so the absence of the marker is a deterministic RED witness.
"""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def test_module_entrypoint_calls_canonical_main_not_duplicate_main_namespace(tmp_path):
    probe = r'''
import runpy, sys
sys.path.insert(0, sys.argv[1])
import tui_gateway.compute_host as owner
from tui_gateway.compute_host import _HostTransport

def witnessed_main():
    transport = owner._HostTransport(lambda event: True)
    assert isinstance(transport, _HostTransport)
    print('CANONICAL_ENTRYPOINT_OWNER')
    return 0

owner.main = witnessed_main
sys.argv = ['compute_host', '--help']
try:
    runpy.run_module('tui_gateway.compute_host', run_name='__main__', alter_sys=True)
except SystemExit as exc:
    assert exc.code == 0
'''
    result = subprocess.run([sys.executable, '-P', '-c', probe, str(ROOT)],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
        env={'HOME': str(tmp_path), 'HERMES_HOME': str(tmp_path / 'home'),
             'PATH': '/usr/bin:/bin', 'PYTHONHASHSEED': '0'})
    assert result.returncode == 0, result.stderr[-1000:]
    assert result.stdout.strip() == 'CANONICAL_ENTRYPOINT_OWNER', result.stdout
