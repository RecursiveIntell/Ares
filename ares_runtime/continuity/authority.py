"""Typed adapter checks for the native context authority protocol.

Native policy owns grant, material identity and canonical signing bytes.
SessionDB owns the local operation admitted for signing. These checks never
enroll a native grant or turn an observed receipt into an execution permit.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import os
import re

from .credentials import ControllerCredentialError


def refuse(code="CONTEXT_AUTHORITY_RECORD_INVALID"):
    raise ControllerCredentialError(code)


def digest(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        refuse()
    return value


def closed(value, fields):
    if type(value) is not dict or set(value) != set(fields.split()):
        refuse()
    return value


def exact(left, right):
    """JSON equality without Python's bool/int/float coercion."""
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return left.keys() == right.keys() and all(exact(left[k], right[k]) for k in left)
    if type(left) is list:
        return len(left) == len(right) and all(exact(a, b) for a, b in zip(left, right))
    return left == right


def head(value):
    closed(value, "context generation mode")
    if (type(value["context"]) is not str or not value["context"] or len(value["context"]) > 192
            or type(value["generation"]) is not int or not 1 <= value["generation"] < 2**63
            or value["mode"] not in {"active", "sealed"}):
        refuse()
    return value


def binding(value):
    closed(value, "incarnation scope grant_digest policy_digest head")
    for key in ("incarnation", "grant_digest", "policy_digest"):
        digest(value[key])
    scope = closed(value["scope"], "store profile root")
    digest(scope["store"])
    for key in ("profile", "root"):
        if type(scope[key]) is not str or not scope[key] or len(scope[key]) > 192:
            refuse()
    head(value["head"])
    return value


def transport(value):
    closed(value, "socket_path timeout_seconds")
    if (type(value["socket_path"]) is not str or not os.path.isabs(value["socket_path"])
            or type(value["timeout_seconds"]) not in (int, float)
            or not math.isfinite(value["timeout_seconds"]) or not 0 < value["timeout_seconds"] <= 30):
        refuse("CONTEXT_AUTHORITY_TRANSPORT_INVALID")
    return deepcopy(value)


def snapshot(value, *, scope, public_key, incarnation, expected=None):
    closed(value, "authority grant approval_verifier configuration_revision enrolled effects_charged transitions_charged consumed")
    actual = binding(value["authority"])
    grant = closed(value["grant"], "scope controller_public_key actor policy_version policy_digest write_root initial_head not_before expires_at max_effects max_transitions previous_grant")
    digest(value["approval_verifier"])
    if (value["enrolled"] is not True or actual["scope"] != scope or actual["incarnation"] != incarnation
            or grant["scope"] != scope or grant["controller_public_key"] != public_key
            or grant["policy_digest"] != actual["policy_digest"]
            or type(grant["write_root"]) is not str or not os.path.isabs(grant["write_root"])
            or expected is not None and actual != expected):
        refuse("CONTEXT_AUTHORITY_ENROLLMENT_MISMATCH")
    for key in ("configuration_revision", "effects_charged", "transitions_charged"):
        if type(value[key]) is not int or not 0 <= value[key] < 2**63:
            refuse()
    if type(value["consumed"]) is not list or len(value["consumed"]) > 1024:
        refuse()
    for item in value["consumed"]:
        closed(item, "permit_id preflight_receipt_digest outcome_receipt_digest reported_state")
        if type(item["permit_id"]) is not str or not item["permit_id"]:
            refuse()
        digest(item["preflight_receipt_digest"])
        if item["outcome_receipt_digest"] is not None:
            digest(item["outcome_receipt_digest"])
        if item["reported_state"] not in {None, "succeeded", "failed", "outcome_ambiguous"}:
            refuse()
    return deepcopy(value)


def signing_material(prepared, *, authority, action=None):
    """Decode native canonical bytes and compare the entire admitted message.

    Python does not recreate JCS or mint native material IDs. The native
    verifier reconstructs canonical bytes again when admitting the signature.
    """
    binding(authority)
    closed(prepared, "material signing_bytes")
    raw = prepared["signing_bytes"]
    if (type(raw) is not list or not 0 < len(raw) <= 16384
            or any(type(item) is not int or not 0 <= item < 256 for item in raw)):
        refuse("CONTEXT_AUTHORITY_SIGNING_MATERIAL_INVALID")
    raw = bytes(raw)
    material = prepared["material"]
    if action is None:
        closed(material, "authority witness_digest signature")
        digest(material["witness_digest"])
        expected = ["recursive-agent.context-call/v1", authority, material["witness_digest"]]
    else:
        closed(material, "authority transition action signature")
        digest(material["transition"])
        if not exact(material["action"], action):
            refuse("CONTEXT_AUTHORITY_SIGNING_MATERIAL_INVALID")
        expected = ["recursive-agent.context-transition/v1", authority, material["transition"], action]
    if not exact(material["authority"], authority) or material["signature"] != []:
        refuse("CONTEXT_AUTHORITY_SIGNING_MATERIAL_INVALID")

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                refuse("CONTEXT_AUTHORITY_SIGNING_MATERIAL_INVALID")
            result[key] = item
        return result

    try:
        decoded = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: refuse())
    except (ValueError, RecursionError):
        refuse("CONTEXT_AUTHORITY_SIGNING_MATERIAL_INVALID")
    if not exact(decoded, expected):
        refuse("CONTEXT_AUTHORITY_SIGNING_MATERIAL_INVALID")
    return deepcopy(material), raw
