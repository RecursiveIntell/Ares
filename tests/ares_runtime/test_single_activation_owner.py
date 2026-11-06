from __future__ import annotations

import importlib
from importlib.machinery import PathFinder
from pathlib import Path


def test_local_runtime_is_the_only_installed_activation_owner():
    import ares_runtime

    for symbol in ("AresReleaseActivator", "AresRuntimeResolver"):
        assert symbol not in ares_runtime.__all__
        assert not hasattr(ares_runtime, symbol)

    # Resolve against this package's own search path, not the ambient import
    # system: an editable install of another checkout must not mask the result.
    package_root = Path(ares_runtime.__file__).resolve().parent
    for module in ("activation", "resolver"):
        assert PathFinder.find_spec(f"ares_runtime.{module}", list(ares_runtime.__path__)) is None
        assert not (package_root / f"{module}.py").exists()

    local_runtime = importlib.import_module("ares_runtime.local_runtime")
    assert callable(getattr(local_runtime.AresLocalRuntime, "_activate", None))
