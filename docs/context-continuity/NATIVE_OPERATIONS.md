# Native context authority operation

Context Governor works without enrolling this optional external-effect route.
Enrollment is an explicit operator operation. Turning off automatic rebase or
canary configuration does not remove an enrolled session's admission guards.

Use the paired recursive-agent source identified by `native-external-owner.json`.
The daemon owns grants, generations, permit issuance/consumption and receipt
history. SessionDB owns the settled response and local publication. Desktop's
effect approval key and the SessionDB controller key have separate purposes.

## Enrollment

1. Initialize a new native external namespace with
   `ra-daemon initialize-context-authority --root <native-root>`. Keep its printed
   incarnation. Initialization permanently fences legacy permit admission in
   that namespace; choose a disposable namespace for the first canary.
2. Start the paired daemon using the existing production public-verifier file
   and exact production write root. Export public controller grant inputs with
   `hermes context-authority export --session <session-id> --socket <socket>`.
   The existing session must be idle; the command acquires its actual turn lease.
3. Obtain the native public verifier digest using
   `ra-daemon describe-production-verifier --production-verifier-file <file>
   --production-write-root <root>`. Add the exported scope, public controller
   key and initial head to an operator-owned native scope grant. Set explicit
   actor, policy version/digest, validity, effect cap and transition cap. The
   native `docs/context-authority-v1.md` specifies the closed configuration.
4. Restart the daemon with its `--context-authority-file <file>`, then run
   `hermes context-authority enroll --session <session-id> --socket <socket>
   --incarnation <incarnation>`. Ares only attaches the exact unused initial
   grant. It cannot create a grant over IPC or silently adopt another generation.
5. Inspect public registration and recovery state with
   `hermes context-authority status --session <session-id>`.

The controller seed remains in a private `context-controller-keys` directory
beside this profile's database. Ordinary exports, backups, profile clones,
managed file readers and attachments exclude it. Database/key copies cannot
silently acquire the original controller identity. These checks do not isolate
programs running as the same operating-system user or detect in-place rollback.

## Dispatch and recovery

The submitting thread captures the settled response, physical context,
generation and authenticated approval route. A queued worker cannot adopt a
later response. After middleware and approval, the native daemon consumes the
permit for the exact final call. A missing consume acknowledgement never runs
the effect. Historical outcome reporting uses the original transport and permit.

The enrolled effect route currently admits production `write_file`. Other effect
owners fail with `CONTEXT_EFFECT_OWNER_UNQUALIFIED`; the supported read-only
tools retain local response/control checks. Nested tool dispatch uses the same
captured owner. Approval routing keeps the live gateway/API route when rebase
changes the physical SessionDB session ID.

Rollover persists its native transition request before sending it. Native
retirement precedes local child publication; activation binds the exact committed
child and retirement receipt. Readback resolves an uncertain acknowledgement.
Mutating retries recheck current lease, controls, identity and the existing shared
three-attempt/900-second recovery budget. A stale native snapshot cannot admit
provider work or a tool signature.

Two cases deliberately remain stopped for owner reconciliation:

- A prepublication failure after native retirement leaves the original context
  fenced and the exact successor intent preserved. It does not reopen old tokens
  or mint a different child automatically.
- Native consumed-effect references remain obligations, including a tool's
  reported success. Until a qualified external owner supplies a disposition,
  activation stops with `CONTEXT_NATIVE_EFFECT_RECONCILIATION_REQUIRED`. Deleting
  receipt history or clearing database metadata is not a recovery procedure.

## Receipt-based live validation after merge

The operator authorized merge after implementation review and repository checks,
with real-workload debugging and installed-route/endurance observations after
merge. Repository tests do not prove live transport, Desktop approval UX, remote
effect confirmation, arbitrary tool-owner coverage, or long-running stability.
Keep the native incarnation, scope, grant digest, generation, request/transition
IDs, preflight and outcome receipt digests with each observed failure. A response
receipt means that a response was recorded; it does not mean the task is done.

No installed profile is enrolled or activated by this change. Rollback may stop
new publication, but must preserve readers and native fences for existing
enrollment. Restore source and paired native version together; retain state and
receipts for explicit owner recovery.
