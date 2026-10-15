"""Progress witness for the delegation inactivity mode (C04).

Limits used here are TEST FIXTURES. The module has no defaults on purpose:
choosing a production number belongs to the operator, set from measured owners
(plan 5.3), not to this file.
"""
import ast
import subprocess
import threading
from pathlib import Path

import pytest

from agent.progress_witness import (
    PROGRESS_EMITTERS, InactivityLimits, InactivityLimitsError, ProgressKind, ProgressWitness,
)

ROOT = Path(__file__).resolve().parents[2]
G = 'gen-1'


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def make(stall=100, request=300, tool=500, ceiling=None, milestones=('built',)):
    clock = Clock()
    witness = ProgressWitness(generation=G, limits=InactivityLimits(stall, request, tool, ceiling),
                              allowed_milestones=milestones, clock=clock)
    return witness, clock


def state(witness, clock):
    return witness.evaluate(clock()).state


# --- stall budget -----------------------------------------------------------

def test_stall_is_measured_from_admission_and_fires_exactly_at_the_limit():
    w, c = make()
    c.advance(99.9)
    assert state(w, c) == 'ok'
    c.advance(0.1)
    assert state(w, c) == 'stalled'


def test_accepted_progress_resets_only_the_stall_budget():
    w, c = make()
    w.open_request('r1', generation=G, streaming=True)
    c.advance(90)
    assert w.record_model_delta('r1', 12, generation=G)
    c.advance(90)
    assert state(w, c) == 'ok'
    c.advance(10)
    assert state(w, c) == 'stalled'


def test_opening_a_request_or_tool_is_never_progress():
    w, c = make()
    c.advance(60)
    w.open_request('r1', generation=G, streaming=True)
    w.open_tool('t1', generation=G)
    assert w.snapshot().last_progress_at is None
    assert w.snapshot().accepted == {}


@pytest.mark.parametrize('nbytes', [0, -1, True, 1.5, '3', None])
def test_empty_or_malformed_model_delta_is_not_progress(nbytes):
    w, c = make()
    w.open_request('r1', generation=G, streaming=True)
    c.advance(99)
    assert not w.record_model_delta('r1', nbytes, generation=G)
    c.advance(1)
    assert state(w, c) == 'stalled'
    assert w.snapshot().rejected == {'empty_delta': 1}


# --- hazard: retries, kill stamps and failed attempts ---------------------------

def test_failed_attempts_and_retries_cannot_reset_the_stall_budget():
    w, c = make(stall=100)
    for attempt in range(5):
        op = f'r{attempt}'
        assert w.open_request(op, generation=G, streaming=True)
        c.advance(20)
        assert w.abandon_request(op, generation=G)
    # Five full retry cycles elapsed (100 s) and none of them moved the stall clock.
    assert w.snapshot().last_progress_at is None
    assert w.snapshot().accepted == {}
    assert state(w, c) == 'stalled'


def test_a_completed_response_counts_once_and_replays_are_rejected():
    w, c = make()
    w.open_request('r1', generation=G, streaming=False)
    assert w.complete_request('r1', generation=G)
    assert not w.complete_request('r1', generation=G)
    assert not w.open_request('r1', generation=G, streaming=False)   # a completed id cannot be reopened
    assert w.snapshot().accepted == {ProgressKind.MODEL_COMPLETE.value: 1}


def test_delta_or_completion_for_an_unopened_request_is_rejected():
    w, _ = make()
    assert not w.record_model_delta('ghost', 5, generation=G)
    assert not w.complete_request('ghost', generation=G)
    assert w.snapshot().rejected == {'unknown_request': 2}


# --- hazard: heartbeat-only liveness ----------------------------------------------

def test_a_silent_streaming_request_stalls_even_though_it_is_open():
    w, c = make()
    w.open_request('r1', generation=G, streaming=True)
    c.advance(100)
    assert state(w, c) == 'stalled'


def test_a_silent_nonstreaming_request_is_bounded_by_its_own_request_limit_not_forever():
    w, c = make(stall=100, request=300)
    w.open_request('r1', generation=G, streaming=False)
    c.advance(299)
    assert state(w, c) == 'ok'          # stall suspended: this operation has its own finite budget
    c.advance(1)
    assert state(w, c) == 'request_limit'


