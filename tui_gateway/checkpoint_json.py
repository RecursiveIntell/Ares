"""Strict raw basis frames without changing unrelated legacy JSON semantics."""
import json
import math


BASIS_ROUTE = "session.run_checkpoint.basis"


def load_checkpoint_frame(raw):
    """Reject ambiguous basis selectors before duplicate keys are discarded.

    Track every selector occurrence in the root or host rpc.message envelope,
    including overwritten/escaped selectors. Strings in unrelated payload data
    are not routes. Legacy frames retain last-key and nonfinite behavior.
    """
    objects = {}
    invalid = False

    def pairs(items):
        nonlocal invalid
        value = {}
        for key, item in items:
            if key in value:
                invalid = True
            value[key] = item
        # Retain original pairs, so overwritten selectors/envelopes cannot hide.
        objects[id(value)] = (value, items)
        return value

    def constant(value):
        nonlocal invalid
        invalid = True
        return float(value)

    def floating(value):
        nonlocal invalid
        result = float(value)
        if not math.isfinite(result):
            invalid = True
        return result

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=floating)
    def selects_basis(candidate):
        record = objects.get(id(candidate))
        return bool(record and any(key in {"method", "route_name"} and item == BASIS_ROUTE
            for key, item in record[1]))

    basis = selects_basis(value)
    if type(value) is dict:
        root_pairs = objects[id(value)][1]
        if any(key == "type" and item == "rpc" for key, item in root_pairs):
            basis = basis or any(key == "message" and selects_basis(item) for key, item in root_pairs)
    if basis and (invalid or len(raw) > 65536):
        # No source excerpt: callers may log JSONDecodeError's string form.
        raise json.JSONDecodeError("invalid checkpoint basis frame", "", 0)
    return value
