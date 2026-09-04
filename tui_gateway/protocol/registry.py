from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

PROTOCOL_REVISION = 2
_PROTOCOL_RUNTIME_ID = uuid.uuid4().hex

_CAPABILITY_NAMES = (
    "session_observe_v1",
    "session_snapshot_v1",
    "event_cursor_v1",
    "bounded_replay_v1",
    "session_control_lease_v1",
    "write_idempotency_v1",
    "mobile_surface_v1",
)
_SCHEMA_DIR = Path(__file__).with_name("schemas")


def _schema_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted(_SCHEMA_DIR.glob("*.json")):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def protocol_runtime_id() -> str:
    """Return the current gateway-process generation identity."""
    return _PROTOCOL_RUNTIME_ID


def host_identity_digest() -> str:
    """Return the server-owned configured host digest, or explicit unbound state."""
    try:
        import yaml

        config_path = Path(get_hermes_home()) / "config.yaml"
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        value = ((raw.get("mobile") or {}).get("host_identity_digest"))
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
            return value
    except Exception:
        pass
    return "unbound"


def protocol_manifest() -> dict[str, Any]:
    """Return the stable, server-owned protocol manifest projection."""
    return {
        "protocol_revision": PROTOCOL_REVISION,
        "schema_digest": _schema_digest(),
        "capabilities": {name: False for name in _CAPABILITY_NAMES},
    }


def build_gateway_ready_payload(
    *,
    host_identity_digest: str,
    runtime_id: str,
    capabilities: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Build an explicit capability advertisement without widening defaults."""
    manifest = protocol_manifest()
    advertised = dict(manifest["capabilities"])
    if capabilities:
        for name, value in capabilities.items():
            if name in advertised and isinstance(value, bool):
                advertised[name] = value
    return {
        "protocol_revision": PROTOCOL_REVISION,
        "schema_digest": manifest["schema_digest"],
        "host_identity_digest": str(host_identity_digest),
        "runtime_id": str(runtime_id),
        "capabilities": advertised,
    }


def canonical_schema_bytes() -> bytes:
    """Return a deterministic manifest projection for receipt tooling."""
    return json.dumps(protocol_manifest(), sort_keys=True, separators=(",", ":")).encode("utf-8")
