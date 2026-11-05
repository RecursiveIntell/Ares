"""Exercise the alert script through the real no-agent cron execution owner."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import sysconfig
import time
import venv

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture(scope='session')
def candidate_python(tmp_path_factory):
    """Private 3.14 venv binds the candidate without editing the live venv."""
    root = Path(__file__).resolve().parents[2]
    env_dir = tmp_path_factory.mktemp('health-alert-venv')
    venv.EnvBuilder(with_pip=False).create(env_dir)
    python = env_dir / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    site_dir = env_dir / ('Lib/site-packages' if sys.platform == 'win32' else
                          f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages')
    runtime_site = sysconfig.get_path('purelib')
    (site_dir / 'candidate-test.pth').write_text(
        f'import sys, site; sys.path.insert(0, {str(root)!r}); site.addsitedir({runtime_site!r})\n'
    )
    probe = subprocess.run(
        [str(python), '-c', 'import hermes_cli.delegation_health as h; print(h.__file__)'],
        cwd=env_dir, env={'HOME': str(env_dir), 'PATH': os.defpath},
        text=True, capture_output=True, check=True,
    )
    assert Path(probe.stdout.strip()).resolve() == root / 'hermes_cli/delegation_health.py'
    return python


@pytest.fixture
def alert_home(tmp_path, monkeypatch, candidate_python):
    home = tmp_path / 'profile'
    scripts = home / 'scripts'
    scripts.mkdir(parents=True)
    (home / 'config.yaml').write_text('{}\n')
    root = Path(__file__).resolve().parents[2]
    shutil.copyfile(root / 'cron/scripts/delegation_health_alert.py', scripts / 'delegation_health_alert.py')
    monkeypatch.setenv('HERMES_HOME', str(home))
    # The scheduler deliberately strips Hermes-owned PYTHONPATH. Bind its
    # Python executable to an isolated venv which imports the candidate.
    monkeypatch.setattr(sys, 'executable', str(candidate_python))
    token = set_hermes_home_override(home)
    try:
        yield home
    finally:
        reset_hermes_home_override(token)


def _setup_receipt(home, statuses):
    with sqlite3.connect(home / 'state.db') as db:
        db.execute('CREATE TABLE async_delegations (state TEXT, result_json TEXT, task_json TEXT, dispatched_at REAL)')
        if statuses is not None:
            workers = [dict(task_index=i, status=status, **({'exit_reason': 'completed'} if status == 'completed' else {}))
                       for i, status in enumerate(statuses)]
            receipt = {'results': workers, 'private': 'PRIVATE_CANARY'}
            intent = {'is_batch': True, 'goals': ['PRIVATE_GOAL'] * len(workers)}
            db.execute('INSERT INTO async_delegations VALUES (?, ?, ?, ?)',
                       ('completed', json.dumps(receipt), json.dumps(intent), time.time()))


@pytest.mark.parametrize(('statuses', 'expected'), [
    (['completed'], 'silent'),
    (['completed', 'timeout'], 'below 80%'),
    (['completed', 'running'], 'indeterminate'),
    (None, 'indeterminate'),
])
def test_no_agent_cron_consumes_alert_output(alert_home, statuses, expected):
    from cron.scheduler import SILENT_MARKER, run_job

    _setup_receipt(alert_home, statuses)
    before = (alert_home / 'state.db').read_bytes()
    ok, doc, message, error = run_job({
        'id': 'health-fixture', 'name': 'synthetic health fixture',
        'no_agent': True, 'script': 'delegation_health_alert.py',
    })
    assert ok is True and error is None, (doc, message, error)
    if expected == 'silent':
        assert message == SILENT_MARKER
    else:
        assert expected in message
    assert 'PRIVATE_CANARY' not in doc + message
    assert 'PRIVATE_GOAL' not in doc + message
    assert before == (alert_home / 'state.db').read_bytes()


def test_no_agent_cron_surfaces_future_dated_receipt_as_indeterminate(alert_home):
    from cron.scheduler import run_job

    _setup_receipt(alert_home, ['completed'])
    with sqlite3.connect(alert_home / 'state.db') as db:
        db.execute('UPDATE async_delegations SET dispatched_at = ?', (time.time() + 86400,))
    before = (alert_home / 'state.db').read_bytes()
    ok, doc, message, error = run_job({
        'id': 'health-fixture', 'name': 'synthetic health fixture',
        'no_agent': True, 'script': 'delegation_health_alert.py',
    })
    assert ok is True and error is None
    assert 'indeterminate' in message
    assert 'PRIVATE_CANARY' not in doc + message
    assert before == (alert_home / 'state.db').read_bytes()


def test_no_agent_cron_surfaces_invalid_timestamp_as_indeterminate(alert_home):
    from cron.scheduler import run_job

    _setup_receipt(alert_home, ['completed'])
    with sqlite3.connect(alert_home / 'state.db') as db:
        row = db.execute('SELECT state, result_json, task_json FROM async_delegations').fetchone()
        db.execute('INSERT INTO async_delegations VALUES (?, ?, ?, ?)', (*row, float('-inf')))
    ok, doc, message, error = run_job({
        'id': 'health-fixture', 'name': 'synthetic health fixture',
        'no_agent': True, 'script': 'delegation_health_alert.py',
    })
    assert ok is True and error is None
    assert 'indeterminate' in message
    assert 'PRIVATE_CANARY' not in doc + message
    assert 'PRIVATE_GOAL' not in doc + message


def test_no_agent_cron_reports_missing_database_as_failure(alert_home):
    from cron.scheduler import run_job

    ok, doc, message, error = run_job({
        'id': 'health-fixture', 'name': 'synthetic health fixture',
        'no_agent': True, 'script': 'delegation_health_alert.py',
    })
    assert ok is False
    assert 'script failed' in message
    assert 'Delegation health unavailable' in error
    assert not (alert_home / 'state.db').exists()
