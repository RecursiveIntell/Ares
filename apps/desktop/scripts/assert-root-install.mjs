import { readFileSync } from "fs"
import { createRequire } from "module"
import { resolve, join } from "path"

const app = resolve(import.meta.dirname, "..")
const root = resolve(app, "..", "..")

// Development admission is stronger than a shallow vite-directory check. Empty
// or incomplete package folders can satisfy `accessSync` yet leave typecheck,
// lint, or Electron tests unable to resolve their direct dependencies. This
// script runs only from source development/build paths; packaged runtime
// integrity is intentionally checked by shipped-assets/build-stamp validators,
// not by requiring pytest or development-only npm packages in a release.
const requireFromApp = createRequire(join(app, "package.json"))

export const REQUIRED_DEVELOPMENT_PACKAGES = [
  "@assistant-ui/react",
  "@assistant-ui/react-streamdown",
  "@babel/core",
  "@testing-library/react",
  "eslint",
  "globals",
  "react-router",
  "vite",
  "vitest"
]

/**
 * Source-development admission only. Packaged Electron artifacts do not carry
 * this workspace dependency tree, so their validation stays with dist/stamp
 * assertions instead of manufacturing a release dependency on test tooling.
 */
export function checkDevelopmentInstall(requireFromApp = createRequire(join(app, "package.json"))) {
  const missing = REQUIRED_DEVELOPMENT_PACKAGES.filter(pkg => {
    try {
      requireFromApp.resolve(pkg)

      return false
    } catch {
      return true
    }
  })

  return { missing, ok: missing.length === 0 }
}

const developmentInstall = checkDevelopmentInstall()

if (!developmentInstall.ok) {
  console.error(
    `Development dependencies are incomplete (${developmentInstall.missing.join(", ")}). ` +
      `Run from repo root: cd ${root} && env -u NODE_ENV npm ci --include=dev`
  )
  process.exit(1)
}

// `vite.config.ts` aliases react/react-dom to whatever this workspace resolves,
// and React refuses to run when the two come from different installed copies
// ("Minified React error #527" — it throws before the first paint, so the app
// window stays blank). npm stays silent about the split because the hoisted
// react still satisfies react-dom's caret peer range. Fail the build loudly
// instead of shipping a white screen.
const installedVersion = (pkg) =>
  JSON.parse(readFileSync(requireFromApp.resolve(`${pkg}/package.json`), "utf8")).version

const react = installedVersion("react")
const reactDom = installedVersion("react-dom")

if (react !== reactDom) {
  console.error(
    `react@${react} / react-dom@${reactDom} version mismatch — React would fail ` +
      `with error #527 and render a blank window. Pin both to the same version ` +
      `in ${join(app, "package.json")}, then reinstall: cd ${root} && npm ci`
  )
  process.exit(1)
}
