"""Context engine plugin discovery.

Scans ``plugins/context_engine/<name>/`` directories for context engine
plugins.  Each subdirectory must contain ``__init__.py`` with a class
implementing the ContextEngine ABC.

Context engines are separate from the general plugin system — they live
in the repo and are always available without user installation.  Only ONE
can be active at a time, selected via ``context.engine`` in config.yaml.
The default engine is ``"compressor"`` (the built-in ContextCompressor).

Usage:
    from plugins.context_engine import discover_context_engines, load_context_engine

    available = discover_context_engines()   # [(name, desc, available), ...]
    engine = load_context_engine("lcm")      # ContextEngine instance
"""

from __future__ import annotations

import importlib
import logging
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_CONTEXT_ENGINE_PLUGINS_DIR = Path(__file__).parent


class ContextEngineActivationError(RuntimeError):
    """A specifically configured engine could not become the active engine.

    Discovery remains best-effort for UI listing, but selection must be strict:
    silently substituting the built-in compressor changes provenance semantics.
    """

    def __init__(self, message: str, *, stage: str | None = None,
                 diagnostic_id: str | None = None):
        super().__init__(message)
        self.stage = stage
        self.diagnostic_id = diagnostic_id


def _activation_failure(name: str, stage: str, error: Exception) -> ContextEngineActivationError:
    """Keep the exception chain local; never copy arbitrary failure text to UI."""
    diagnostic_id = uuid.uuid4().hex
    message = (f"configured context engine '{name}' failed its {stage} "
               f"({type(error).__name__}; diagnostic {diagnostic_id})")
    logger.warning("%s", message)
    return ContextEngineActivationError(message, stage=stage, diagnostic_id=diagnostic_id)


def discover_context_engines() -> List[Tuple[str, str, bool]]:
    """Scan plugins/context_engine/ for available engines.

    Returns list of (name, description, is_available) tuples.
    Reads plugin.yaml and best-effort loads each engine for availability.
    """
    results = []
    if not _CONTEXT_ENGINE_PLUGINS_DIR.is_dir():
        return results

    for child in sorted(_CONTEXT_ENGINE_PLUGINS_DIR.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".")):
            continue
        init_file = child / "__init__.py"
        if not init_file.exists():
            continue

        # Read description from plugin.yaml if available
        desc = ""
        yaml_file = child / "plugin.yaml"
        if yaml_file.exists():
            try:
                import yaml

                with open(yaml_file, encoding="utf-8-sig") as f:
                    meta = yaml.safe_load(f) or {}
                desc = meta.get("description", "")
            except Exception:
                pass

        # Quick availability check — try loading and calling is_available()
        available = True
        try:
            engine = _load_engine_from_dir(child)
            if engine is None:
                available = False
            elif hasattr(engine, "is_available"):
                available = engine.is_available()
        except Exception:
            available = False

        results.append((child.name, desc, available))

    return results


def load_context_engine(name: str) -> Optional["ContextEngine"]:
    """Load and return a ContextEngine instance by name.

    Returns None if the engine is not found or fails to load.
    """
    engine_dir = _CONTEXT_ENGINE_PLUGINS_DIR / name
    if not engine_dir.is_dir():
        logger.debug(
            "Context engine '%s' not found in %s", name, _CONTEXT_ENGINE_PLUGINS_DIR
        )
        return None
    try:
        engine = _load_engine_from_dir(engine_dir)
        if engine:
            return engine
        logger.warning("Context engine '%s' loaded but no engine instance found", name)
        return None
    except Exception as e:
        logger.warning("Failed to load context engine '%s': %s", name, e)
        return None


def load_context_engine_strict(name: str) -> "ContextEngine":
    """Load, instantiate, and capability-probe an explicitly configured engine.

    This intentionally differs from :func:`load_context_engine`, which is
    retained for non-authoritative discovery screens. Call this only when
    `context.engine` names a non-built-in engine.
    """
    engine_dir = _CONTEXT_ENGINE_PLUGINS_DIR / name
    if not engine_dir.is_dir():
        raise ContextEngineActivationError(
            f"configured context engine '{name}' was not discovered"
        )
    engine = _load_engine_from_dir(engine_dir)
    if engine is None:
        raise ContextEngineActivationError(
            f"configured context engine '{name}' could not be instantiated"
        )
    try:
        probe = getattr(engine, "probe_activation", None)
        if callable(probe):
            probe()
        elif hasattr(engine, "is_available") and not engine.is_available():
            raise RuntimeError("availability check returned false")
    except Exception as exc:
        raise _activation_failure(name, "activation probe", exc) from exc
    return engine


