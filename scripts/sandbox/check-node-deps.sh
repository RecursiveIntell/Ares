#!/usr/bin/env bash
# Read-only health gate for the workspaces required by the CLI/TUI/web update.
# npm ls checks missing and invalid dependency trees without installing,
# running lifecycle scripts, or contacting the registry. Desktop is optional.
# Its raw output can contain registry URLs, so only a fixed status is emitted.
set -euo pipefail

[ "$#" -eq 1 ] || { echo 'error: expected the installation directory' >&2; exit 2; }
cd -- "$1"
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo 'error: Node.js or npm is unavailable for dependency health check' >&2
  exit 1
fi

summary="$(mktemp)"
trap 'rm -f -- "$summary"' EXIT
status=0
npm ls --offline --all --include=dev --workspace ui-tui --workspace web \
  --include-workspace-root --json >"$summary" 2>/dev/null || status=$?
if [ "$status" -ne 0 ]; then
  printf 'error: required Node dependencies are missing or invalid (npm ls exit %s)\n' "$status" >&2
  exit "$status"
fi

# npm's workspace-filtered list can omit missing root devDependencies without
# failing. Check their presence in its result against the canonical manifest.
# A separate --workspaces=false call would require omitted desktop workspace
# links in some npm versions, so it would broaden the update contract.
if ! node - "$summary" "$PWD/package.json" <<'NODE' >/dev/null 2>&1
const fs = require('node:fs');
try {
  const summary = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const manifest = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
  const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  if (!record(summary) || !record(manifest)) throw new Error('invalid JSON object');
  const groups = ['dependencies', 'devDependencies', 'optionalDependencies'].map(key =>
    manifest[key] === undefined ? {} : manifest[key]);
  if (groups.some(group => !record(group) || Object.values(group).some(value => typeof value !== 'string')))
    throw new Error('invalid dependency map');
  const required = Object.keys({...groups[0], ...groups[1]});
  const optional = groups[2];
  const actual = summary.dependencies || {};
  if (!record(actual)) throw new Error('invalid dependency result');
  const healthy = required.filter(name => !Object.hasOwn(optional, name)).every(name =>
    Object.hasOwn(actual, name) && record(actual[name]) && !actual[name].missing && !actual[name].invalid);
  process.exit(healthy ? 0 : 1);
} catch (_) {
  process.exit(1);
}
NODE
then
  echo 'error: required Node dependencies are missing or invalid (root manifest check)' >&2
  exit 1
fi
echo 'Required Node dependency health check passed (root, ui-tui, web)'
