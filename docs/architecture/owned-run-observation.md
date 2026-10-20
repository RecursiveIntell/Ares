# Owned-run observation

`session.run_checkpoint.basis` is a read-only checkpoint-control operation. Its
closed request contains `session_id`, `run_id`, and positive integer
`expected_generation`. The transport session selects the actual live agent;
that agent independently supplies its current SessionDB session and private
turn holder. The caller cannot supply credentials, an owner token, a DB path,
a task definition, a clock, or TTL.

The existing TurnRunCustody private handle selects exactly the named run while
holding its existing lock. Up to sixteen legitimate handles remain supported.
No run is globally selected, claimed, renewed, recovered or released. Pending,
unknown, recovery-pending and missing handles refuse. Existing V1 readers and
claim/refresh/release formats remain unchanged; this new observation requires V2.

SessionDB reads canonical head/member, capability predicates, authentic input
provenance, session/root/profile lineage, root lease, Stop, pending input and
full custody/obligation inventory on one SQLite read snapshot. Released
unresolved obligations still block. End-of-observation wall and monotonic checks
prevent returning an expired observation. No external file reads or native RPC
occur inside that transaction. Writers can immediately stale the returned
observation; it is neither an admission fence nor a reusable capability.

The frozen `SessionDBOwnedRunObservationV1` projection includes historical task
coordinates and exact head/checkpoint/control/inventory digests, with
`observation_only=true`, `authorizes_effects=false`, `custody_changed=false`.
The profile name is a label, not an activation incarnation. The explicitly named
owner contract revision is a protocol version, not an executable Git identity.
Domain-separated SHA-256 binds the closed semantic payload excluding its own
digest. It provides content integrity only. No input text, full checkpoint,
filesystem locator, token, holder or independent secret-field hash is returned.

Direct and compute-host control paths use the same owner operation. The serving
gateway validates host result schema, run/generation/current session and digest
before relay; host error prose and data are never blindly forwarded. An absent,
malformed or lost host response is unavailable/unknown with
`custody_changed=false`, without local fallback or mutation retry. Prior custody
uncertainty is retained untouched.

A route-aware decoder at stdio, WebSocket, compute-host input and host ACK
boundaries detects all basis selectors, including overwritten/escaped ones,
and rejects duplicate keys, nonfinite numbers and oversized basis frames before
handling them. Other routes retain legacy JSON behavior. Malformed-frame logs
omit raw payload previews because syntax errors prevent reliable classification.

## Qualification and rollback

Focused tests use actual disposable SessionDB and private claim paths, real
TUI/compute-host control dispatch and raw ingress loops. Coherence is checked
with a second real SQLite writer committing Stop after the reader's head read:
the returned snapshot stays coherent and the next observation refuses. No
provider call, real profile activation, installed qualification or full W05
bootstrap claim follows from these tests.

Rollback removes/disables only the new basis route and its projection. No
persisted schema or migration is introduced, and no custody history, consumed
permit, prior obligation or activation state is rewritten.
