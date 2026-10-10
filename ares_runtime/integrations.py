"""Typed registration of the default Ares integration services.

The bootstrap installer (``install.sh``) downloads and installs the default
service set — MCP servers, the Recursive Agent plugin payload, and the skills
and hooks packs — and hands structured edits of the Ares home configuration to
this module as a JSON registration plan.

Ownership boundary: the shell installer performs file, network, and systemd
operations, but never hand-edits or string-coerces structured configuration.
The merge here is typed YAML, idempotent, and non-clobbering:

* an entry absent from the config is registered exactly as planned;
* an entry identical to the plan is reported and left untouched;
* an entry that differs from the plan is preserved and reported, never
  overwritten — a re-run of the installer must not erase operator changes.

Usage (invoked by ``install.sh``)::

    python -m ares_runtime.integrations register-mcp \
        --home "$HERMES_HOME" --plan /path/to/mcp-plan.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml

try:  # Canonical config chokepoint: fail-closed guard + shared atomic writer
    # (symlink, owner, mode, fsync, formatting guarantees).
    from hermes_cli.config import atomic_config_write as _shared_config_write
except Exception:  # pragma: no cover - standalone fallback outside the source tree
    _shared_config_write = None

try:  # Shared writer alone, when the full config module is unavailable.
    from utils import atomic_yaml_write as _shared_atomic_yaml_write
except Exception:  # pragma: no cover - standalone fallback outside the source tree
    _shared_atomic_yaml_write = None


class AresIntegrationsError(RuntimeError):
    """Typed failure for integration registration."""


_SERVER_NAME = re.compile(r"^[a-z0-9_]+$")
_ALLOWED_PLAN_KEYS = {"mcp_servers", "disable_builtin_memory"}


def _require_abs_path(value: object, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise AresIntegrationsError(f"{what} must be a non-empty string")
    if not os.path.isabs(value):
        raise AresIntegrationsError(f"{what} must be an absolute path: {value!r}")
    return value


def _require_str_list(value: object, what: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise AresIntegrationsError(f"{what} must be a list of non-empty strings")
    return list(value)


def load_plan(plan_path: Path) -> dict:
    """Load and validate a registration plan written by the installer."""

    try:
        raw = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise AresIntegrationsError(f"cannot read plan file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AresIntegrationsError(f"plan file is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise AresIntegrationsError("plan root must be a JSON object")
    unknown = sorted(set(raw) - _ALLOWED_PLAN_KEYS)
    if unknown:
        raise AresIntegrationsError(f"plan has unknown keys: {', '.join(unknown)}")

    plan: dict = {}
    servers = raw.get("mcp_servers", {})
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise AresIntegrationsError("plan 'mcp_servers' must be an object")
    validated: dict[str, dict] = {}
    for name, entry in servers.items():
        if not isinstance(name, str) or not _SERVER_NAME.match(name):
            raise AresIntegrationsError(f"invalid MCP server name: {name!r}")
        if not isinstance(entry, dict):
            raise AresIntegrationsError(f"server {name!r} must be an object")
        extra = sorted(set(entry) - {"command", "args"})
        if extra:
            raise AresIntegrationsError(
                f"server {name!r} has unknown fields: {', '.join(extra)}"
            )
        validated[name] = {
            "command": _require_abs_path(entry.get("command"), f"{name}.command"),
            "args": _require_str_list(entry.get("args", []), f"{name}.args"),
        }
    plan["mcp_servers"] = validated

    disable = raw.get("disable_builtin_memory", False)
    if not isinstance(disable, bool):
        raise AresIntegrationsError("plan 'disable_builtin_memory' must be a boolean")
    plan["disable_builtin_memory"] = disable
    return plan


def _load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AresIntegrationsError(f"existing config is not valid YAML: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise AresIntegrationsError("existing config root is not a mapping")
    return loaded


def _atomic_write_yaml(path: Path, config: dict) -> None:
    """Write the config through the canonical Hermes config chokepoint.

    ``hermes_cli.config.atomic_config_write`` runs the fail-closed readable-
    config guard and then the shared atomic writer, which preserves symlinked
    ``config.yaml`` files (swap in-place on the real file), ownership, mode,
    and fsync durability. Fallbacks for standalone use outside the source tree
    keep a symlinked config a symlink by writing through its resolved target.
    """

    if _shared_config_write is not None:
        _shared_config_write(path, config, sort_keys=True, create_mode=0o600)
        return

    if _shared_atomic_yaml_write is not None:
        _shared_atomic_yaml_write(path, config, sort_keys=True, create_mode=0o600)
        return

    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = None
    if target.exists():
        mode = os.stat(target).st_mode & 0o777
    tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(
            yaml.safe_dump(config, default_flow_style=False, sort_keys=True),
            encoding="utf-8",
        )
        os.chmod(tmp, mode if mode is not None else 0o600)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def apply_plan(home: Path, plan: dict) -> list[str]:
    """Merge a validated plan into ``<home>/config.yaml``.

    Returns human-readable report lines. Raises :class:`AresIntegrationsError`
    without touching the config file when the existing file cannot be safely
    merged (invalid YAML, non-mapping sections, non-list ``disabled_toolsets``).
    """

    home = Path(home).expanduser()
    config_path = home / "config.yaml"
    config = _load_config(config_path)
    report: list[str] = []
    changed = False

    servers = plan.get("mcp_servers") or {}
    if servers:
        section = config.setdefault("mcp_servers", {})
        if not isinstance(section, dict):
            raise AresIntegrationsError("config 'mcp_servers' is not a mapping")
        for name in sorted(servers):
            entry = servers[name]
            desired: dict = {"command": entry["command"], "enabled": True}
            if entry["args"]:
                desired["args"] = list(entry["args"])
            existing = section.get(name)
            if existing is None:
                section[name] = desired
                changed = True
                report.append(f"registered mcp server: {name}")
            elif existing == desired:
                report.append(f"already registered: {name}")
            else:
                report.append(f"kept existing (differs from plan): {name}")

    if plan.get("disable_builtin_memory"):
        # Keep the working built-in memory tools when the replacement entry is
        # missing or explicitly disabled: a preserved operator customization
        # must not leave the home with both memory systems switched off.
        server_entry = (config.get("mcp_servers") or {}).get("semantic_memory")
        replacement_enabled = (
            isinstance(server_entry, dict) and server_entry.get("enabled") is not False
        )
        if not replacement_enabled:
            report.append(
                "built-in memory toolset kept enabled (semantic-memory entry is missing or disabled)"
            )
        else:
            agent = config.setdefault("agent", {})
            if not isinstance(agent, dict):
                raise AresIntegrationsError("config 'agent' section is not a mapping")
            disabled = agent.setdefault("disabled_toolsets", [])
            if not isinstance(disabled, list):
                raise AresIntegrationsError("config 'agent.disabled_toolsets' is not a list")
            if "memory" not in disabled:
                disabled.append("memory")
                changed = True
                report.append("disabled built-in memory toolset")
            else:
                report.append("built-in memory toolset already disabled")

    if changed:
        _atomic_write_yaml(config_path, config)
        report.append(f"updated: {config_path}")
    else:
        report.append("no configuration changes required")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ares_runtime.integrations",
        description="Register the default Ares integration services (typed config merge).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    register = sub.add_parser(
        "register-mcp",
        help="Merge a bootstrap-installer registration plan into the Ares home config.",
    )
    register.add_argument(
        "--home",
        type=Path,
        required=True,
        help="Ares agent home (the directory containing config.yaml).",
    )
    register.add_argument(
        "--plan",
        type=Path,
        required=True,
        help="JSON registration plan written by install.sh.",
    )
    args = parser.parse_args(argv)

    try:
        plan = load_plan(args.plan)
        for line in apply_plan(args.home, plan):
            print(line)
    except AresIntegrationsError as exc:
        print(f"ares integrations error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
