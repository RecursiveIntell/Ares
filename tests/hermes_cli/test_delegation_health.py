import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from hermes_cli.delegation_health import summarize


def batch_row(state, workers, *, add_completion_reason=True):
    indexed = []
    for i, worker in enumerate(workers):
        entry = {**worker, 'task_index': i}
        if add_completion_reason and entry.get('status') == 'completed':
            entry.setdefault('exit_reason', 'completed')
        indexed.append(entry)
    dispatch = {'is_batch': True, 'goals': ['fixture'] * len(indexed)}
    return state, json.dumps({'results': indexed}), json.dumps(dispatch)


class HealthTests(unittest.TestCase):
    def test_truncated_and_invalid_schema_are_not_completed_work(self):
        workers = [
            {'status': 'completed', 'exit_reason': 'completed', 'truncated': False},
            {'status': 'completed', 'exit_reason': 'max_iterations', 'truncated': True},
            {'status': 'completed', 'exit_reason': 'completed', 'schema_valid': False},
        ]
        summary = summarize([batch_row('completed', workers)])
        self.assertEqual(summary['terminal_workers'], 3)
        self.assertEqual(summary['completed_workers'], 1)
        self.assertEqual(summary['completion_rate'], 1 / 3)
        self.assertEqual(summary['workers_truncated'], 1)
        self.assertEqual(summary['workers_schema_invalid'], 1)
        self.assertTrue(summary['warning_below_80_percent'])

    def test_unproven_or_malformed_completion_is_indeterminate(self):
        for worker in (
            {'status': 'completed'},
            {'status': 'completed', 'exit_reason': 'unrecognized'},
            {'status': 'completed', 'exit_reason': 'completed', 'truncated': 'false'},
            {'status': 'completed', 'exit_reason': 'completed', 'schema_valid': 'true'},
        ):
            with self.subTest(worker=worker):
                summary = summarize([batch_row('completed', [worker], add_completion_reason=False)])
                self.assertEqual(summary['unverified_completed_workers'], 1)
                self.assertEqual(summary['completed_workers'], 0)
                self.assertIsNone(summary['warning_below_80_percent'])
                self.assertEqual(summary['warning_state'], 'indeterminate_incomplete_data')

    def test_interrupted_is_terminal_but_never_success(self):
        summary = summarize([batch_row('completed', [
            {'status': 'completed', 'exit_reason': 'completed'},
            {'status': 'interrupted', 'exit_reason': 'interrupted'},
        ])])
        self.assertEqual(summary['terminal_workers'], 2)
        self.assertEqual(summary['completion_rate'], .5)
        self.assertTrue(summary['warning_below_80_percent'])

    def test_threshold_boundary_and_overlapping_failure_flags(self):
        clean = {'status': 'completed', 'exit_reason': 'completed'}
        bad = {'status': 'completed', 'exit_reason': 'max_iterations',
               'truncated': True, 'schema_valid': False}
        summary = summarize([batch_row('completed', [clean, clean, clean, clean, bad])])
        self.assertEqual(summary['terminal_workers'], 5)
        self.assertEqual(summary['completed_workers'], 4)
        self.assertEqual(summary['workers_truncated'], 1)
        self.assertEqual(summary['workers_schema_invalid'], 1)
        self.assertEqual(summary['completion_rate'], .8)
        self.assertFalse(summary['warning_below_80_percent'])

    def test_conflicting_completion_fields_are_indeterminate(self):
        for worker in (
            {'status': 'completed', 'exit_reason': 'completed', 'truncated': True},
            {'status': 'completed', 'exit_reason': 'max_iterations', 'truncated': False},
        ):
            with self.subTest(worker=worker):
                summary = summarize([batch_row('completed', [worker])])
                self.assertEqual(summary['unverified_completed_workers'], 1)
                self.assertEqual(summary['workers_truncated'], 1)
                self.assertIsNone(summary['warning_below_80_percent'])

    def test_batch_count_and_indices_must_match_dispatch_intent(self):
        task = json.dumps({'is_batch': True, 'goals': ['one', 'two']})
        for results in ([{'task_index': 0, 'status': 'completed'}],
                        [{'task_index': 0, 'status': 'completed'},
                         {'task_index': 0, 'status': 'completed'}]):
            with self.subTest(results=results):
                summary = summarize([('completed', json.dumps({'results': results}), task)])
                self.assertIsNone(summary['warning_below_80_percent'])
                self.assertEqual(summary['warning_state'], 'indeterminate_incomplete_data')
                self.assertEqual(summary['invalid_worker_sets'], 1)

    def test_nonterminal_batch_and_missing_dispatch_evidence_are_indeterminate(self):
        result = json.dumps({'results': [{'task_index': 0, 'status': 'completed'}]})
        task = json.dumps({'is_batch': True, 'goals': ['one']})
        for state, task_data in [('finalizing', task), ('unknown', task), ('completed', None)]:
            with self.subTest(state=state, task_data=task_data):
                summary = summarize([(state, result, task_data)])
                self.assertIsNone(summary['warning_below_80_percent'])
                self.assertEqual(summary['warning_state'], 'indeterminate_incomplete_data')

    def test_single_worker_receipt_is_counted(self):
        task = json.dumps({'goal': 'one'})
        result = json.dumps({'status': 'completed', 'exit_reason': 'completed', 'duration_seconds': 3})
        summary = summarize([('completed', result, task)])
        self.assertEqual(summary['terminal_workers'], 1)
        self.assertEqual(summary['completed_workers'], 1)
        self.assertFalse(summary['warning_below_80_percent'])

    def test_worker_denominator_not_batch_and_unknown_is_visible(self):
        data = {'results': [{'status':'completed', 'duration_seconds':2}, {'status':'timeout', 'timeout_phase':'after_llm_calls', 'duration_seconds':10}, {'status':'running'}, {}]}
        task = json.dumps({'is_batch': True, 'goals': ['fixture']})
        s = summarize([batch_row('error', data['results']), ('running', None, task), ('error', 'not json', task)])
        self.assertEqual(s['terminal_workers'], 2)
        self.assertEqual(s['completion_rate'], .5)
        self.assertIsNone(s['warning_below_80_percent'])
        self.assertEqual(s['warning_state'], 'indeterminate_incomplete_data')
        self.assertEqual(s['invalid_or_missing_receipts'], 2)
        self.assertEqual(s['workers_missing_status'], 1)
        self.assertEqual(s['timeout_phases_not_causes'], {'after_llm_calls': 1})
        self.assertEqual(s['duration_seconds_nearest_rank_p90'], 10)

    def test_complete_and_unknown_status_decisions(self):
        s = summarize([batch_row('completed', [{'status':'completed'}, {'status':'timeout'}])])
        self.assertTrue(s['warning_below_80_percent'])
        self.assertEqual(s['warning_state'], 'below_threshold')
        healthy = summarize([batch_row('completed', [{'status':'completed'}])])
        self.assertFalse(healthy['warning_below_80_percent'])
        self.assertEqual(healthy['warning_state'], 'at_or_above_threshold')
        unknown = summarize([batch_row('completed', [{'status':'completed'}, {'status':'unknown'}])])
        self.assertIsNone(unknown['warning_below_80_percent'])
        self.assertEqual(unknown['warning_state'], 'indeterminate_incomplete_data')

    def test_untrusted_fields_are_not_echoed_and_invalid_phase_is_safe(self):
        summary = summarize([batch_row('completed', [
            {'status': 'PRIVATE_CANARY'},
            {'status': 'timeout', 'timeout_phase': ['PRIVATE_CANARY']},
        ])])
        self.assertNotIn('PRIVATE_CANARY', json.dumps(summary))
        self.assertEqual(summary['timeout_phases_not_causes'], {'unknown': 1})
        self.assertIsNone(summary['warning_below_80_percent'])
        secret_task = json.dumps({'is_batch': True, 'goals': ['PRIVATE_GOAL']})
        safe_result = json.dumps({'results': [{'task_index': 0, 'status': 'completed'}]})
        self.assertNotIn('PRIVATE_GOAL', json.dumps(summarize([('completed', safe_result, secret_task)])))

    def test_no_denominator_and_invalid_values(self):
        s = summarize([batch_row('completed', [{'status':'running','duration_seconds':-1}])])
        self.assertIsNone(s['completion_rate'])
        self.assertIsNone(s['warning_below_80_percent'])
        self.assertIsNone(s['duration_seconds_nearest_rank_p50'])

    def test_monitoring_cli_uses_profile_db_without_exposing_summary(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            path = home / 'state.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE async_delegations (state TEXT, result_json TEXT, task_json TEXT, dispatched_at REAL)')
                state, result, task = batch_row('completed', [{'status': 'completed', 'summary': 'PRIVATE_CANARY'}])
                db.execute('INSERT INTO async_delegations VALUES (?, ?, ?, ?)', (state, result, task, time.time()))
            env = {'HOME': str(home), 'HERMES_HOME': str(home),
                   'PATH': os.defpath, 'LANG': 'C.UTF-8',
                   'PYTHONPATH': str(Path(__file__).resolve().parents[2])}
            process = subprocess.run([sys.executable, '-m', 'hermes_cli.main', 'monitoring', 'delegation'],
                                     env=env, capture_output=True, text=True, check=True)
            result = json.loads(process.stdout)
            self.assertEqual(result['completed_workers'], 1)
            self.assertNotIn('PRIVATE_CANARY', process.stdout)
            self.assertEqual(result['source'], str(path))

    def test_alert_only_reports_threshold_and_indeterminate_without_private_text(self):
        cases = [
            ([{'status': 'completed'}], ''),
            ([{'status': 'completed'}] * 4 + [{'status': 'timeout'}], ''),
            ([{'status': 'completed'}, {'status': 'timeout'}], 'below 80%'),
            ([{'status': 'completed'}, {'status': 'running'}], 'indeterminate'),
        ]
        for workers, expected in cases:
            with self.subTest(workers=workers), tempfile.TemporaryDirectory() as td:
                home = Path(td)
                path = home / 'state.db'
                with sqlite3.connect(path) as db:
                    db.execute('CREATE TABLE async_delegations (state TEXT, result_json TEXT, task_json TEXT, dispatched_at REAL)')
                    state, result, task = batch_row('completed', workers)
                    result = result.replace('"results":', '"private": "PRIVATE_CANARY", "results":')
                    db.execute('INSERT INTO async_delegations VALUES (?, ?, ?, ?)', (state, result, task, time.time()))
                before = path.read_bytes()
                env = {'HOME': str(home), 'HERMES_HOME': str(home),
                       'PATH': os.defpath, 'LANG': 'C.UTF-8',
                       'PYTHONPATH': str(Path(__file__).resolve().parents[2])}
                proc = subprocess.run([sys.executable, '-m', 'hermes_cli.main', 'monitoring',
                                       'delegation', '--alert-only'],
                                      env=env, capture_output=True, text=True, check=True)
                if expected:
                    self.assertIn(expected, proc.stdout)
                else:
                    self.assertEqual(proc.stdout, '')
                self.assertNotIn('PRIVATE_CANARY', proc.stdout + proc.stderr)
                self.assertEqual(before, path.read_bytes())

    def test_alert_only_missing_and_empty_database_do_not_appear_healthy(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            env = {'HOME': str(home), 'HERMES_HOME': str(home),
                   'PATH': os.defpath, 'LANG': 'C.UTF-8',
                   'PYTHONPATH': str(Path(__file__).resolve().parents[2])}
            command = [sys.executable, '-m', 'hermes_cli.main', 'monitoring',
                       'delegation', '--alert-only']
            missing = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn('unavailable', missing.stderr)
            self.assertFalse((home / 'state.db').exists())
            with sqlite3.connect(home / 'state.db') as db:
                db.execute('CREATE TABLE async_delegations (state TEXT, result_json TEXT, task_json TEXT, dispatched_at REAL)')
            empty = subprocess.run(command, env=env, capture_output=True, text=True, check=True)
            self.assertIn('indeterminate', empty.stdout)
            self.assertIn('no terminal worker', empty.stdout)

    def test_nonfinite_dispatch_timestamp_does_not_hide_failed_worker(self):
        from hermes_cli.delegation_health import format_alert, read_summary

        for timestamp in (float('-inf'), float('inf'), 'PRIVATE_BAD_TIME'):
            with self.subTest(timestamp=timestamp), tempfile.TemporaryDirectory() as td:
                path = Path(td) / 'fixture.db'
                with sqlite3.connect(path) as db:
                    db.execute('CREATE TABLE async_delegations (state TEXT, result_json TEXT, task_json TEXT, dispatched_at REAL NOT NULL)')
                    healthy = (*batch_row('completed', [{'status': 'completed'}]), time.time())
                    hidden = (*batch_row('completed', [{'status': 'timeout'}]), timestamp)
                    old = (*batch_row('completed', [{'status': 'timeout'}]), -1e300)
                    db.executemany('INSERT INTO async_delegations VALUES (?, ?, ?, ?)', [healthy, hidden, old])
                before = path.read_bytes()
                summary = read_summary(path)
                self.assertEqual(summary['terminal_workers'], 1)
                self.assertEqual(summary['invalid_dispatch_timestamps'], 1)
                self.assertEqual(summary['future_dated_dispatches'], 0)
                self.assertEqual(summary['warning_state'], 'indeterminate_incomplete_data')
                self.assertIsNone(summary['warning_below_80_percent'])
                self.assertIn('indeterminate', format_alert(summary))
                self.assertNotIn('PRIVATE_BAD_TIME', json.dumps(summary))
                self.assertEqual(path.read_bytes(), before)

    def test_future_dispatches_do_not_create_a_healthy_24_hour_result(self):
        from hermes_cli.delegation_health import format_alert, read_summary

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'fixture.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE async_delegations (state TEXT, result_json TEXT, task_json TEXT, dispatched_at REAL)')
                now = time.time()
                recent = (*batch_row('completed', [{'status': 'timeout'}]), now - 60)
                future = (*batch_row('completed', [{'status': 'completed'}] * 5), now + 86400)
                db.executemany('INSERT INTO async_delegations VALUES (?, ?, ?, ?)', [recent, future])
            summary = read_summary(path)
            self.assertEqual(summary['batch_count'], 1)
            self.assertEqual(summary['terminal_workers'], 1)
            self.assertEqual(summary['completed_workers'], 0)
            self.assertEqual(summary['future_dated_dispatches'], 1)
            self.assertEqual(summary['warning_state'], 'indeterminate_incomplete_data')
            self.assertIsNone(summary['warning_below_80_percent'])
            self.assertIn('indeterminate', format_alert(summary))

    def test_window_endpoints_and_future_only_are_explicit(self):
        from unittest.mock import patch
        from hermes_cli.delegation_health import format_alert, read_summary

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'fixture.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE async_delegations (state TEXT, result_json TEXT, task_json TEXT, dispatched_at REAL)')
                row = batch_row('completed', [{'status': 'completed'}])
                db.executemany('INSERT INTO async_delegations VALUES (?, ?, ?, ?)', [
                    (*row, 100000 - 86400 - 1), (*row, 100000 - 86400),
                    (*row, 100000), (*row, 100001),
                ])
            before = path.read_bytes()
            with patch('hermes_cli.delegation_health.time.time', return_value=100000):
                summary = read_summary(path)
            self.assertEqual(summary['batch_count'], 2)
            self.assertEqual(summary['completed_workers'], 2)
            self.assertEqual(summary['future_dated_dispatches'], 1)
            self.assertEqual(summary['warning_state'], 'indeterminate_incomplete_data')
            self.assertEqual(path.read_bytes(), before)
            with sqlite3.connect(path) as db:
                db.execute('DELETE FROM async_delegations WHERE dispatched_at <= 100000')
            with patch('hermes_cli.delegation_health.time.time', return_value=100000):
                future_only = read_summary(path)
            self.assertEqual(future_only['batch_count'], 0)
            self.assertEqual(future_only['future_dated_dispatches'], 1)
            self.assertEqual(future_only['warning_state'], 'indeterminate_incomplete_data')
            self.assertIn('indeterminate', format_alert(future_only))

    def test_cli_reads_time_window_and_leaves_database_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'fixture.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE async_delegations (state TEXT, result_json TEXT, task_json TEXT, dispatched_at REAL)')
                current = (*batch_row('completed', [{'status':'completed'}]), time.time())
                old = (*batch_row('error', [{'status':'timeout'}]), time.time()-100000)
                db.executemany('INSERT INTO async_delegations VALUES (?, ?, ?, ?)', [current, old])
            before = path.read_bytes()
            process = subprocess.run([sys.executable, '-m', 'hermes_cli.delegation_health', '--db', str(path)], capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(process.stdout)['completion_rate'], 1)
            self.assertEqual(before, path.read_bytes())


if __name__ == '__main__':
    unittest.main()
