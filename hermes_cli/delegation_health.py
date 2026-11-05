#!/usr/bin/env python3
"""Read-only per-worker outcomes from Ares async delegation receipts.

Use only terminal worker outcomes, not parent batch states, for the denominator.
Missing, malformed, and active receipts remain visible and are not successes.
"""
import argparse
import collections
from contextlib import closing
import json
import math
import os
import re
import sqlite3
import stat
import time
from pathlib import Path

TERMINAL = frozenset({'completed', 'timeout', 'error', 'failed', 'cancelled', 'canceled', 'interrupted'})

_TIMING_ID = re.compile(r'deleg_[0-9a-f]{8}\Z')
_TIMING_RESULT = re.compile(r'^\d{2}:\d{2}:\d{2} result\s*\|\s*(?P<tool>\S+)\s+(?:ok|ERROR)\s+(?P<seconds>\d+(?:\.\d+)?)s:')
_TIMING_RESULT_PREFIX = re.compile(r'^\d{2}:\d{2}:\d{2} result\s*\|')
_TIMING_API_WINDOW = re.compile(r'^\d{2}:\d{2}:\d{2} timing\s*\| api-window (\d+(?:\.\d+)?)s$')
_TIMING_API_PREFIX = re.compile(r'^\d{2}:\d{2}:\d{2} timing\s*\| api-window(?:\s|$)')
_TIMING_MAX_LOG_BYTES = 2 * 1024 * 1024
_TIMING_MAX_TASKS_PER_BATCH = 128
_TIMING_NAMED_TOOLS = frozenset({'terminal', 'read_file', 'write_file', 'search_files'})
_DISPATCH_WINDOW_WHERE = (
    "dispatched_at >= ? OR dispatched_at IS NULL "
    "OR typeof(dispatched_at) NOT IN ('integer', 'real') "
    "OR dispatched_at < -1.7976931348623157e308"
)


