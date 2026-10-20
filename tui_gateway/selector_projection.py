"""Rebuildable selector projections; the session owner issues their ordering.

This is not history ordering or wall time. One record owns one epoch. A snapshot
is stamped before expensive enrichment; publication must still match the latest
owner projection. Child stamps are checked before a serving mirror is updated.
"""
from __future__ import annotations

import copy
import threading
import uuid

SELECTOR_KEYS = ("model", "provider", "reasoning_effort", "service_tier", "fast", "pending_runtime_options")


def selector_lock(session: dict):
    # The GIL-bound setdefault publishes a single lock; separate from the plain
    # history lock because several callers already hold that non-reentrant lock.
    return session.setdefault("_selector_projection_lock", threading.RLock())


def selector_values(info: dict) -> dict:
    return {key: copy.deepcopy(info.get(key)) for key in SELECTOR_KEYS}


def stamp_selector_snapshot(session: dict, info: dict) -> dict:
    with selector_lock(session):
        values = selector_values(info)
        prior = session.get("_selector_projection")
        if prior is None or prior["values"] != values:
            prior = {
                "epoch": prior["epoch"] if prior else uuid.uuid4().hex,
                "revision": prior["revision"] + 1 if prior else 1,
                "values": values,
            }
            session["_selector_projection"] = prior
        return {"epoch": prior["epoch"], "revision": prior["revision"]}


def valid_selector_stamp(stamp) -> bool:
    return (type(stamp) is dict and type(stamp.get("epoch")) is str
            and bool(stamp["epoch"]) and type(stamp.get("revision")) is int
            and 0 < stamp["revision"] <= 9007199254740991)


def selector_snapshot_is_current(session: dict, info: dict) -> bool:
    stamp = info.get("selector_version")
    if stamp is None:
        return True  # Only legacy/synthetic producers; never mint an order for them.
    prior = session.get("_selector_projection")
    return (valid_selector_stamp(stamp) and prior is not None
            and stamp["epoch"] == prior["epoch"] and stamp["revision"] == prior["revision"]
            and selector_values(info) == prior["values"])


def admit_host_selector_snapshot(session: dict, info: dict, boot: str | None,
                                 current_boot: str | None) -> bool:
    stamp = info.get("selector_version")
    prior = session.get("_host_selector_projection")
    if boot and current_boot and boot != current_boot:
        return False
    if stamp is None:
        return prior is None  # Once ordered, a legacy frame cannot downgrade it.
    if not valid_selector_stamp(stamp):
        return False
    if prior:
        if prior["boot"] == boot:
            if prior["epoch"] != stamp["epoch"] or stamp["revision"] < prior["revision"]:
                return False
            if stamp["revision"] == prior["revision"] and selector_values(info) != prior["values"]:
                return False
        elif not boot or boot != current_boot:
            return False
    session["_host_selector_projection"] = {**stamp, "boot": boot, "values": selector_values(info)}
    return True
