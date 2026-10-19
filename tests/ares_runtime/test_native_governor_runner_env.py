"""The native executable locator survives the real clean runner; secrets do not."""
import os
from pathlib import Path
import subprocess
import pytest


@pytest.mark.parametrize('empty', [False, True])
def test_canonical_runner_preserves_native_binary_locator_without_credentials(tmp_path,empty):
    root=Path(__file__).resolve().parents[2]
    expected='' if empty else str(tmp_path/'native binary input (not executed)')
    probe=tmp_path/'test_environment_probe.py'
    probe.write_text('import os\n\ndef test_owned_environment():\n'
        +f'    assert os.environ.get("RA_DAEMON_BIN") == {expected!r}\n'
        +'    assert "OPENAI_API_KEY" not in os.environ\n'
        +'    assert "ANTHROPIC_API_KEY" not in os.environ\n')
    env=dict(os.environ,RA_DAEMON_BIN=expected,OPENAI_API_KEY='non-secret-probe-only',ANTHROPIC_API_KEY='non-secret-probe-only')
    result=subprocess.run([str(root/'scripts/run_tests.sh'),'--paths',str(probe),
        '--file-retries','0','--file-timeout','30','-j','1','--','-q','--tb=short'],
        cwd=root,env=env,capture_output=True,text=True,timeout=90)
    assert result.returncode==0,result.stdout+result.stderr
    assert '1 tests passed, 0 failed' in result.stdout,result.stdout+result.stderr


def test_unset_native_locator_skips_only_the_optional_integration(monkeypatch):
    from tests.ares_runtime.test_native_governor_integration import _native_inputs
    monkeypatch.delenv('RA_DAEMON_BIN',raising=False)
    with pytest.raises(pytest.skip.Exception,match='explicit qualified artifact'):
        _native_inputs()


@pytest.mark.parametrize('token',['','/no-such-native-artifact-gmc'])
def test_explicit_invalid_native_locator_is_not_silently_skipped(monkeypatch,token):
    from tests.ares_runtime.test_native_governor_integration import _native_inputs
    monkeypatch.setenv('RA_DAEMON_BIN',token)
    with pytest.raises(AssertionError,match='not executable'):
        _native_inputs()
