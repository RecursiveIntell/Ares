"""Operator-owned per-session controls; no shared config or main-route writes.

The command/transport ingress authenticates the operator. An origin string in
this storage call is audit metadata, not an authentication mechanism. Arbitrary
host shell/SQLite access is outside this framework enforcement boundary.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import shlex

from agent.orchestration_policy import OrchestrationError, OrchestrationPolicy, SecondaryRoute
from agent.orchestration_runtime import _config_for_owner, validate_managed_policy

_ACTIONS = ("show", "off", "direct", "prefer", "no-delegation", "no-graphs", "secondary", "inherit-secondary")


def operator_control_text(message):
    """Narrow full-message grammar, only for authenticated CURRENT user ingress.

    Never call on conversation history, a tool result, a memory, or an internal
    helper prompt. This parser does not classify arbitrary natural language.
    Exact model/provider tokens are retained, not normalized or guessed.
    """
    import re
    if type(message) is not str or '\n' in message or '\r' in message:
        return None
    text = message.strip()
    parts = text.split(None, 1)
    if parts and parts[0] == '/orchestration':
        return parts[1] if len(parts) == 2 else 'show'
    prohibitions = {
        'do not delegate': 'off', 'no subagents': 'off',
        'do it yourself': 'off', 'no delegation or graphs': 'off',
        'no graphs': 'no-graphs',
    }
    if text.lower().removesuffix('.') in prohibitions:
        return prohibitions[text.lower().removesuffix('.')]
    match = re.fullmatch(r'use ([^\s\"\'`]+) via ([^\s\"\'`]+) as secondary model(?: reasoning ([a-z]+))?', text)
    if match:
        model, provider, effort = match.groups()
        return shlex.join(['secondary', provider, model] + ([effort] if effort else []))
    return None


def apply_operator_control(db, session_id, text, *, source_message_id=None):
    """Apply one explicit control with CAS; callers supply authenticated identity."""
    try:
        words = shlex.split(text or "show")
    except ValueError as exc:
        raise OrchestrationError("orchestration_control_invalid") from exc
    action = words[0] if words else "show"
    if action not in _ACTIONS or (action != "secondary" and len(words) != 1):
        raise OrchestrationError("orchestration_control_invalid")
    route = None
    if action == "secondary":
        if len(words) not in (3, 4):
            raise OrchestrationError("orchestration_control_invalid")
        route = SecondaryRoute(words[1], words[2], words[3] if len(words) == 4 else None)
    record = db.get_orchestration_policy(session_id)
    snapshot = _config_for_owner(db)
    policy = OrchestrationPolicy.from_dict(record["policy"] if record else snapshot.config["orchestration"])
    revision = record["revision"] if record else 0
    if action == "show":
        return record or {"session_id": session_id, "revision": 0,
                          "policy": policy.to_dict(), "origin": "profile-default",
                          "source_message_id": None}
    if action in {"off", "direct"}:
        policy = replace(policy, mode="direct", allow_delegation=False, allow_graph=False)
    elif action == "prefer":
        policy = replace(policy, mode="prefer_workers", allow_delegation=True, allow_graph=True)
    elif action == "no-delegation":
        policy = replace(policy, allow_delegation=False)
    elif action == "no-graphs":
        policy = replace(policy, allow_graph=False)
    elif action == "secondary":
        policy = replace(policy, secondary_route=route)
    elif action == "inherit-secondary":
        policy = replace(policy, secondary_route=None)
    validate_managed_policy(policy, snapshot)
    return db.set_orchestration_policy(session_id, policy.to_dict(),
        expected_revision=revision, origin="operator-command",
        source_message_id=source_message_id)


def handle_cli_command(cli, command):
    """Called only by the CLI's explicit operator slash-command dispatcher."""
    owner = getattr(cli, "_session_db", None)
    session_id = getattr(cli, "session_id", None)
    if owner is None or type(session_id) is not str or not session_id:
        raise OrchestrationError("policy_store_unavailable")
    # This is the host-generated current CLI identity, not a user-supplied
    # prefix or the model's guessed session id. Existing rows are never reset.
    if owner.get_session(session_id) is None:
        model = getattr(cli, "model", None)
        owner.create_session(session_id, "cli", model=model if type(model) is str else None)
    text = command.partition(" ")[2] or "show"
    return apply_operator_control(owner, session_id, text)


def register_cli(subparsers):
    parser = subparsers.add_parser("orchestration", help="Inspect or set exact session orchestration controls")
    parser.add_argument("--session-id", required=True, help="Exact current-profile session identity (no prefix lookup)")
    parser.add_argument("action", choices=_ACTIONS, nargs="?", default="show")
    parser.add_argument("--provider", dest="secondary_provider")
    parser.add_argument("--model", dest="secondary_model")
    parser.add_argument("--reasoning", dest="secondary_effort")
    parser.set_defaults(func=run_cli)
    return parser


def run_cli(args):
    from hermes_state import SessionDB
    words = [args.action]
    if args.action == "secondary":
        if args.secondary_provider is None or args.secondary_model is None:
            print(json.dumps({"error": "secondary requires --provider and --model"}))
            raise SystemExit(2)
        words += [args.secondary_provider, args.secondary_model]
        if args.secondary_effort is not None:
            words.append(args.secondary_effort)
    owner = SessionDB()
    try:
        result = apply_operator_control(owner, args.session_id, shlex.join(words))
    except (OrchestrationError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "status": "blocked"}))
        raise SystemExit(2) from exc
    finally:
        owner.close()
    print(json.dumps(result, indent=2))
