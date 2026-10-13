"""Pure selector tuple capture. Caller holds the canonical record history lock.

No filesystem/provider lookup, no transport, no auxiliary source of truth.
"""
import copy


def runtime_selector_values(agent, session, *, host_owned, owns_once,
                            default_model="", default_provider=""):
    record = session or {}
    mirror = record.get("_metadata_mirror") or {}
    pending = record.get("pending_model_switch") or {}
    pending_model = str(pending.get("display_model") or "").strip()
    pending_provider = str(pending.get("display_provider") or "").strip()
    reasoning_config = getattr(agent, "reasoning_config", None)
    if host_owned:
        effort = str(mirror.get("reasoning_effort") or "")
    else:
        if agent is None:
            reasoning_config = record.get("create_reasoning_override")
        effort = ("none" if reasoning_config.get("enabled") is False
                  else str(reasoning_config.get("effort") or "")) if isinstance(reasoning_config, dict) else ""
    if host_owned and "service_tier" in mirror:
        tier = mirror["service_tier"] or ""
    else:
        tier = getattr(agent, "service_tier", None) or mirror.get("service_tier") or ""
        if agent is None and record.get("create_service_tier_override") is not None:
            tier = record["create_service_tier_override"]
    model = pending_model or (getattr(agent, "model", "") if owns_once
                              else mirror.get("model", getattr(agent, "model", "")))
    provider = pending_provider or (getattr(agent, "provider", "") if owns_once
                                    else mirror.get("provider", getattr(agent, "provider", "")))
    override = record.get("model_override")
    if not record.get("_compute_host_active") and not owns_once and isinstance(override, dict) and override.get("model"):
        model = pending_model or override["model"]
        provider = pending_provider or override.get("provider") or provider
    return {
        "model": model or default_model, "provider": provider or default_provider,
        "reasoning_effort": effort, "service_tier": tier, "fast": tier == "priority",
        "pending_runtime_options": copy.deepcopy(mirror.get("pending_runtime_options") if host_owned else pending.get("runtime_options")),
    }, pending_model
