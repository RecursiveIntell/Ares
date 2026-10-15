/** C09 supporting native witness: continuity text receipts across a cold application restart.
 * Real Electron -> temporary serve -> compute host -> SQLite, local mock provider only.
 * Not the complete route/media/goal/Stop qualification matrix and not a paid-provider test.
 */
import { spawnSync } from 'node:child_process'
import * as path from 'node:path'
import * as fs from 'node:fs'
import { test, expect, type ElectronApplication, type Page } from './test'
import { setupMockBackend, launchDesktop, buildAppEnv, waitForAppReady } from './fixtures'

const ROOT = path.resolve(import.meta.dirname, '../../..')
const PYTHON = path.join(ROOT, '.venv/bin/python')
const FIRST = 'C09 neutral exact input before restart.'
const SECOND = 'C09 neutral exact input after restart.'

// Read live WAL through a read-only transaction, NEVER copy only state.db.
// Do not import SessionDB: inspection must not run schema migrations or write bookkeeping.
const READBACK = String.raw`
import json, os, sqlite3, sys
from pathlib import Path
home = Path(sys.argv[1]).resolve()
assert home.name == 'hermes-home' and home.parent.name.startswith('hermes-e2e-')
result = {'receipts': [], 'messages': [], 'processes': []}
for dbfile in home.rglob('state.db'):
    db = sqlite3.connect(dbfile.as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        db.execute('BEGIN')
        tables = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
        if 'state_meta' in tables:
            meta = dict(db.execute('select key,value from state_meta'))
            for key, raw in meta.items():
                if ':event:' not in key or key.endswith(':origin'):
                    continue
                value = json.loads(raw)
                if value.get('schema') not in ('SessionDBContextInputV1','SessionDBContextInputV2'):
                    continue
                origin_raw = meta.get(key + ':origin')
                result['receipts'].append({'db':str(dbfile.relative_to(home)), 'receipt':value,
                    'origin':json.loads(origin_raw) if origin_raw else None})
        if 'messages' in tables:
            result['messages'].extend({'session_id':r[0], 'role':r[1], 'content':r[2]}
                for r in db.execute("select session_id,role,content from messages where role='user'"))
    finally:
        db.close()
if sys.platform == 'linux':
    for directory in Path('/proc').iterdir():
        if not directory.name.isdigit():
            continue
        try:
            env = (directory/'environ').read_bytes().split(b'\0')
            sandboxed = any(e.startswith(b'HERMES_HOME=') and os.fsdecode(e.split(b'=',1)[1]) == str(home) for e in env)
            if not sandboxed:
                continue
            cmd = (directory/'cmdline').read_bytes()
            role = 'host' if b'tui_gateway.compute_host' in cmd else 'serve' if b'hermes_cli.main' in cmd and b'serve' in cmd else None
            if role:
                stat = (directory/'stat').read_text().rsplit(')',1)[1].split()
                result['processes'].append({'pid':int(directory.name), 'birth':stat[19], 'role':role})
        except (OSError, ValueError):
            continue
print(json.dumps(result))
`

type ReceiptRecord = {
  db: string
  receipt: { schema: string; source: string; content: unknown; event_id: string; conversation_root: string }
  origin: unknown
}
type Readback = {
  receipts: ReceiptRecord[]
  messages: { session_id: string; role: string; content: string }[]
  processes: { pid: number; birth: string; role: string }[]
}

function readback(home: string): Readback {
  const child = spawnSync(PYTHON, ['-P', '-c', READBACK, home], {
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
      .poll(() => readback(fixture.sandbox.hermesHome).messages.filter(row => row.content === FIRST).length, {
        timeout: 30_000
      })
      .toBe(1)
    const before = readback(fixture.sandbox.hermesHome)
    const receiptBefore = matching(before, FIRST)[0]
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
    expect(reopened.messages.filter(row => row.content === FIRST)).toHaveLength(1)
    const oldOwners = before.processes.map(row => `${row.pid}:${row.birth}`)
    expect(reopened.processes.filter(row => oldOwners.includes(`${row.pid}:${row.birth}`))).toEqual([])

    await send(page, SECOND)
    await expect.poll(() => matching(readback(fixture.sandbox.hermesHome), SECOND).length, { timeout: 60_000 }).toBe(1)
    await expect
      .poll(() => readback(fixture.sandbox.hermesHome).messages.filter(row => row.content === SECOND).length, {
        timeout: 60_000
      })
      .toBe(1)
    const after = readback(fixture.sandbox.hermesHome)
    expect(matching(after, FIRST)).toEqual([receiptBefore])
    expect(matching(after, SECOND)[0].receipt.conversation_root).toBe(receiptBefore.receipt.conversation_root)
    expect(matching(after, SECOND)[0].receipt.event_id).not.toBe(receiptBefore.receipt.event_id)
    expect(after.messages.filter(row => row.content === FIRST)).toHaveLength(1)
    expect(after.messages.filter(row => row.content === SECOND)).toHaveLength(1)
    await info.attach('after-restart-receipt.json', {
      body: JSON.stringify(after, null, 2),
      contentType: 'application/json'
    })
    await page.screenshot({ path: info.outputPath('after-restart.png') })
  } finally {
    try {
      await info.attach('final-owner-readback.json', {
        body: JSON.stringify(readback(fixture.sandbox.hermesHome), null, 2),
        contentType: 'application/json'
      })
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
    } finally {
      await app.close().catch(() => undefined)
      await fixture.mock.close()
      fixture.sandbox.cleanup()
    }
  }
})
