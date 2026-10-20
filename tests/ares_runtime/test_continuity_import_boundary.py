"""Optional native key machinery must not break ordinary Windows SessionDB."""
import subprocess
import sys


def test_sessiondb_import_without_posix_key_dependencies():
    result = subprocess.run([sys.executable, "-c", '''
import importlib.abc, sys
class NoFcntl(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "fcntl":
            raise ModuleNotFoundError("No fcntl on Windows")
sys.meta_path.insert(0, NoFcntl())
from hermes_state import SessionDB
from agent.tool_executor import execute_tool_calls_concurrent
from ares_runtime import ContractError
assert ContractError("example").code == "example"
assert "plugins.context_engine._context_governor.key_state" not in sys.modules
'''], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
