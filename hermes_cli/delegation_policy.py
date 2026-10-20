"""Canonical strict delegation configuration grammar; no execution authority."""
from dataclasses import dataclass
import math
import threading


class DelegationTimeoutConfigError(ValueError):
    """Invalid declared mode/budget must not widen execution permission."""


class DelegationConfigError(ValueError):
    """Unavailable or malformed policy cannot borrow another config owner."""


def validate_child_timeout_mode(value):
    if type(value) is not str or value not in {'total', 'inactivity'}:
        raise DelegationTimeoutConfigError('DELEGATION_INVALID_TIMEOUT_MODE: expected total or inactivity')
    return value


def parse_child_timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise DelegationTimeoutConfigError('DELEGATION_INVALID_TIMEOUT: expected a finite number')
    try:
        parsed = float(value)
    except (ValueError, OverflowError) as exc:
        raise DelegationTimeoutConfigError('DELEGATION_INVALID_TIMEOUT: expected a finite number') from exc
    if not math.isfinite(parsed) or abs(parsed) > threading.TIMEOUT_MAX:
        raise DelegationTimeoutConfigError('DELEGATION_INVALID_TIMEOUT: wait is nonfinite or unrepresentable')
    return None if parsed <= 0 else max(30.0, parsed)


@dataclass(frozen=True)
class DelegationLifetimePolicy:
    mode: str
    timeout_seconds: float | None
