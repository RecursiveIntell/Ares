"""Session-owned admission for framework worker starts.

Permission is read from the real SessionDB owner, never from tool arguments or
an optional observer. This is not a sandbox against arbitrary host Python/SDK
access. Admission does not claim cancellation of an already submitted request.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from agent.orchestration_policy import OrchestrationError, OrchestrationPolicy
from hermes_cli.config import ConfigAdmissionError, load_config_for_admission


def _config_for_owner(db):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(Path(db.db_path).parent)
    try:
        return load_config_for_admission()
    finally:
        reset_hermes_home_override(token)


def validate_managed_policy(policy, snapshot, *, effect=None):
    """Managed denials/route pins cannot be widened by a session preference."""
    config = snapshot.config
    keys = snapshot.managed_keys
    configured = config.get("orchestration", {})
    if type(configured) is not dict:
        raise OrchestrationError("policy_invalid")
    for lane in ("delegation", "graph"):
        field_name = "allow_" + lane
        if "orchestration." + field_name in keys:
            value = configured.get(field_name)
            if type(value) is not bool:
                raise OrchestrationError("policy_invalid")
            if value is False and getattr(policy, field_name) and effect in (None, lane):
                raise OrchestrationError("managed_orchestration_denied")
    route = policy.secondary_route
    if route is not None:
        defaults = config.get("delegation", {})
        if type(defaults) is not dict:
            raise OrchestrationError("policy_invalid")
        for name in ("provider", "model", "reasoning_effort"):
            selected = getattr(route, name)
            if "delegation." + name in keys and selected is not None and selected != defaults.get(name):
                raise OrchestrationError("managed_secondary_route_locked")
        if "delegation.base_url" in keys and defaults.get("base_url"):
            raise OrchestrationError("managed_secondary_endpoint_locked")


@dataclass(frozen=True)
class SpawnAdmission:
    session_id: str
    revision: int
    policy: OrchestrationPolicy
    config_fingerprint: str
    profile_home: str
    _owner: Any = field(repr=False, compare=False)
    _routing_config: dict = field(repr=False, compare=False)
    effect: str = "delegation"
    route_role: str = "optional_worker"

    def __post_init__(self):
        if self.route_role not in {"optional_worker", "internal_auxiliary", "named_specialist"}:
            raise OrchestrationError("orchestration_route_role_invalid")
        if self.effect not in {"delegation", "graph"}:
            raise OrchestrationError("orchestration_effect_unknown")

    @property
    def resolved_optional_route(self):
        if self.route_role != "optional_worker":
            return None
        if self.policy.secondary_route is not None:
            return self.policy.secondary_route
        from agent.orchestration_policy import SecondaryRoute
        cfg = self._routing_config
        if cfg.get("provider") and cfg.get("model"):
            effort = cfg.get("reasoning_effort")
            if effort is False:
                effort = "none"  # existing canonical YAML-disabled semantics
            return SecondaryRoute(cfg["provider"], cfg["model"], effort or None)
        return None

    @property
    def routing_config(self):
        return copy.deepcopy(self._routing_config)

    def assert_config_current(self):
        """Recheck the admitted config identity without revising spawn policy."""
        try:
            cfg = _config_for_owner(self._owner)
        except ConfigAdmissionError as exc:
            raise OrchestrationError(exc.code) from exc
        if cfg.fingerprint != self.config_fingerprint or cfg.profile_home != self.profile_home:
            raise OrchestrationError("orchestration_config_changed")
        return cfg

    def assert_current(self, *, effect=None):
        if effect is not None and effect != self.effect:
            raise OrchestrationError("orchestration_effect_mismatch")
        current = self._owner.get_orchestration_policy(self.session_id, require_qualified_lineage=True)
        if (current["revision"] if current else 0) != self.revision:
            raise OrchestrationError("orchestration_revision_changed")
        if current is not None and OrchestrationPolicy.from_dict(current["policy"]) != self.policy:
            raise OrchestrationError("orchestration_policy_changed")
        try:
            cfg = _config_for_owner(self._owner)
        except ConfigAdmissionError as exc:
            raise OrchestrationError(exc.code) from exc
        validate_managed_policy(self.policy, cfg, effect=self.effect)
        if cfg.fingerprint != self.config_fingerprint or cfg.profile_home != self.profile_home:
            raise OrchestrationError("orchestration_config_changed")

    def to_receipt(self):
        # Deliberately no credential bundle, endpoint headers or raw config.
        return {"session_id": self.session_id, "revision": self.revision,
                "config_fingerprint": self.config_fingerprint,
                "secondary_route": self.policy.secondary_route.to_dict()
                if self.policy.secondary_route else None}


def admit_worker(parent, *, effect="delegation") -> SpawnAdmission:
    """Fail closed before credentials, construction and queue admission."""
    if parent is None:
        raise OrchestrationError("policy_owner_required")
    inherited = getattr(parent, "_orchestration_admission", None)
    if type(inherited) is SpawnAdmission:
        if inherited.effect != effect:
            inherited = replace(inherited, effect=effect)
        inherited.assert_current()
        if effect == "delegation" and not inherited.policy.allow_delegation:
            raise OrchestrationError("delegation_disabled")
        if effect == "graph" and not inherited.policy.allow_graph:
            raise OrchestrationError("graph_disabled")
        if effect not in {"delegation", "graph"}:
            raise OrchestrationError("orchestration_effect_unknown")
        return inherited
    db = getattr(parent, "_session_db", None)
    from hermes_state import SessionDB
    if not isinstance(db, SessionDB) and isinstance(getattr(db, "_db", None), SessionDB):
        db = db._db
    if not isinstance(db, SessionDB):
        raise OrchestrationError("policy_store_unavailable")
    ensure = getattr(type(parent), "_ensure_db_session", None)
    if callable(ensure):
        ensure(parent)
    session_id = getattr(parent, "session_id", None)
    if type(session_id) is not str or not session_id:
        raise OrchestrationError("policy_owner_required")
    try:
        cfg = _config_for_owner(db)
    except ConfigAdmissionError as exc:
        raise OrchestrationError(exc.code) from exc
    if str(Path(db.db_path).parent.resolve()) != cfg.profile_home:
        raise OrchestrationError("policy_profile_mismatch")
    record = db.get_orchestration_policy(session_id, require_qualified_lineage=True)
    config = cfg.config
    if record is None:
        raw = config.get("orchestration", OrchestrationPolicy().to_dict())
        policy = OrchestrationPolicy.from_dict(raw)
        revision = 0
    else:
        policy = OrchestrationPolicy.from_dict(record["policy"])
        revision = record["revision"]
    validate_managed_policy(policy, cfg, effect=effect)
    if effect == "delegation":
        if not policy.allow_delegation:
            raise OrchestrationError("delegation_disabled")
    elif effect == "graph":
        if not policy.allow_graph:
            raise OrchestrationError("graph_disabled")
    else:
        raise OrchestrationError("orchestration_effect_unknown")
    route_config = config.get("delegation", {})
    if type(route_config) is not dict:
        raise OrchestrationError("policy_invalid")
    route_config = copy.deepcopy(route_config)
    if policy.secondary_route is not None:
        route = policy.secondary_route
        # Tier support must be qualified by the target transport; a stored
        # provider-neutral tier word is not permission to silently inherit one.
        if route.service_tier is not None:
            raise OrchestrationError("secondary_service_tier_unqualified")
        route_config.update({"provider": route.provider, "model": route.model})
        if route.reasoning_effort is not None:
            route_config["reasoning_effort"] = route.reasoning_effort
        # An inherited direct endpoint must never override the selected provider.
        route_config.update({"base_url": "", "api_key": "", "api_mode": ""})
    admission = SpawnAdmission(session_id, revision, policy, cfg.fingerprint,
                               cfg.profile_home, db, route_config, effect)
    admission.assert_current()
    return admission


def assert_child_dispatch(child):
    """Revalidate queued/worker execution without trusting a tool payload."""
    admission = getattr(child, "_orchestration_admission", None)
    if type(admission) is not SpawnAdmission:
        raise OrchestrationError("policy_owner_required")
    admission.assert_current(effect="delegation")
    if not admission.policy.allow_delegation:
        raise OrchestrationError("delegation_disabled")
    route = admission.resolved_optional_route
    if route is not None:
        if getattr(child, "model", None) != route.model or getattr(child, "provider", None) != route.provider:
            raise OrchestrationError("secondary_route_not_observed")
        if route.reasoning_effort is not None:
            from hermes_constants import parse_reasoning_effort
            if getattr(child, "reasoning_config", None) != parse_reasoning_effort(route.reasoning_effort):
                raise OrchestrationError("secondary_reasoning_not_observed")
    return admission


def physical_worker_binding(agent):
    """Return only host-created binding, never one decoded from model payloads."""
    if agent is None:
        return None
    admission = getattr(agent, "_orchestration_admission", None)
    depth = getattr(agent, "_delegate_depth", 0)
    if admission is None and not (type(depth) is int and depth > 0):
        return None  # genuine root: own foreground model is not a worker
    admission = assert_child_dispatch(agent)
    if admission.resolved_optional_route is not None and getattr(agent, "api_mode", None) not in {
        "chat_completions", "codex_responses", "anthropic_messages", "bedrock_converse",
    }:
        raise OrchestrationError("secondary_transport_unqualified")
    return (agent, admission)


def admit_physical_worker_send(binding, body):
    """Recheck each actual prepared request, including SDK retries/lazy streams.

    Records dispatch admission, not successful transmission or completion. The
    commit is ordered with opt-outs; requests admitted earlier may be in flight.
    """
    import hashlib
    import json
    if binding is None:
        return None
    child, admission = binding
    if getattr(child, "_orchestration_admission", None) is not admission:
        raise OrchestrationError("policy_owner_mismatch")
    assert_child_dispatch(child)
    if type(body) is not bytes:
        raise OrchestrationError("secondary_transport_unqualified")
    try:
        payload = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise OrchestrationError("secondary_transport_unqualified") from exc
    if type(payload) is not dict:
        raise OrchestrationError("secondary_transport_unqualified")
    # Optional worker pins must be demonstrable on the actual provider body.
    # Internal maintenance retains its independently owned route.
    route = admission.resolved_optional_route
    model = payload.get("model")
    if route is not None:
        if model != route.model:
            raise OrchestrationError("secondary_model_not_observed")
        if route.reasoning_effort is not None:
            effort = payload.get("reasoning_effort")
            for key in ("reasoning", "output_config"):
                value = payload.get(key)
                if type(value) is dict and "effort" in value:
                    effort = value["effort"]
            # No inference from token budgets, clamped labels, or defaults.
            if effort != route.reasoning_effort:
                raise OrchestrationError("secondary_reasoning_not_observed")
    observed = {"provider": getattr(child, "provider", None), "model": model,
                "request_sha256": hashlib.sha256(body).hexdigest()}
    if route is not None:
        observed["reasoning_effort"] = route.reasoning_effort
    return admission._owner.claim_orchestration_effect(admission.session_id,
        expected_revision=admission.revision, effect=admission.effect,
        route=observed, config_fingerprint=admission.config_fingerprint)


def denial_result(exc: OrchestrationError) -> str:
    import json
    return json.dumps({"error": str(exc), "code": exc.code,
                       "status": "blocked", "provider_calls": 0})
