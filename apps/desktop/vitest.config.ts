import type { TestProjectConfiguration } from 'vitest/config'
import { defineConfig } from 'vitest/config'

const reactUi: TestProjectConfiguration = {
  extends: './vite.config.ts',
  test: {
    name: 'ui',
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    globals: true,
    // The first test in each file pays jsdom env init + full module transform.
    // The 578-file UI lane can push that cold start past 15s on high-core
    // hosts even when the same file completes in under 7s alone. 30s keeps a
    // bounded hang detector while covering proven full-suite contention.
    testTimeout: 30_000
  }
}

const electronNative: TestProjectConfiguration = {
  test: {
    name: 'electron',
    environment: 'node',
    // `e2e/**/*.unit.test.ts` is the e2e HELPERS, not the specs: plain node
    // modules that should be provable without booting Electron. Playwright
    // ignores the same pattern so they run in exactly one runner.
    include: ['electron/**/*.test.ts', 'scripts/**.test.{ts,mjs}', 'e2e/**/*.unit.test.ts'],
    // These use node:test and have dedicated npm scripts, not Vitest suites.
    exclude: ['scripts/run-short-session-hang-repro.test.mjs', 'scripts/tasks-scroll.test.mjs']
  }
}

export default defineConfig({
  test: {
    projects: [reactUi, electronNative]
  }
})