def test_a_streaming_request_cannot_be_kept_alive_past_the_request_limit_by_deltas():
    w, c = make(stall=100, request=300)
    w.open_request('r1', generation=G, streaming=True)
    for _ in range(30):
        c.advance(10)
        w.record_model_delta('r1', 4, generation=G)
    assert state(w, c) == 'request_limit'


# --- tools ----------------------------------------------------------------------------

def test_polling_an_unchanged_tool_result_is_not_progress_but_growth_is():
    w, c = make(stall=100, tool=500)
    w.open_tool('t1', generation=G)
    assert w.record_tool_output('t1', 10, generation=G)
    for _ in range(3):
        assert not w.record_tool_output('t1', 10, generation=G)
    assert not w.record_tool_output('t1', 9, generation=G)
    assert w.record_tool_output('t1', 11, generation=G)
    assert w.snapshot().accepted == {ProgressKind.TOOL_OUTPUT.value: 2}
    assert w.snapshot().rejected == {'unchanged_tool_output': 4}


def test_an_open_tool_is_bounded_by_its_limit_even_when_its_output_keeps_growing():
    w, c = make(stall=100, tool=500)
    w.open_tool('t1', generation=G)
    total = 0
    for _ in range(60):
        c.advance(10)
        total += 5
        w.record_tool_output('t1', total, generation=G)
    assert state(w, c) == 'tool_limit'


def test_tool_completion_is_progress_once_and_closes_the_governed_window():
    w, c = make(stall=100, tool=500)
    w.open_tool('t1', generation=G)
    c.advance(400)
    assert state(w, c) == 'ok'          # governed by tool_seconds, not stall
    assert w.complete_tool('t1', generation=G)
    assert not w.complete_tool('t1', generation=G)
    c.advance(99)
    assert state(w, c) == 'ok'
    c.advance(1)
    assert state(w, c) == 'stalled'


def test_an_abandoned_tool_is_neither_progress_nor_an_open_operation():
    w, c = make()
    w.open_tool('t1', generation=G)
    assert w.abandon_tool('t1', generation=G)
    assert w.snapshot().open_tools == 0 and w.snapshot().accepted == {}


# --- initialization -----------------------------------------------------------------

def test_only_declared_milestones_count_and_each_counts_once():
    w, _ = make(milestones=('built',))
    assert not w.record_init_milestone('surprise', generation=G)
    assert w.record_init_milestone('built', generation=G)
    assert not w.record_init_milestone('built', generation=G)
    assert w.snapshot().rejected == {'undeclared_milestone': 1, 'repeated_milestone': 1}


def test_no_milestones_declared_means_initialization_progress_is_impossible():
    w, _ = make(milestones=())
    assert not w.record_init_milestone('built', generation=G)


# --- generation fencing and closure ---------------------------------------------------

def test_late_events_from_another_generation_are_rejected_without_effect():
    w, c = make()
    w.open_request('r1', generation=G, streaming=True)
    c.advance(100)
    assert not w.record_model_delta('r1', 50, generation='gen-0')
    assert not w.complete_request('r1', generation='gen-0')
    assert state(w, c) == 'stalled'
    assert w.snapshot().rejected == {'stale_generation': 2}


def test_a_closed_witness_accepts_nothing_and_reports_closed():
    w, c = make()
    w.open_request('r1', generation=G, streaming=True)
    w.close()
    assert not w.record_model_delta('r1', 5, generation=G)
    assert not w.open_tool('t1', generation=G)
    assert w.snapshot().closed and state(w, c) == 'closed'
    assert w.snapshot().rejected == {'closed': 2}


# --- one admission deadline ------------------------------------------------------------

def test_total_ceiling_runs_from_the_single_admission_instant_whatever_the_progress():
    w, c = make(stall=100, request=1000, tool=1000, ceiling=250)
    w.open_request('r1', generation=G, streaming=True)
    for _ in range(24):
        c.advance(10)
        w.record_model_delta('r1', 4, generation=G)
    assert state(w, c) == 'ok'
    c.advance(10)
    assert state(w, c) == 'ceiling'


def test_no_public_operation_resets_admission_time_or_the_budget():
    w, c = make(ceiling=250)
    before = w.admitted_at
    c.advance(200)
    w.open_request('repair-pass', generation=G, streaming=True)   # e.g. a schema-repair attempt
    w.complete_request('repair-pass', generation=G)
    assert w.admitted_at == before
    with pytest.raises(AttributeError):
        w.admitted_at = c()
    assert not hasattr(w, 'reset') and not hasattr(w, 'extend')


