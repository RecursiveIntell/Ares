import { expect, test } from './test'
import PACKAGE_JSON from '../package.json' with { type: 'json' }

import {
  PACKAGED_BINARY_PATH,
  type MockBackendFixture,
  packagedBinaryExists,
  setupMockBackend,
  waitForAppReady,
} from './fixtures'
import { expectVisualSnapshot } from './visual-snapshot'

/**
 * E2E smoke tests for the packaged Hermes desktop app.
 *
 * Launches the real packaged Electron binary (produced by `npm run pack` →
 * `electron-builder --dir`) with a real temporary source backend and local
 * mock inference. Private homes and stripped credentials protect operator
 * state; existing --no-sandbox/--disable-gpu flags are NOT security/GPU proof.
 *
 * Skips if the packaged binary doesn't exist — run `npm run pack` first.
 */

let fixture: MockBackendFixture | null = null

test.beforeAll(async () => {
  test.skip(
    !packagedBinaryExists(),
    `Built app binary not found: ${PACKAGED_BINARY_PATH}. Run 'npm run pack' first.`,
  )

  fixture = await setupMockBackend({ packaged: true })
  await waitForAppReady(fixture, 120_000)
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test('window opens with the configured product title', async () => {
  const title = await fixture!.page.title()
  expect(title).toBe(PACKAGE_JSON.build.productName || PACKAGE_JSON.productName)
})

test('renderer loads and shows DOM content', async () => {
  const page = fixture!.page
  await page.waitForSelector('#root', { state: 'attached', timeout: 30_000 })
  const childCount = await page.locator('#root > *').count()
  expect(childCount).toBeGreaterThan(0)
})

test('HUD composer remains fully inside the transparent window', async () => {
  const hudPagePromise = fixture!.app.waitForEvent('window')

  await fixture!.page.evaluate(() =>
    (window as typeof window & {
      hermesDesktop?: { hud?: { open: (options: { sessionId: null }) => Promise<void> } }
    }).hermesDesktop?.hud?.open({ sessionId: null })
  )

  const hudPage = await hudPagePromise
  await hudPage.waitForSelector('[data-slot="composer-rich-input"]', { state: 'visible' })

  const geometry = await hudPage.evaluate(() => {
    const dock = document.querySelector<HTMLElement>('[data-slot="composer-dock"]')
    const input = document.querySelector<HTMLElement>('[data-slot="composer-rich-input"]')

    if (!dock || !input) {
      throw new Error('HUD composer did not render')
    }

    const dockRect = dock.getBoundingClientRect()
    const inputRect = input.getBoundingClientRect()

    return {
      viewportWidth: window.innerWidth,
      viewportHeight: window.innerHeight,
      dockLeft: dockRect.left,
      dockRight: dockRect.right,
      dockTop: dockRect.top,
      dockBottom: dockRect.bottom,
      inputLeft: inputRect.left,
      inputRight: inputRect.right,
      inputTop: inputRect.top,
      inputBottom: inputRect.bottom,
      // The bug class this guards: a build-time CSS optimization folding the
      // dock's identity `translate` override into `transform`, leaving
      // Tailwind's standalone `translate: -50%` live and shifting the dock
      // half a window off-screen. Surface the computed value so a failure
      // says WHY the dock moved, not just that it did.
      dockTranslate: getComputedStyle(dock).translate,
    }
  })

  // Horizontal containment — the composer shifted half a window left when the
  // standalone `translate: -50%` survived optimization (#82214, #82233).
  expect(geometry.dockLeft).toBeGreaterThanOrEqual(0)
  expect(geometry.inputLeft).toBeGreaterThanOrEqual(0)
  expect(geometry.dockRight).toBeLessThanOrEqual(geometry.viewportWidth)
  expect(geometry.inputRight).toBeLessThanOrEqual(geometry.viewportWidth)

  // Vertical containment — the toolbar/transcript clipping reported on
  // Windows (#82203) and macOS (#82214) is the same "composer escapes the
  // window" class on the other axis.
  expect(geometry.dockTop).toBeGreaterThanOrEqual(0)
  expect(geometry.inputTop).toBeGreaterThanOrEqual(0)
  expect(geometry.dockBottom).toBeLessThanOrEqual(geometry.viewportHeight)
  expect(geometry.inputBottom).toBeLessThanOrEqual(geometry.viewportHeight)

  // The dock's centering translate must be fully neutralized. Any live
  // percentage translate means the HUD override lost to the app's centering.
  // (Computed `translate` keeps percentages as-is, so this is assertable;
  // computed `transform` resolves to a matrix and is covered by the
  // geometric containment checks above.)
  expect(geometry.dockTranslate ?? 'none').not.toContain('%')

  await hudPage.close()
})

test('packaged shell connects to its temporary backend without boot errors', async () => {
  await waitForAppReady(fixture!, 60_000)
  await expect(fixture!.page.locator('[data-slot="composer-rich-input"]')).toBeVisible()
  await expect(fixture!.page.getByText('Desktop boot failed', { exact: true })).toHaveCount(0)
})

test('can capture a screenshot for the CI artifact', async () => {
  if (!fixture) {
    test.skip(true, 'Previous test failed — no app running')

    return
  }

  // Visual snapshot — won't fail on diff, just logs + generates diff image
  await expectVisualSnapshot(fixture!.page, { name: 'packaged-app-booted', timeout: 10_000, app: fixture!.app })
})