def _valid_dispatch_timestamp(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _read_timing_log(root_fd, identifier, index):
    """Read a bounded stable task leaf without following run/leaf links."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    directory_fd = os.open(identifier, directory_flags, dir_fd=root_fd)
    try:
        name = f'task-{index}.log'
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_size > _TIMING_MAX_LOG_BYTES
                    or (hasattr(os, 'geteuid') and before.st_uid != os.geteuid())):
                raise OSError('unsafe transcript')
            blocks = []
            remaining = _TIMING_MAX_LOG_BYTES + 1
            while remaining:
                block = os.read(fd, min(remaining, 65536))
                if not block:
                    break
                blocks.append(block)
                remaining -= len(block)
            after = os.fstat(fd)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_nlink)
            if identity(before) != identity(after) or identity(after) != identity(current):
                raise OSError('transcript changed during read')
            data = b''.join(blocks)
            if len(data) > _TIMING_MAX_LOG_BYTES:
                raise OSError('transcript exceeds read bound')
            text = data.decode('utf-8')
            expected_header = f'delegation: {identifier}   task: {index}'
            lines = text.splitlines()
            if len(lines) < 2 or lines[0] != '=== Hermes subagent live transcript ===' or lines[1] != expected_header:
                raise OSError('transcript identity mismatch')
            return lines
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def read_tool_timing(db_path: Path, hours: float = 24):
    """Aggregate directly logged tool durations for the dispatch-time window.

    Reads only index-addressed live logs for validated receipt identifiers;
    incomplete coverage is reported, never extrapolated to missing workers.
    This separate query is not an atomic snapshot with `read_summary`.
    """
    if not math.isfinite(hours) or hours <= 0:
        raise ValueError('hours must be finite and positive')
    from hermes_constants import get_hermes_dir

    path = db_path.resolve()
    now = time.time()
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        rows = db.execute(
            'SELECT delegation_id, task_json, dispatched_at FROM async_delegations WHERE '
            + _DISPATCH_WINDOW_WHERE, (now - hours * 3600,),
        ).fetchall()
    flags_ok = (all(hasattr(os, key) for key in ('O_DIRECTORY', 'O_NOFOLLOW', 'O_NONBLOCK'))
                and os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd
                and os.stat in os.supports_follow_symlinks)
    root_fd = None
    if flags_ok:
        root = get_hermes_dir('cache/delegation', 'delegation_cache', home=path.parent) / 'live'
        try:
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            pass
    expected = readable = missing = invalid = future_count = unparsed = 0
    invalid_timestamps = 0
    durations = []
    api_durations = []
    api_transcripts = api_unparsed = 0
    durations_by_class = collections.defaultdict(list)
    try:
        for identifier, payload, dispatched_at in rows:
            if not _valid_dispatch_timestamp(dispatched_at):
                invalid_timestamps += 1
                continue
            if dispatched_at > now:
                future_count += 1
                continue
            try:
                task = json.loads(payload)
                if not isinstance(identifier, str) or not _TIMING_ID.fullmatch(identifier) or not isinstance(task, dict):
                    raise ValueError('invalid dispatch')
                if task.get('is_batch') is True:
                    goals = task.get('goals')
                    if (not isinstance(goals, list) or not 1 <= len(goals) <= _TIMING_MAX_TASKS_PER_BATCH
                            or any(not isinstance(goal, str) or not goal.strip() for goal in goals)):
                        raise ValueError('invalid goals')
                    count = len(goals)
                elif (task.get('is_batch') is None or task.get('is_batch') is False) and isinstance(task.get('goal'), str) and task['goal'].strip():
                    count = 1
                else:
                    raise ValueError('invalid dispatch')
            except (TypeError, ValueError):
                invalid += 1
                continue
            expected += count
            for index in range(count):
                try:
                    if root_fd is None:
                        raise OSError('transcript root unavailable')
                    lines = _read_timing_log(root_fd, identifier, index)
                except (OSError, UnicodeError):
                    missing += 1
                    continue
                readable += 1
                api_seen = False
                for line in lines:
                    api_match = _TIMING_API_WINDOW.match(line)
                    if api_match:
                        api_seconds = float(api_match.group(1))
                        if math.isfinite(api_seconds):
                            api_durations.append(api_seconds)
                            api_seen = True
                        else:
                            api_unparsed += 1
                    elif _TIMING_API_PREFIX.match(line):
                        api_unparsed += 1
                    match = _TIMING_RESULT.match(line)
                    if match:
                        seconds = float(match.group('seconds'))
                        if math.isfinite(seconds):
                            durations.append(seconds)
                            name = match.group('tool')
                            durations_by_class[name if name in _TIMING_NAMED_TOOLS else 'other'].append(seconds)
                        else:
                            unparsed += 1
                    elif _TIMING_RESULT_PREFIX.match(line):
                        unparsed += 1
                api_transcripts += int(api_seen)
    finally:
        if root_fd is not None:
            os.close(root_fd)
    durations.sort()
    api_durations.sort()
    def percentile(values, q):
        return values[max(0, math.ceil(q * len(values)) - 1)] if values else None
    groups = {}
    for name, values in sorted(durations_by_class.items()):
        values.sort()
        groups[name] = {'samples': len(values), 'p50_seconds': percentile(values, .50),
                        'p90_seconds': percentile(values, .90)}
    return {
        'window_hours': hours,
        'expected_transcripts': expected,
        'readable_transcripts': readable,
        'missing_or_unsafe_transcripts': missing,
        'invalid_dispatches': invalid,
        'future_dated_dispatches': future_count,
        'invalid_dispatch_timestamps': invalid_timestamps,
        'unparsed_result_lines': unparsed,
        'observed_tool_results': len(durations),
        'tool_duration_seconds_nearest_rank_p50': percentile(durations, .50),
        'tool_duration_seconds_nearest_rank_p90': percentile(durations, .90),
        'tool_duration_by_class': groups,
        'api_window_samples': len(api_durations),
        'api_window_transcripts_with_samples': api_transcripts,
        'api_window_unparsed_lines': api_unparsed,
        'api_window_seconds_nearest_rank_p50': percentile(api_durations, .50),
        'api_window_seconds_nearest_rank_p90': percentile(api_durations, .90),
        'coverage': ('all_expected_transcripts_read' if expected and not (missing or invalid or future_count or invalid_timestamps or unparsed or api_unparsed)
                     else 'partial' if expected or invalid or future_count or invalid_timestamps else 'no_dispatches'),
        'limitations': [
            'Transcript-reported tool results only; rounded to 0.1s and may omit events.',
            'Transcript lines are unauthenticated; older logs may contain spoofed events.',
            'Named tool classes are fixed; all other or unrecognized names are combined.',
            'Missing, active, expired or replaced logs may bias the observed samples.',
            'Tool calls can overlap; durations are not additive wall time.',
            'API-window samples are successful logical response windows, including middleware and retries; not provider-only time.',
            'Older logs, failed and interrupted windows may have no API-window samples.',
            'No timeout cause is inferred.',
            'Separate query from outcome summary; concurrent activity can change the window.',
        ],
    }


def summarize(rows):
    outcomes = collections.Counter()
    timeout_phases = collections.Counter()
    durations = []
    invalid_receipts = 0
    unfinished_batches = 0
    unsettled_batches = 0
    missing_worker_status = 0
    invalid_dispatch = 0
    invalid_worker_sets = 0
    completed_workers = 0
    unverified_completed_workers = 0
    workers_truncated = 0
    workers_schema_invalid = 0
    for state, payload, task_payload in rows:
        if state == 'running':
            unfinished_batches += 1
        if state not in TERMINAL:
            unsettled_batches += 1
        try:
            task = json.loads(task_payload)
            if not isinstance(task, dict):
                raise ValueError('dispatch must be an object')
            if task.get('is_batch') is True:
                goals = task.get('goals')
                if not isinstance(goals, list) or not goals or any(
                    not isinstance(goal, str) or not goal.strip() for goal in goals
                ):
                    raise ValueError('batch must have explicit goals')
                expected, is_batch = len(goals), True
            elif (task.get('is_batch') is None or task.get('is_batch') is False) and isinstance(task.get('goal'), str) and task['goal'].strip():
                expected, is_batch = 1, False
            else:
                raise ValueError('unknown dispatch shape')
        except (ValueError, TypeError, AttributeError):
            invalid_dispatch += 1
            expected, is_batch = None, None
        if not payload:
            invalid_receipts += 1
            continue
        try:
            document = json.loads(payload)
            if not isinstance(document, dict):
                raise ValueError('receipt must be an object')
            workers = document.get('results')
            if is_batch is False or (is_batch is None and workers is None):
                workers = [document]
            if not isinstance(workers, list) or not workers:
                raise ValueError('results must be a non-empty list')
        except (ValueError, TypeError, KeyError):
            invalid_receipts += 1
            continue
        if is_batch is True:
            indices = [worker.get('task_index') if isinstance(worker, dict) else None
                       for worker in workers]
            if (len(workers) != expected or
                    any(type(index) is not int for index in indices) or
                    set(indices) != set(range(expected))):
                invalid_worker_sets += 1
        for worker in workers:
            if not isinstance(worker, dict) or not isinstance(worker.get('status'), str):
                missing_worker_status += 1
                continue
            status = worker['status'].lower()
            if status not in TERMINAL and status not in {'running', 'pending'}:
                status = 'unknown'
            outcomes[status] += 1
            if status == 'completed':
                reason = worker.get('exit_reason')
                truncated = worker.get('truncated')
                schema_valid = worker.get('schema_valid')
                is_truncated = reason == 'max_iterations' or truncated is True
                is_schema_invalid = schema_valid is False
                workers_truncated += int(is_truncated)
                workers_schema_invalid += int(is_schema_invalid)
                malformed = (
                    ('truncated' in worker and type(truncated) is not bool)
                    or ('schema_valid' in worker and type(schema_valid) is not bool)
                    or reason not in ('completed', 'max_iterations')
                    or (type(truncated) is bool and truncated != (reason == 'max_iterations'))
                )
                if malformed:
                    unverified_completed_workers += 1
                elif not is_truncated and not is_schema_invalid:
                    completed_workers += 1
            duration = worker.get('duration_seconds')
            if isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration) and duration >= 0:
                durations.append(duration)
            if status == 'timeout':
                phase = worker.get('timeout_phase')
                if not isinstance(phase, str) or phase not in {'before_first_llm_call', 'after_llm_calls'}:
                    phase = 'unknown'
                timeout_phases[phase] += 1
    denominator = sum(outcomes[s] for s in TERMINAL)
    complete = completed_workers
    incomplete_workers = sum(count for status, count in outcomes.items() if status not in TERMINAL)
    incomplete = bool(invalid_receipts or unsettled_batches or missing_worker_status or
                      incomplete_workers or invalid_dispatch or invalid_worker_sets or
                      unverified_completed_workers)
    rate = complete / denominator if denominator else None
    warning = None if incomplete or rate is None else rate < .8
    warning_state = ('indeterminate_incomplete_data' if incomplete else
                     'indeterminate_no_terminal_workers' if rate is None else
                     'below_threshold' if warning else 'at_or_above_threshold')
    durations.sort()
    def percentile(q):
        return durations[max(0, math.ceil(q * len(durations)) - 1)] if durations else None
    return {'worker_outcomes': dict(sorted(outcomes.items())),
            'terminal_workers': denominator,
            'completed_workers': complete,
            'completed_status_workers': outcomes['completed'],
            'workers_truncated': workers_truncated,
            'workers_schema_invalid': workers_schema_invalid,
            'unverified_completed_workers': unverified_completed_workers,
            'completion_rate': rate,
            'warning_below_80_percent': warning,
            'warning_state': warning_state,
            'nonterminal_or_unknown_workers': incomplete_workers,
            'duration_seconds_nearest_rank_p50': percentile(.50),
            'duration_seconds_nearest_rank_p90': percentile(.90),
            'duration_sample_count': len(durations),
            'timeout_phases_not_causes': dict(sorted(timeout_phases.items())),
            'invalid_or_missing_receipts': invalid_receipts,
            'workers_missing_status': missing_worker_status,
            'running_batches': unfinished_batches,
            'unsettled_or_unknown_batches': unsettled_batches,
            'invalid_or_missing_dispatches': invalid_dispatch,
            'invalid_worker_sets': invalid_worker_sets,
            'limitations': ['Dispatch-time window; receipts can be incomplete or expired.',
                            'Completion rate covers observed terminal workers; it may be biased when data is incomplete.',
                            'A successful exit is not proof of task correctness; schema metadata is checked only when present.',
                            'Timeout phase is not root cause.',
                            'Turn and tool durations are unavailable from these receipts.']}


def read_summary(db_path: Path, hours: float = 24):
    """Read one profile's existing receipts without creating or modifying its DB."""
    if not math.isfinite(hours) or hours <= 0:
        raise ValueError('hours must be finite and positive')
    path = db_path.resolve()
    uri = path.as_uri() + '?mode=ro'
    now = time.time()
    with closing(sqlite3.connect(uri, uri=True)) as db:
        # One statement observes one SQLite snapshot for the entire window.
        # Future dispatches are not part of the window and make its threshold
        # indeterminate rather than silently fabricating a healthy rate.
        # Include malformed timestamps even when they cannot be assigned to a
        # window; otherwise a corrupt old-looking timestamp can hide a failure.
        # Keep this and the window rows in one SQLite statement/snapshot.
        records = db.execute(
            'SELECT state, result_json, task_json, dispatched_at FROM async_delegations WHERE '
            + _DISPATCH_WINDOW_WHERE,
            (now - hours * 3600,),
        ).fetchall()
    valid_rows = []
    invalid_timestamps = 0
    future_dated = 0
    for state, receipt, task, dispatched_at in records:
        if not _valid_dispatch_timestamp(dispatched_at):
            invalid_timestamps += 1
        elif dispatched_at > now:
            future_dated += 1
        else:
            valid_rows.append((state, receipt, task))
    result = summarize(valid_rows)
    if future_dated or invalid_timestamps:
        result['warning_below_80_percent'] = None
        result['warning_state'] = 'indeterminate_incomplete_data'
    result.update({'window_hours': hours, 'batch_count': len(valid_rows), 'source': str(path),
                   'future_dated_dispatches': future_dated,
                   'invalid_dispatch_timestamps': invalid_timestamps})
    return result


def format_alert(summary):
    """Render a content-free warning, or None when the threshold is met.

    An incomplete or empty observation is not evidence of health. This helper
    does not schedule, deliver, deduplicate, or persist alerts.
    """
    state = summary['warning_state']
    if state == 'at_or_above_threshold':
        return None
    if state == 'below_threshold':
        return (
            'Delegation health below 80%: '
            f"{summary['completed_workers']}/{summary['terminal_workers']} "
            f"observed terminal workers completed in the last {summary['window_hours']:g} hours."
        )
    if state == 'indeterminate_no_terminal_workers':
        return 'Delegation health indeterminate: no terminal worker receipts in the selected window.'
    return 'Delegation health indeterminate: worker receipt data is incomplete or inconsistent.'


def main():
    from hermes_constants import get_hermes_home
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=get_hermes_home() / 'state.db')
    parser.add_argument('--hours', type=float, default=24)
    args = parser.parse_args()
    try:
        result = read_summary(args.db, args.hours)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
