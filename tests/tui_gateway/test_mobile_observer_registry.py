from __future__ import annotations

from tui_gateway.observers import ObserverRegistry


class _Transport:
    def __init__(self, *, accepts: bool = True) -> None:
        self.accepts = accepts
        self.frames: list[dict] = []

    def write(self, frame: dict) -> bool:
        self.frames.append(dict(frame))
        return self.accepts


def test_observer_registry_never_fans_out_to_primary_transport() -> None:
    registry = ObserverRegistry(max_observers_per_session=2)
    primary = _Transport()
    observer = _Transport()
    registry.register(
        session_id="session", profile="default", runtime_id="runtime", transport=primary
    )
    registry.register(
        session_id="session", profile="default", runtime_id="runtime", transport=observer
    )

    assert registry.fanout(
        session_id="session",
        profile="default",
        runtime_id="runtime",
        frame={"event": "message.delta"},
        primary_transport=primary,
    ) == 1
    assert primary.frames == []
    assert observer.frames == [{"event": "message.delta"}]


def test_observer_registry_is_bounded_and_removes_stale_transports() -> None:
    registry = ObserverRegistry(max_observers_per_session=1)
    stale = _Transport(accepts=False)
    registry.register(
        session_id="session", profile="default", runtime_id="runtime", transport=stale
    )
    assert registry.fanout(
        session_id="session", profile="default", runtime_id="runtime", frame={"event": "x"}
    ) == 0
    assert registry.observers_for(session_id="session", profile="default", runtime_id="runtime") == []


def test_observer_registry_scope_is_exact() -> None:
    registry = ObserverRegistry()
    observer = _Transport()
    registry.register(
        session_id="same", profile="default", runtime_id="runtime-a", transport=observer
    )
    assert registry.fanout(
        session_id="same", profile="other", runtime_id="runtime-a", frame={"event": "x"}
    ) == 0
    assert registry.fanout(
        session_id="same", profile="default", runtime_id="runtime-b", frame={"event": "x"}
    ) == 0
    assert observer.frames == []
