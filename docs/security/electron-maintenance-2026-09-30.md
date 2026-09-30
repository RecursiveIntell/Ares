# Electron security dependency maintenance — 2026-09-30

## Scope and decision

Update the Desktop Electron pin from **41.10.3 to 41.10.7**, the latest
published same-major patch as checked on 2026-09-30. Version 41.10.6 contains
the named advisory fixes below; 41.10.7 additionally backports Chromium, V8,
ANGLE, Dawn and Skia fixes and repairs Linux/Windows/DevTools behavior. Keep
the installed dependency, `build.electronVersion`, root `allowScripts`
decision and root lockfile in agreement, without unrelated dependency churn.

Upstream [marks Electron 41.x end-of-support](https://github.com/electron/electron/releases/tag/v41.10.7).
This is an **interim advisory remediation**; migrating to a supported major
requires a separately validated compatibility pass. It does not close the
unsupported-runtime risk.

The pre-change source is Ares `9e571b7811407f47c50648cf53ba88c39ce5cea9`.
Upstream [41.10.6 release notes](https://github.com/electron/electron/releases/tag/v41.10.6)
confirm the protocol, popup and worker-preference fixes.

## Advisory applicability at that source

- [GHSA-gr2m-v5gq-v685](https://github.com/electron/electron/security/advisories/GHSA-gr2m-v5gq-v685): top-level HTML sandbox popup inheritance, fixed in 41.10.6.
  Common chat windows deny Electron popup creation via
  `wireCommonWindowHandlers` in `electron/main.ts`. OAuth and portal windows
  are separate, remote-content windows without that common popup-deny handler.
  Whole-app non-applicability is **not established**. Electron process
  `sandbox: true` is distinct from the advisory's HTML sandbox restriction.
- [GHSA-9qh4-3jw8-366w](https://github.com/electron/electron/security/advisories/GHSA-9qh4-3jw8-366w): guest worker Node integration, fixed in 41.10.6.
  `chatWindowWebPreferences` defaults the embedder to `sandbox: true` and
  `nodeIntegration: false`; the preview guest also explicitly requests those
  settings. That mitigates the default path. Existing Windows recovery and
  user opt-out paths can launch with `--no-sandbox`, so the default declaration
  alone does not certify every deployed launch unaffected.
- [GHSA-j84w-jfhq-vhvj](https://github.com/electron/electron/security/advisories/GHSA-j84w-jfhq-vhvj): legacy file/HTTP protocol cross-origin reads, fixed in 41.10.6.
  Ares media registration uses `protocol.handle`; no `registerFileProtocol` or
  `registerHttpProtocol` use was found in Desktop source. The reviewed media
  path does not meet this advisory's legacy-handler condition.
- [GHSA-hq2x-r82h-9wj4](https://github.com/electron/electron/security/advisories/GHSA-hq2x-r82h-9wj4): HTML iframe sandbox popup inheritance, fixed in 41.10.4.
  The common chat handler denies Electron popup creation. Preview webviews do
  not request `allowpopups`; arbitrary remote content can still contain its
  own frames. Updating Electron removes reliance on source-only mitigation.
- [GHSA-vv43-5jgx-7qv8](https://github.com/electron/electron/security/advisories/GHSA-vv43-5jgx-7qv8): Squirrel.Mac privileged update race, fixed in 41.10.5.
  No Electron `autoUpdater`/Squirrel update integration was found in Desktop
  source; Ares uses its own update handoff. macOS update behavior was not
  executed on the Linux validation host.
- [GHSA-qmv3-fv6v-rmhq](https://github.com/electron/electron/security/advisories/GHSA-qmv3-fv6v-rmhq): the published affected ranges start in Electron 42;
  the selected 41.x release is outside those ranges.

These observations bound the dependency fix; they are not a comprehensive
application-security certification or an exploit reproduction.

## Regression and validation

Existing `electron/desktop-electron-pin.test.ts` verifies the relationship
among the installed, locked and packaged versions without hard-coding today's
release. `tests-js/allow-scripts-sync.test.ts` checks the version-specific
install-script decisions. No duplicate version-snapshot test is added.

Validation results and remaining platform/runtime limits are recorded in the
PR. Keep build, native rebuild/staging, Desktop tests, exact-head CI and
independent review distinct from release/deployment certification. The existing
broad Desktop E2E workflow is explicitly disabled in `ci.yaml`; a skipped lane
must not be counted as a successful browser/auth smoke test.

## Rollback and remaining scope

Revert this bounded commit to restore the previous manifests and lockfile,
then reinstall from that lock. Doing so reintroduces the old dependency's
advisory exposure; use rollback only for a demonstrated regression and retain
a hold on distribution until resolved. No application deployment, public
release, credential or OS-permission change is part of this maintenance.

A post-update npm audit must be interpreted package by package. Other
workspace advisories remain separate work and are not suppressed by this fix.

## Supported-major follow-up

The [official schedule](https://releases.electronjs.org/schedule) makes 42
the nearest supported major on 2026-09-30; its observed latest npm patch is
42.11.10, but its scheduled end-of-life is **2026-10-20**. Electron 43 is
supported until 2027-01-05 and 44 until 2027-03-02. Select the migration target
with the supported OS floor and remaining support window in view.

The [42 breaking changes](https://www.electronjs.org/docs/latest/breaking-changes#breaking-api-changes-420)
need a real compatibility pass: binary download moves from postinstall to
first CLI execution (or explicit install-electron), macOS notifications
require code signing, offscreen rendering defaults its scale factor to 1,
and clearStorageData removes quotas. Ares currently assumes postinstall
artifacts in CI and directly resolves Electron distributions in packaging
and development scripts. Its native Notification path needs signed macOS
validation. Rebuild native modules and test clean install, packaging,
preload/IPC, in-app browser/popup/auth and upgrade/relaunch on target OSes.
No supported-major compatibility result is claimed by this patch.
