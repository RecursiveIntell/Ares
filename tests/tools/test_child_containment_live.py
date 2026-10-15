"""C05 live witness: what actually contains (or fails to contain) an escaped descendant.

Real processes, real cgroup v2, no provider, no shell-out to systemd. Everything here
is a WITNESS for a candidate mechanism (a dedicated cgroup subtree per owned operation,
plan 5.4). It does not wire containment into the product and grants no containment claim
by itself. It also records, as passing tests, the two limits the plan said must be shown
before such a mechanism is credited:

* process-group / lineage kill does not reach a double-forked ``setsid`` descendant;
* ``cgroup.events populated=0`` is NOT proof of quiescence if same-uid code can migrate
  itself out of the subtree, so tracked PID + birth identity must be checked as well.

Safety: the only thing ever killed through ``cgroup.kill`` is a uniquely named child
directory this test created under the test process's own cgroup. Escaped descendants are
killed individually, and only after their /proc start-time still matches what was recorded.
"""
import os
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.live_system_guard_bypass,
    pytest.mark.skipif(not sys.platform.startswith('linux'), reason='cgroup v2 witness is Linux-only'),
]

CG_ROOT = Path('/sys/fs/cgroup')

LEADER = textwrap.dedent('''
    import os, sys, time
    cg, pidfile, mode = sys.argv[1:4]
    with open(os.path.join(cg, "cgroup.procs"), "w") as f:
        f.write(str(os.getpid()))
    pid = os.fork()
    if pid == 0:
        os.setsid()                      # leave the leader's process group and session
        if os.fork() == 0:               # double fork: the leader will never be its parent
            if mode == "migrate":        # a same-uid descendant moving itself out of the subtree
                with open(os.path.join(os.path.dirname(cg), "cgroup.procs"), "w") as f:
                    f.write(str(os.getpid()))
            with open(pidfile, "w") as f:
                f.write(str(os.getpid()))
            time.sleep(300)
        os._exit(0)
    os.waitpid(pid, 0)
''')


def _own_cgroup():
    for line in Path('/proc/self/cgroup').read_text().splitlines():
        if line.startswith('0::'):
            return CG_ROOT / line[3:].lstrip('/')
    return None


def _birth(pid):
    """Process start time (clock ticks since boot): with the pid, a birth identity."""
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
    except OSError:
        return None
    return int(stat[stat.rindex(')') + 2:].split()[19])


def _state(pid):
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
    except OSError:
        return None
    return stat[stat.rindex(')') + 2]


def _alive(pid, birth):
    """Live (not zombie) and still the same process incarnation."""
    return _birth(pid) == birth and _state(pid) not in (None, 'Z', 'X')


def _populated(cg):
    for line in (cg / 'cgroup.events').read_text().splitlines():
        key, _, value = line.partition(' ')
        if key == 'populated':
            return value.strip() == '1'
    raise AssertionError('cgroup.events has no populated field')


def _members(cg):
    return {int(p) for p in (cg / 'cgroup.procs').read_text().split()}