def test_verdict_precedence_is_deterministic():
    w, c = make(stall=10, request=20, tool=20, ceiling=30)
    w.open_request('r1', generation=G, streaming=False)
    w.open_tool('t1', generation=G)
    c.advance(30)
    assert state(w, c) == 'ceiling'
    w2, c2 = make(stall=10, request=20, tool=20, ceiling=None)
    w2.open_request('r1', generation=G, streaming=False)
    w2.open_tool('t1', generation=G)
    c2.advance(20)
    assert state(w2, c2) == 'request_limit'


# --- limits grammar --------------------------------------------------------------------

@pytest.mark.parametrize('bad', [True, False, 0, -1, float('nan'), float('inf'), '60', None, [], threading.TIMEOUT_MAX * 2])
@pytest.mark.parametrize('field', ['stall_seconds', 'request_seconds', 'tool_seconds'])
def test_every_required_limit_refuses_invalid_values(field, bad):
    values = {'stall_seconds': 10, 'request_seconds': 10, 'tool_seconds': 10}
    values[field] = bad
    with pytest.raises(InactivityLimitsError, match='DELEGATION_INACTIVITY_LIMITS_INVALID'):
        InactivityLimits(**values)


@pytest.mark.parametrize('bad', [True, 0, -5, float('nan'), '9'])
def test_an_invalid_optional_ceiling_refuses_rather_than_disabling_itself(bad):
    with pytest.raises(InactivityLimitsError):
        InactivityLimits(10, 10, 10, bad)


def test_limits_have_no_defaults_and_no_witness_without_them():
    with pytest.raises(TypeError):
        InactivityLimits()
    with pytest.raises(TypeError):
        InactivityLimits(10, 10)
    with pytest.raises(InactivityLimitsError):
        ProgressWitness(generation=G, limits=None)
    with pytest.raises(InactivityLimitsError):
        ProgressWitness(generation='', limits=InactivityLimits(1, 1, 1))


def test_concurrent_emitters_keep_counts_exact():
    w, _ = make()
    for i in range(8):
        w.open_tool(f't{i}', generation=G)

    def work(i):
        for n in range(1, 101):
            w.record_tool_output(f't{i}', n, generation=G)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert w.snapshot().accepted == {ProgressKind.TOOL_OUTPUT.value: 800}


# --- structural guards: the closed emitter set --------------------------------------------

# Where a future wiring change may call the progress emitters. Anything else is a
# new, unreviewed source of "progress" and must fail here first.
EMITTER_OWNERS = {
    'agent/progress_witness.py',
    'agent/operation_progress.py',  # scoped SDK operation owner; no tick/presentation emitters
    'agent/chat_completion_helpers.py', 'agent/codex_runtime.py', 'agent/conversation_loop.py',
    'agent/tool_executor.py', 'tools/environments/base.py', 'tools/delegate_tool.py',
}
# Shared tick sources that must never feed the witness.
TICK_SOURCES = {'agent/stream_diag.py', 'agent/session_activity.py', 'gateway/run.py', 'run_agent.py'}


def _source_files():
    out = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', '*.py'],
                         cwd=ROOT, capture_output=True).stdout.split(b'\0')
    return [p.decode() for p in out if p and not p.decode().startswith('tests/')]


def test_progress_emitters_are_only_called_from_declared_owner_files():
    offenders = []
    for rel in _source_files():
        text = (ROOT / rel).read_text(errors='ignore')
        if not any(name in text for name in PROGRESS_EMITTERS):
            continue
        for node in ast.walk(ast.parse(text)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in PROGRESS_EMITTERS and rel not in EMITTER_OWNERS):
                offenders.append((rel, node.lineno, node.func.attr))
    assert offenders == []


def test_shared_tick_sources_never_reference_the_witness():
    for rel in sorted(TICK_SOURCES):
        text = (ROOT / rel).read_text(errors='ignore')
        assert 'progress_witness' not in text, rel
        assert not any(name in text for name in PROGRESS_EMITTERS), rel


def test_witness_is_pure_stdlib_and_imports_no_runtime_owner():
    tree = ast.parse((ROOT / 'agent/progress_witness.py').read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add((node.module or '').split('.')[0])
    assert modules <= {'__future__', 'enum', 'math', 'threading', 'time', 'dataclasses', 'typing'}
