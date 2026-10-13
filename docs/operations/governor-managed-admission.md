# Governor-only managed admission and backout

This opt-in recipe adds one Context Governor executable to an Ares release. It does not run the full enhancement installer, replace Cargo's global executable, configure MCP servers, or change the profile's model/provider/context policy.

## Admission

Run the source-bound controller that implements this option against a clean Ares source checkout and a clean Git checkout of the standalone native crate:

```bash
ares setup --source /path/to/clean/ares --governor-source /path/to/libraries/context-governor
```

An older installed controller will reject the new argument. For the initial transition, invoke the reviewed new source's controller using its verified interpreter/import context; do not replace launchers or release pointers manually.

The native recipe captures the exact commit, crate subtree, remote, Cargo lock digest, toolchain version, flags, target and resulting executable digest. The build derives its inputs from Git, requires the commit to be reachable from the declared remote's default head, uses locked default-feature Cargo compilation in staging, and copies only the produced executable into that inactive release. The existing `release.json` owns the component binding; there is no second active-native selector.

Before final binding, the controller probes the actual final-path Python producer and native compact/finalize/prepare/discard contract with newly generated fixture-only authority. Live keys are not copied into that fixture. The normal key owner retains existing live authority; a first install initializes missing canonical authority through its existing path, not by key rotation or fixture-key adoption.

The governor adapter resolves a declared bundle from its physical import-time source owner and validates its digest before native execution. A failed or missing declared binding cannot fall back to an ambient executable. Explicit binary injection remains available for unbundled isolated fixtures, but cannot override a declared native component.

## Binding families and reuse

Base releases retain their existing local lifecycle binding. Native-bearing releases use a distinct native-aware binding family. An older controller cannot validate or select that family. Neither old descriptors nor old binary provenance are restamped.

An existing immutable Ares revision cannot acquire a different native recipe in place. A mismatch is refused; use a new reviewed Ares revision. Setup without an explicit new native source, and ordinary updates, carry the admitted source tuple from the current release descriptor rather than silently dropping the component or following a moving native branch.

## Backout and recovery

The existing `current`/`previous` pair and transition journal remain the only selection owner. A transition from an external-native base release records that exact predecessor's executable and descriptor digests in the candidate's backout witness. The external executable remains intact. Activation/backout refuse predecessor drift; native-to-native backout validates both bundles.

```bash
ares rollback
```

After backout to a base release, its older controller intentionally cannot forward-select a native-aware release. Retain the exact new release interpreter/source and use that supported new controller for explicit setup/reselection with the reviewed native source. Do not infer the new owner from directory recency, rewrite links, or retry a possibly completed effect without reading the exact target first.

A selected release, passing capability probe, or service start is not Desktop or provider-backed qualification. Verify effective engine binary, source/artifact identity, receipt/key compatibility, gateway identity, Desktop coherence and interaction separately. Preserve failed attempts and use successor receipts. Unrelated active work must not be interrupted by a deployment.
