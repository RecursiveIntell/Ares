"""Explicit operator enrollment and readback; no model-facing signing API."""
import json
import secrets


def build_context_authority_parser(subparsers):
    parser = subparsers.add_parser("context-authority", help="Inspect or attach an operator-enrolled native context scope")
    parser.add_argument("action", choices=("export", "enroll", "status"))
    parser.add_argument("--session", required=True, help="Existing physical session ID in this profile")
    parser.add_argument("--socket", help="Absolute socket path of the paired native daemon")
    parser.add_argument("--incarnation", help="Native initialize-context-authority challenge (enroll only)")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.set_defaults(func=cmd_context_authority)


def cmd_context_authority(args):
    from hermes_state import SessionDB
    from ares_runtime.continuity.authority import transport

    with SessionDB() as db:
        if db.get_session(args.session) is None:
            raise SystemExit("CONTEXT_AUTHORITY_SESSION_NOT_FOUND")
        if args.action == "status":
            print(json.dumps({"registration": db.read_native_context_authority(args.session),
                              "rebase": db.read_native_context_rebase(args.session)}, sort_keys=True))
            return
        connection = transport({"socket_path": args.socket, "timeout_seconds": args.timeout})
        if args.action == "enroll" and not args.incarnation:
            raise SystemExit("CONTEXT_AUTHORITY_INCARNATION_REQUIRED")
        holder = "context-authority:" + secrets.token_hex(16)
        if not db.try_acquire_session_turn_lease(args.session, holder, ttl_seconds=120):
            raise SystemExit("CONTEXT_AUTHORITY_SESSION_BUSY")
        try:
            if args.action == "export":
                result = db.export_context_authority_identity(args.session, turn_lease_holder=holder, transport=connection)
            else:
                result = db.enroll_native_context_authority(args.session, turn_lease_holder=holder,
                                                           transport=connection, incarnation=args.incarnation)
            print(json.dumps(result, sort_keys=True))
        finally:
            db.release_session_turn_lease(args.session, holder)