def _wait(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def subtree(tmp_path):
    parent = _own_cgroup()
    if (parent is None or not (CG_ROOT / 'cgroup.controllers').exists()
            or not (parent / 'cgroup.kill').exists() or not os.access(parent, os.W_OK)
            or not os.access(parent / 'cgroup.procs', os.W_OK)):
        pytest.skip('no writable cgroup v2 subtree with cgroup.kill (delegation unavailable here)')
    child = parent / f'c05-witness-{uuid.uuid4().hex[:12]}'
    child.mkdir()
    assert child.parent == parent                      # strict direct child, never the parent itself
    tracked = []                                       # (pid, birth) of every process we may need to reap
    state = SimpleNamespace(parent=parent, child=child, tracked=tracked, tmp=tmp_path)
    try:
        yield state
    finally:
        try:
            (child / 'cgroup.kill').write_text('1')
        except OSError:
            pass
        for pid, birth in tracked:
            if _alive(pid, birth):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        _wait(lambda: not _populated(child), 5.0)
        try:
            child.rmdir()
        except OSError:
            pass


from types import SimpleNamespace  # noqa: E402  (kept below the fixture to keep the intent readable)


def _launch(sub, mode, name='d'):
    pidfile = sub.tmp / f'{name}.pid'
    leader = subprocess.Popen([sys.executable, '-c', LEADER, str(sub.child), str(pidfile), mode],
                              start_new_session=True)
    sub.tracked.append((leader.pid, _birth(leader.pid)))
    assert _wait(pidfile.exists, 10) and _wait(lambda: pidfile.read_text().strip() != '', 5)
    descendant = int(pidfile.read_text())
    birth = _birth(descendant)
    assert birth is not None
    sub.tracked.append((descendant, birth))
    leader.wait(timeout=10)                            # the leader is gone; only the descendant remains
    return leader.pid, descendant, birth


def test_the_subtree_is_a_strict_child_with_only_our_own_processes(subtree):
    leader_pid, descendant, birth = _launch(subtree, 'stay')
    assert subtree.child.parent == subtree.parent and subtree.child != subtree.parent
    assert _members(subtree.child) == {descendant}
    assert _alive(descendant, birth)


def test_group_and_lineage_kill_do_not_reach_a_double_forked_setsid_descendant(subtree):
    leader_pid, descendant, birth = _launch(subtree, 'stay')
    assert not Path(f'/proc/{leader_pid}').exists() or _state(leader_pid) in ('Z', 'X', None)
    # Lineage: the leader is gone, so a walk from it finds nothing, and the descendant is not its child.
    stat = Path(f'/proc/{descendant}/stat').read_text()
    ppid = int(stat[stat.rindex(')') + 2:].split()[1])
    assert ppid != leader_pid
    # Process group: the descendant lives in its own session, so killpg(leader's group) cannot name it.
    assert os.getpgid(descendant) != leader_pid and os.getsid(descendant) != os.getsid(0)
    try:
        os.killpg(leader_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    time.sleep(0.2)
    assert _alive(descendant, birth), 'tree/group kill unexpectedly reached the escaped descendant'


def test_cgroup_kill_empties_the_subtree_and_the_tracked_identity_is_gone(subtree):
    leader_pid, descendant, birth = _launch(subtree, 'stay')
    assert _populated(subtree.child)
    (subtree.child / 'cgroup.kill').write_text('1')
    assert _wait(lambda: not _populated(subtree.child), 5), 'subtree still populated after cgroup.kill'
    # Independent confirmation: the emptiness signal and the tracked PID+birth identity agree.
    assert _wait(lambda: not _alive(descendant, birth), 5)
    assert _members(subtree.child) == set()


def test_cgroup_kill_does_not_touch_a_sibling_outside_the_subtree(subtree):
    sibling = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'], start_new_session=True)
    subtree.tracked.append((sibling.pid, _birth(sibling.pid)))
    assert sibling.pid in _members(subtree.parent)
    _launch(subtree, 'stay')
    (subtree.child / 'cgroup.kill').write_text('1')
    assert _wait(lambda: not _populated(subtree.child), 5)
    assert sibling.poll() is None and _alive(sibling.pid, _birth(sibling.pid))
    sibling.kill()
    sibling.wait(timeout=5)


def test_emptiness_alone_is_not_quiescence_when_a_same_uid_descendant_migrates_out(subtree):
    """Negative witness. The subtree reports populated=0 while the escaped work is still running."""
    leader_pid, descendant, birth = _launch(subtree, 'migrate')
    assert _wait(lambda: descendant in _members(subtree.parent), 5), 'descendant did not migrate'
    (subtree.child / 'cgroup.kill').write_text('1')
    assert _wait(lambda: not _populated(subtree.child), 5)
    # A check that stops at "subtree empty" would declare quiescence here, wrongly:
    assert _alive(descendant, birth), 'migrated descendant was unexpectedly killed'
    # The tracked identity is what exposes it, so it is mandatory beside the cgroup signal.
    assert descendant in _members(subtree.parent)


def test_a_reused_pid_is_not_mistaken_for_the_tracked_process(subtree):
    leader_pid, descendant, birth = _launch(subtree, 'stay')
    assert _alive(descendant, birth)
    assert not _alive(descendant, birth + 1), 'identity check must include birth, not only the pid'
