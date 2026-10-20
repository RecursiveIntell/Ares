"""Execute emitted desktop commands without leaving the current Python env."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from hermes_cli import linux_desktop_entry as owner

pytestmark = pytest.mark.linux_only


@pytest.mark.parametrize('entry_kind', ['env_shebang', 'module_fallback'])
def test_desktop_command_preserves_invoked_python_environment(tmp_path, monkeypatch, entry_kind):
    payload = 'import json,sys; print(json.dumps({"prefix":sys.prefix,"args":sys.argv[1:]}))\n'
    if entry_kind == 'env_shebang':
        script = tmp_path / 'hermes'
        script.write_text('#!/usr/bin/env python3\n' + payload)
        script.chmod(0o755)
        monkeypatch.setattr('hermes_cli.relaunch.resolve_hermes_bin', lambda: str(script))
    else:
        # This owned inert module prevents launching the actual desktop app.
        package = tmp_path / 'hermes_cli'
        package.mkdir()
        (package / '__init__.py').write_text('')
        (package / 'main.py').write_text(payload)
        monkeypatch.setattr('hermes_cli.relaunch.resolve_hermes_bin', lambda: None)
    command = owner.resolve_exec_command()
    completed = subprocess.run(shlex.split(command), cwd=tmp_path,
        env={'PATH': os.defpath, 'HOME': str(tmp_path), 'LANG': 'C.UTF-8', 'TZ': 'UTC'},
        text=True, capture_output=True, timeout=10, check=True)
    result = json.loads(completed.stdout)
    assert result['prefix'] == sys.prefix, 'desktop command escaped its admitted Python environment'
    assert result['args'] == ['desktop']
    assert shlex.split(command)[0] == str(Path(sys.executable).absolute())
