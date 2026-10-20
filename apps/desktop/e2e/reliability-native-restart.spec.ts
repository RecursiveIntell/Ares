/** C09 supporting native witness: continuity text receipts across a cold application restart.
 * Real Electron -> temporary serve -> compute host -> SQLite, local mock provider only.
 * Not the complete route/media/goal/Stop qualification matrix and not a paid-provider test.
 */
import { spawnSync } from 'node:child_process'
import * as path from 'node:path'
import * as fs from 'node:fs'
import { test, expect, type ElectronApplication, type Page } from './test'
import { setupMockBackend, launchDesktop, buildAppEnv, waitForAppReady } from './fixtures'
import { matchingReceiptMessages, recordedOwnersSettled, type RecordedOwner, type OwnerObservation } from './native-state-identity'

const ROOT = path.resolve(import.meta.dirname, '../../..')
const PYTHON = path.join(ROOT, '.venv/bin/python')
const FIRST = 'C09 neutral exact input before restart.'
const SECOND = 'C09 neutral exact input after restart.'

// Read live state through read-only transactions, NEVER copy only state.db.
// Do not import SessionDB: inspection must not run schema migrations or write bookkeeping.
const READBACK = fs.readFileSync(path.join(import.meta.dirname, 'native-state-readback.py'), 'utf8')

type ReceiptRecord = {
  db: string
  receipt: { schema: string; source: string; content: unknown; event_id: string; conversation_root: string }
  origin: unknown
}
type Readback = {
  receipts: ReceiptRecord[]
  messages: { db: string; session_id: string; role: string; content: string }[]
  processes: { pid: number; birth: string; role: string }[]
  prior_owners: OwnerObservation[]
}

function readback(home: string, previous: RecordedOwner[] = []): Readback {
  const child = spawnSync(PYTHON, ['-P', '-c', READBACK, home, JSON.stringify(previous)], {
    encoding: 'utf8',
    timeout: 10_000,
    env: { PATH: process.env.PATH, HOME: process.env.HOME, HERMES_HOME: home, PYTHONHASHSEED: '0' }
  })
  if (child.status !== 0) throw new Error(`SQLite readback failed: ${child.stderr.slice(-600)}`)
  return JSON.parse(child.stdout.trim()) as Readback
}

function matching(state: Readback, text: string) {
  return state.receipts.filter(row => row.receipt.content === text)
}

async function send(page: Page, text: string) {
  const composer = page.locator('[contenteditable="true"]').first()
  await composer.waitFor({ state: 'visible', timeout: 30_000 })
  await composer.fill(text)
  await page.keyboard.press('Enter')
}