def _load_engine_from_dir(engine_dir: Path) -> Optional["ContextEngine"]:
    """Import an engine module and extract the ContextEngine instance.

    The module must have either:
    - A register(ctx) function (plugin-style) — we simulate a ctx
    - A top-level class that extends ContextEngine — we instantiate it
    """
    name = engine_dir.name
    module_name = f"plugins.context_engine.{name}"
    init_file = engine_dir / "__init__.py"

    if not init_file.exists():
        return None

    # The standard importer owns its per-module lock, relative submodules and
    # failure cleanup. Publishing a hand-built module in sys.modules before
    # exec_module let concurrent session builds mistake partial import for
    # completion; eagerly loading submodules also left failed partial objects.
    # Cache the module, never a mutable per-session engine instance.
    try:
        mod = importlib.import_module(module_name)
    except Exception as exc:
        raise _activation_failure(name, "import", exc) from exc

    # Try register(ctx) pattern first (how plugins are written)
    if hasattr(mod, "register"):
        collector = _EngineCollector(engine_name=name)
        try:
            mod.register(collector)
            if collector.engine:
                return collector.engine
        except Exception as exc:
            # A failed explicit registration is not permission to instantiate
            # an imported base class or replay a possibly effectful constructor.
            raise _activation_failure(name, "registration", exc) from exc

    # Fallback: find a ContextEngine subclass and instantiate it
    from agent.context_engine import ContextEngine

    for attr_name in dir(mod):
        attr = getattr(mod, attr_name, None)
        if (
            isinstance(attr, type)
            and issubclass(attr, ContextEngine)
            and attr is not ContextEngine
        ):
            try:
                return attr()
            except Exception as exc:
                raise _activation_failure(name, "construction", exc) from exc

    return None


class _EngineCollector:
    """Fake plugin context that captures register_context_engine calls.

    Plugin context engines using the standard ``register(ctx)`` pattern may
    also call ``ctx.register_command(...)`` to expose slash commands (e.g.
    ``/lcm``). Forward those to the global plugin command registry so they
    behave identically to commands registered by normal plugins.
    """

    def __init__(self, engine_name: str = ""):
        self.engine = None
        self._engine_name = engine_name or "context_engine"
        self._registered_commands: list[str] = []

    def register_context_engine(self, engine):
        self.engine = engine

    def register_command(
        self,
        name: str,
        handler,
        description: str = "",
        args_hint: str = "",
    ) -> None:
        """Forward to the global plugin command registry."""
        clean = (name or "").lower().strip().lstrip("/").replace(" ", "-")
        if not clean:
            logger.warning(
                "Context engine '%s' tried to register a command with an empty name.",
                self._engine_name,
            )
            return

        # Reject conflicts with built-in commands.
        try:
            from hermes_cli.commands import resolve_command

            if resolve_command(clean) is not None:
                logger.warning(
                    "Context engine '%s' tried to register command '/%s' which conflicts "
                    "with a built-in command. Skipping.",
                    self._engine_name,
                    clean,
                )
                return
        except Exception:
            pass

        try:
            from hermes_cli.plugins import get_plugin_manager

            manager = get_plugin_manager()
            if clean in manager._plugin_commands:
                # Don't clobber a regular plugin's command — same conflict
                # policy the plugin system uses for plugin-vs-plugin collisions.
                logger.warning(
                    "Context engine '%s' tried to register command '/%s' which "
                    "is already registered by a plugin. Skipping.",
                    self._engine_name,
                    clean,
                )
                return
            manager._plugin_commands[clean] = {
                "handler": handler,
                "description": description or "Context engine command",
                "plugin": f"context-engine:{self._engine_name}",
                "args_hint": (args_hint or "").strip(),
            }
            self._registered_commands.append(clean)
            logger.debug(
                "Context engine '%s' registered command: /%s",
                self._engine_name,
                clean,
            )
        except Exception as exc:
            logger.debug(
                "Context engine '%s' could not register /%s: %s",
                self._engine_name,
                clean,
                exc,
            )

    # No-op for other registration methods
    def register_tool(self, *args, **kwargs):
        pass

    def register_hook(self, *args, **kwargs):
        pass

    def register_cli_command(self, *args, **kwargs):
        pass

    def register_memory_provider(self, *args, **kwargs):
        pass
