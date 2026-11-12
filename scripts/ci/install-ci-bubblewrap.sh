#!/usr/bin/env bash
# Build and admit the reviewed Bubblewrap release on an ephemeral CI runner.
set -euo pipefail

version=0.13.1
archive="$RUNNER_TEMP/bubblewrap-${version}.tar.xz"
expected_sha256=0e85c8702248b8dd64144f594147553650cdd0e4c951690345faace79c217c4d
curl --fail --location --silent --show-error \
  --retry 3 --retry-delay 2 \
  -o "$archive" \
  "https://github.com/containers/bubblewrap/releases/download/v${version}/bubblewrap-${version}.tar.xz"
printf '%s  %s\n' "$expected_sha256" "$archive" | sha256sum --check --strict
tar -xJf "$archive" -C "$RUNNER_TEMP"
meson setup "$RUNNER_TEMP/bwrap-build" \
  "$RUNNER_TEMP/bubblewrap-${version}" \
  --prefix=/usr \
  -Dtests=false -Dman=disabled -Dselinux=disabled \
  -Dbash_completion=disabled -Dzsh_completion=disabled
meson compile -C "$RUNNER_TEMP/bwrap-build"
DESTDIR="$RUNNER_TEMP/bwrap-stage" meson install -C "$RUNNER_TEMP/bwrap-build"
sudo install -m 0755 "$RUNNER_TEMP/bwrap-stage/usr/bin/bwrap" /usr/bin/bwrap
test "$(bwrap --version)" = "bubblewrap ${version}"
sha256sum /usr/bin/bwrap

if sysctl -n kernel.apparmor_restrict_unprivileged_userns >/dev/null 2>&1; then
  sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
if sysctl -n kernel.unprivileged_userns_clone >/dev/null 2>&1; then
  sudo sysctl -w kernel.unprivileged_userns_clone=1
fi
bwrap --ro-bind / / -- /usr/bin/true
