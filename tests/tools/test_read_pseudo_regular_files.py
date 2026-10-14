"""Linux pseudo-regular files have contents despite stat size zero."""
from pathlib import Path
import pytest
from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations

@pytest.mark.linux_only
def test_proc_regular_file_keeps_supported_read_contract(tmp_path):
    source=Path('/proc/version')
    text=source.read_text(encoding='utf-8')
    assert source.stat().st_size==0 and text
    env=LocalEnvironment(cwd=str(tmp_path),timeout=20)
    try:
        result=ShellFileOperations(env).read_file(str(source),offset=1,limit=10)
        assert result.error is None
        assert result.file_size==0
        assert result.total_lines==text.count('\n')
        assert result.content.startswith('1|'+text.rstrip('\n'))
    finally:
        env.cleanup()
