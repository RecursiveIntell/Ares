# Current-owner integration boundary

## Profile-runtime V2

The current Libraries owner at `02ee5dd96a3f5fb403bdd2b955d57a1456b929ea`
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
