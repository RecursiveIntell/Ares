# Current-owner integration boundary

## Profile-runtime V2

The current Libraries owner at `9849d12adc8a5ef852d56e36abec4ffbb5122c62`
exports `profile-runtime.resolved-policy-basis/v2`. Its outer reference binds
the task-specific V1 projection digest. The legacy V1 reference names a
composition receipt and can be shared by different task projections.

Use `ResolvedPolicyBasisV2.from_profile_runtime` with the expected task reference
and a trusted current-owner resolver. Ares preserves the complete V2 envelope,
outer reference and opaque owner digest through materialization, persistence
and egress revalidation. The nested V1 fields supply policy constraints; they
never replace the outer V2 identity. V1 callers remain available through their
explicit existing API; V2 never silently falls back to V1.

The resolver must authenticate the owner and current task state. An injected
callback, deserialized projection, valid digest, qualification fixture or
successful materialization is not effect authorization. Serialized projections
remain evidence-only; consume-time owner readback and independent permit
boundaries remain mandatory.

`Current owner integration` CI compiles the exact pinned Rust owner, regenerates
fresh inputs and exercises all eight Ares managed-call classes through receipt
persistence and egress. The checked-in golden artifact is a captured fixture;
CI regenerates it because owner constructor IDs may differ on each run. The
workflow uploads exact source identities and produced input hashes.

This certifies the candidate materialization path. Ares main has no production
bootstrap constructing this policy adapter or `GovernedContextMaterializer`.
This PR does not claim live profile/service activation or close that separate
authenticated runtime-bootstrap gate.

## Separate owner gates

- Semantic-memory witnessed recall stays on `sm_search_governed_witnessed_v2`.
  Libraries' new integrity planner is a trusted-local, sealed read-only
  schema-39 diagnostic API. It has no MCP transport or apply API and must not
  be called as normal recall or interpreted as repair permission.
- ClaimLedger owns support admission and support history. Its V1 support
  diagnostic's `observed_links_consistent_history_unverified` status is not an
  authoritative supported/closed state. Generic Ares closure booleans or
  projection dictionaries must never substitute for owner verification.
- Native paired-owner qualification and its Libraries pin are independent of
  this profile-runtime gate. They remain separately qualified; a passing
  profile-runtime gate does not establish native daemon compatibility.

Rollback is the exact source PR revert. No data migration, profile activation,
credential change, repair application or new persisted owner state is involved.

## Owner validity-window wire contract

Ares compares profile-runtime RFC3339 timestamps as UTC-second/nanosecond
pairs without rewriting the original strings or recomputing owner digests.
This follows the pinned owner's Chrono semantics: `Z`, signed offsets,
lowercase/space separators, leap seconds and nanosecond precision (excess
fraction digits are ignored for comparison only). An inverted window fails
before owner readback; equal endpoints remain structurally valid but are
never active because expiry is exclusive. Equivalent instants encoded with
different wire strings do not satisfy an exact current-owner readback.

The Rust qualification producer generates both accepted temporal V2
envelopes and rejected malformed/inverted cases through the actual owner.
Consumer tests exercise every managed-call kind through materialization,
receipt persistence and egress, including nanosecond validity boundaries.
This is a typed mapping boundary: it does not claim that an unimplemented
raw JSON transport rejects duplicate keys or imposes wire size/depth limits.
Those checks must be bound to the concrete authenticated transport when it
is selected; qualification callbacks do not authenticate an owner.

The generic Libraries `boundary-compiler::SchemaValidator::validate` is a
no-op in the inspected owner revision, but is neither called nor depended on
by this profile-runtime validation path. Profile-runtime validates its own
closed typed policy contract and validity window. The generic no-op remains
explicitly out-of-path debt, not a claimed fixed Ares bypass.

## Origin revocation adoption

The current paired native, Governor and current-owner qualification selections
all use Libraries `9849d12adc8a5ef852d56e36abec4ffbb5122c62`, including the
canonical origin-revocation epoch fix from Libraries PR57. Historical proof
objects retain their original tuple and do not certify this new selection.
Native stays at `2ea67c4ffe70b0dce95a7792b241693a9f9f4dcc`; no unmerged native
guard candidate is adopted.

The current-owner lane builds a test-only peer against that exact source with
an explicit MockEmbedder, brute-force backend and disposable stores. Ares calls
its actual witnessed V2 owner API, persists a managed materialization, then
checks final egress before and after an independently opened owner handle
revokes the admitted fact. Every managed-call kind must reject the old witness
with `MEMORY_AUTHORITY_CHANGED` for both required and optional memory. Exact
revocation replay preserves state and cannot restore egress. Fresh recall must
return no admitted revoked content. The fixture has no product command, remote
transport, live-store input, operator credential or provider call.

This is source-selection and consumer qualification only. Production task/profile
authority derivation and an authenticated memory-owner transport remain separate
open integration gates. Ordinary runtime bootstrap and installed adoption are
not established by this test, by historical fixtures or by the pin change.
