"""Fixtures shared across hermes_cli unit tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def s6_layout_chown_spy(tmp_path, monkeypatch):
    """Observe owner intent; layout/FIFO operations stay real, chown does not.

    Unit tests cannot assume the container UID/GID exists in the host's user
    namespace. This does not rewrite that identity or prove real ownership;
    privileged ownership/s6 behavior belongs to container integration tests.
    """
    import os
    from pathlib import Path
    from hermes_cli import service_manager

    requests = []
    root = tmp_path.resolve()
    expected = (service_manager._HERMES_UID, service_manager._HERMES_GID)

    def observe_chown(path, uid, gid):
        target = Path(path)
        assert target.resolve().is_relative_to(root), 'layout chown escaped the owned fixture'
        assert (uid, gid) == expected, 'layout changed the canonical container owner'
        assert target.exists(), 'layout requested ownership before creating its entry'
        requests.append((target, uid, gid))

    monkeypatch.setattr(os, 'chown', observe_chown)
    yield requests
    assert requests, 'layout fixture never observed its ownership boundary'


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_hermes_gate(request, monkeypatch):
    """Default ``_detect_concurrent_hermes_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``hermes.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``hermes`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_hermes_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    # raising=False: under pytest's per-test spawn isolation, a concurrent
    # xdist worker importing a module that transitively touches hermes_cli.main
    # can briefly expose a partially-initialized module object here — one where
    # _detect_concurrent_hermes_instances isn't defined yet. A bare setattr
    # would raise AttributeError and error the (unrelated) test. The attribute
    # always exists once main.py finishes importing, so a no-op when it's
    # transiently absent is the correct, race-free default.
    monkeypatch.setattr(
        _cli_main,
        "_detect_concurrent_hermes_instances",
        lambda *_a, **_k: [],
        raising=False,
    )
