"""Server-owned protocol contracts for Ares cross-client surfaces.

This package contains only the admitted structural/semantic foundation. Runtime
capabilities remain disabled until their owning phase has passed its gate.
"""

from .registry import (
    PROTOCOL_REVISION,
    build_gateway_ready_payload,
    host_identity_digest,
    protocol_manifest,
    protocol_runtime_id,
)
from .semantic import (
    ProtocolValidationError,
    validate_event_envelope,
    validate_session_snapshot,
)

__all__ = [
    "PROTOCOL_REVISION",
    "ProtocolValidationError",
    "build_gateway_ready_payload",
    "protocol_manifest",
    "validate_event_envelope",
    "validate_session_snapshot",
]