test('native text receipt and original origin survive restart without replay', async ({}, info) => {
  test.setTimeout(300_000)
  const fixture = await setupMockBackend({
    extraConfig: 'compression:\n  context_rebase_enabled: true\ndashboard:\n  turn_isolation: true\n'
  })
  let app: ElectronApplication = fixture.app
  let page: Page = fixture.page
  let bodyFailed = false
  try {
    await waitForAppReady(fixture, 120_000)
    const initialElectronPid = app.process().pid
    await send(page, FIRST)
    await expect.poll(() => matching(readback(fixture.sandbox.hermesHome), FIRST).length, { timeout: 60_000 }).toBe(1)
    await page.waitForFunction(
      () => document.body.textContent?.includes('Hello from the mock inference server'),
      undefined,
      { timeout: 60_000 }
    )
    await expect
      .poll(() => matchingReceiptMessages(readback(fixture.sandbox.hermesHome), FIRST).length, {
        timeout: 30_000
      })
      .toBe(1)
    const before = readback(fixture.sandbox.hermesHome)
    const receiptBefore = matching(before, FIRST)[0]
    expect(matchingReceiptMessages(before, FIRST)).toHaveLength(1)
    expect(receiptBefore.receipt.schema).toBe('SessionDBContextInputV1')
    expect(receiptBefore.receipt.source).toBe('desktop')
    expect(receiptBefore.origin).not.toBeNull()
    expect(before.processes.some(row => row.role === 'serve')).toBe(true)
    expect(before.processes.some(row => row.role === 'host')).toBe(true)
    const route = await page.evaluate(() => window.location.hash)
    await info.attach('before-restart-receipt.json', {
      body: JSON.stringify(before, null, 2),
      contentType: 'application/json'
    })
    await page.screenshot({ path: info.outputPath('before-restart.png') })

    await app.close()
    const restarted = await launchDesktop(buildAppEnv(fixture.sandbox))
    app = restarted.app
    page = restarted.page
    expect(app.process().pid).not.toBe(initialElectronPid)
    await waitForAppReady({ ...fixture, app, page }, 120_000)
    // Explicitly select the stored route. This is not automatic retry or re-home.
    await page.evaluate(hash => {
      window.location.hash = hash
    }, route)
    await expect.poll(() => page.evaluate(() => window.location.hash), { timeout: 15_000 }).toBe(route)
    const reopened = readback(fixture.sandbox.hermesHome)
    expect(matching(reopened, FIRST)).toEqual([receiptBefore])
    expect(matchingReceiptMessages(reopened, FIRST)).toHaveLength(1)
    // Observe exact recorded PID/birth identities, independent of later argv,
    // role, HOME, or discovery visibility. Unknown never proves retirement.
    let retirementObservation: Readback | undefined
    await expect.poll(() => {
      retirementObservation = readback(fixture.sandbox.hermesHome, before.processes)
      return recordedOwnersSettled(before.processes, retirementObservation.prior_owners)
    }, { timeout: 30_000 }).toBe(true)
    await info.attach('recorded-owner-retirement.json', {
      body: JSON.stringify(retirementObservation!.prior_owners, null, 2), contentType: 'application/json'
    })

    await send(page, SECOND)
    await expect.poll(() => matching(readback(fixture.sandbox.hermesHome), SECOND).length, { timeout: 60_000 }).toBe(1)
    await expect
      .poll(() => matchingReceiptMessages(readback(fixture.sandbox.hermesHome), SECOND).length, {
        timeout: 60_000
      })
      .toBe(1)
    const after = readback(fixture.sandbox.hermesHome)
    expect(matching(after, FIRST)).toEqual([receiptBefore])
    expect(matching(after, SECOND)[0].receipt.conversation_root).toBe(receiptBefore.receipt.conversation_root)
    expect(matching(after, SECOND)[0].receipt.event_id).not.toBe(receiptBefore.receipt.event_id)
    expect(matchingReceiptMessages(after, FIRST)).toHaveLength(1)
    expect(matchingReceiptMessages(after, SECOND)).toHaveLength(1)
    await info.attach('after-restart-receipt.json', {
      body: JSON.stringify(after, null, 2),
      contentType: 'application/json'
    })
    await page.screenshot({ path: info.outputPath('after-restart.png') })
  } catch (error) {
    bodyFailed = true
    throw error
  } finally {
    const cleanupErrors: { step: string; error: string }[] = []
    const observeCleanup = async (step: string, action: () => unknown | Promise<unknown>) => {
      try { await action() } catch (error) { cleanupErrors.push({ step, error: String(error) }) }
    }
    await observeCleanup('final-readback', () => info.attach('final-owner-readback.json', {
      body: JSON.stringify(readback(fixture.sandbox.hermesHome), null, 2),
      contentType: 'application/json'
    }))
    await observeCleanup('diagnostic-logs', async () => {
      const logs = path.join(fixture.sandbox.hermesHome, 'logs')
      for (const name of fs.existsSync(logs) ? fs.readdirSync(logs) : []) {
        const file = path.join(logs, name)
        if (!fs.statSync(file).isFile()) continue
        // Select error/ownership diagnostics only, not raw request/auth dumps.
        const lines = fs
          .readFileSync(file, 'utf8')
          .split('\n')
          .filter(line => /AUTHZ_WITNESS|CONTEXT_INPUT_|ContextContinuationError|_context_input_authorizer/.test(line))
          .slice(-30)
        if (lines.length) await info.attach(`owner-${name}.txt`, { body: lines.join('\n'), contentType: 'text/plain' })
      }
    })
    await observeCleanup('electron-close', () => app.close())
    await observeCleanup('mock-close', () => fixture.mock.close())
    await observeCleanup('sandbox-cleanup', () => fixture.sandbox.cleanup())
    if (cleanupErrors.length) {
      await observeCleanup('cleanup-report', () => info.attach('cleanup-errors.json', {
        body: JSON.stringify(cleanupErrors), contentType: 'application/json'
      }))
      // Retain the original body exception. A cleanup-only failure still
      // refuses the test, and every cleanup step was attempted independently.
      if (!bodyFailed) throw new Error(`Native fixture cleanup incomplete: ${JSON.stringify(cleanupErrors)}`)
    }
  }
})
