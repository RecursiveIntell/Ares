# Desktop package discovery correction

Scope: CLI unpacked-executable discovery and macOS updater rebuilt-source naming.
Canonical owner: `apps/desktop/package.json` builder product/executable identity.
No change to destination authorization, signing, installation eligibility, rollback,
architecture validation, or live updater execution. No dependency/build changes.

The former detector enumerated only Hermes names although the manifest builds
Ares. The POSIX updater also searched only `Hermes.app`; its relaunch target is
an installed destination and did not override rebuilt-source selection.

Both callers now use a standard-library discovery helper. It reads current builder
names, validates path components, accepts only regular files in explicit unpacked
layouts, and rejects symlink substitution. Current identity is authoritative: missing Ares output cannot select stale Hermes
output. Explicit Hermes compatibility layouts require a positively identified
Hermes manifest (fallback is logged). Missing/malformed manifests and
missing candidates fail closed. A missing macOS rebuilt source reports manual
recovery rather than silently declaring that the desktop was updated.

Rollback: revert this bounded commit. No user installations or runtime state are
modified by validation. Cross-platform fixture discovery is not native platform
execution or signing proof; Windows/macOS launch and installation remain separate
gates. Existing Windows architecture/integrity checks remain downstream.
