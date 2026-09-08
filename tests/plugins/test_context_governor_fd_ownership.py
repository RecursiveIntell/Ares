"""Concurrency regression tests for operation-local governed descriptors."""

import threading
from types import SimpleNamespace

import pytest

from plugins.context_engine._context_governor import ContextGovernorEngine


class _Binding:
    def __init__(self, marker: int) -> None:
        self.marker = marker
        self.pass_fds = (marker, marker + 100)
        self.close_count = 0

    def command_args(self) -> list[str]:
        return ["--governed-key-fd", str(self.marker), "--governed-snapshot-fd", str(self.marker + 100)]

    def close(self) -> None:
        self.close_count += 1


def test_certified_subprocess_owns_its_binding_across_overlap() -> None:
    engine = ContextGovernorEngine.__new__(ContextGovernorEngine)
    bindings = [_Binding(11), _Binding(22)]
    binding_lock = threading.Lock()

    def active_binding() -> _Binding:
        with binding_lock:
            return bindings.pop(0)

    engine._key_state = SimpleNamespace(active_binding=active_binding)
    a_entered = threading.Event()
    release_a = threading.Event()
    calls: list[tuple[list[str], tuple[int, ...]]] = []

    def run_json(args, payload, *, pass_fds=()):
        del payload
        marker = int(args[args.index("--governed-key-fd") + 1])
        calls.append((list(args), tuple(pass_fds)))
        if marker == 11:
            a_entered.set()
            assert release_a.wait(2)
        return {"marker": marker}

    engine._run_json = run_json
    results: dict[str, dict] = {}
    thread_a = threading.Thread(
        target=lambda: results.setdefault("a", engine._run_certified_json(["compact-v2"], {}))
    )
    thread_a.start()
    assert a_entered.wait(2)
    results["b"] = engine._run_certified_json(["search"], {})
    assert bindings == []
    # B may close only its own binding while A remains in flight.
    assert calls[0][1] == (11, 111)
    assert calls[1][1] == (22, 122)
    release_a.set()
    thread_a.join(2)
    assert not thread_a.is_alive()
    assert results == {"a": {"marker": 11}, "b": {"marker": 22}}
    for args, pass_fds in calls:
        argv_fds = {
            int(args[args.index("--governed-key-fd") + 1]),
            int(args[args.index("--governed-snapshot-fd") + 1]),
        }
        assert argv_fds == set(pass_fds)


def test_certified_binding_closes_once_on_timeout_without_touching_peer() -> None:
    engine = ContextGovernorEngine.__new__(ContextGovernorEngine)
    timed_out = _Binding(31)
    peer = _Binding(42)
    bindings = iter((timed_out, peer))
    engine._key_state = SimpleNamespace(active_binding=lambda: next(bindings))

    def run_json(args, payload, *, pass_fds=()):
        del payload, pass_fds
        marker = int(args[args.index("--governed-key-fd") + 1])
        if marker == 31:
            raise TimeoutError("child timeout")
        assert timed_out.close_count == 1
        assert peer.close_count == 0
        return {"ok": True}

    engine._run_json = run_json
    with pytest.raises(TimeoutError, match="child timeout"):
        engine._run_certified_json(["compact-v2"], {})
    assert engine._run_certified_json(["search"], {}) == {"ok": True}
    assert timed_out.close_count == 1
    assert peer.close_count == 1


def test_detached_worker_binding_does_not_close_new_operation_binding() -> None:
    engine = ContextGovernorEngine.__new__(ContextGovernorEngine)
    old_worker = _Binding(51)
    new_worker = _Binding(61)
    bindings = iter((old_worker, new_worker))
    setattr(engine, "_key_state", SimpleNamespace(active_binding=lambda: next(bindings)))
    old_started = threading.Event()
    release_old = threading.Event()

    def run_json(args, payload, *, pass_fds=()):
        del payload
        marker = int(args[args.index("--governed-key-fd") + 1])
        assert tuple(pass_fds) == (marker, marker + 100)
        if marker == old_worker.marker:
            old_started.set()
            assert release_old.wait(2)
        return {"marker": marker}

    engine._run_json = run_json
    results: dict[str, dict] = {}
    old_thread = threading.Thread(
        target=lambda: results.setdefault("old", engine._run_certified_json(["compact-v2"], {}))
    )
    old_thread.start()
    assert old_started.wait(2)

    # The host may fence-cancel the old worker while it winds down. A new
    # operation must remain independently paired with its own descriptors.
    results["new"] = engine._run_certified_json(["prepare-v2"], {})
    assert old_worker.close_count == 0
    assert new_worker.close_count == 1
    release_old.set()
    old_thread.join(2)
    assert not old_thread.is_alive()
    assert old_worker.close_count == 1
    assert results == {"old": {"marker": 51}, "new": {"marker": 61}}
