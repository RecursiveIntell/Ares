import fs from 'node:fs'
import path from 'node:path'

import type { Page } from '@playwright/test'

import { buildAppEnv, createSandbox, launchDesktop, waitForAppReady, writeEnvFile } from './fixtures'
import { startMockServer } from './mock-server'
import { expect, test } from './test'

const FAST_MODEL = 'gpt-6.1-sol'
const NORMAL_MODEL = 'gpt-5.3-codex'

async function rpc(page: Page, method: string, params: Record<string, unknown> = {}): Promise<any> {
  return page.evaluate(async ({ method, params }) => {
    const descriptor = await (window as any).hermesDesktop.getGatewayWsUrl('default')

    if (typeof descriptor !== 'string' && !descriptor.ok) { throw Error('fixture gateway unavailable') }
    const url = typeof descriptor === 'string' ? descriptor : descriptor.wsUrl
    const id = crypto.randomUUID()

    return new Promise((resolve, reject) => {
      const socket = new WebSocket(url)
      const timer = setTimeout(() => { socket.close(); reject(Error('fixture RPC deadline')) }, 60000)
      let answered = false
      socket.onopen = () => socket.send(JSON.stringify({ jsonrpc: '2.0', id, method, params }))

      socket.onmessage = event => {
        const reply = JSON.parse(String(event.data))

        if (reply.id !== id) { return }
        answered = true; clearTimeout(timer); socket.close()
        reply.error ? reject(Error(JSON.stringify(reply.error))) : resolve(reply.result)
      }

      socket.onerror = () => { clearTimeout(timer); socket.close(); reject(Error('fixture gateway transport failure')) }

      socket.onclose = () => { if (!answered) { clearTimeout(timer); reject(Error('fixture RPC closed before reply')) } }
    })
  }, { method, params })
}

function lastUserText(body: Record<string, unknown>): string {
  const messages = Array.isArray(body.messages) ? body.messages as Array<{ role?: string; content?: unknown }> : []
  const user = [...messages].reverse().find(message => message.role === 'user')

  return typeof user?.content === 'string' ? user.content : ''
}

async function chooseModel(page: Page, sid: string, model: string): Promise<void> {
  await page.getByRole('button', { name: /^Model · mock:/ }).click()
  await page.getByRole('textbox', { name: 'Search models' }).fill(model)
  await page.keyboard.press('Enter')
  await expect.poll(async () => (await rpc(page, 'model.options', { session_id: sid })).model).toBe(model)
}

async function openSelectedOptions(page: Page): Promise<void> {
  await page.getByRole('button', { name: /^Model · mock:/ }).click()
  const row = page.locator('[role="menuitem"][data-kb-active]').first()
  await row.scrollIntoViewIfNeeded()
  await page.mouse.move(0, 0)
  await expect.poll(() => row.evaluate(element => getComputedStyle(element).pointerEvents)).toBe('auto')
  await row.hover()
  await expect(row).toHaveAttribute('data-state', 'open')
}

async function chooseEffort(page: Page, effort: string): Promise<void> {
  await openSelectedOptions(page)
  const item = page.getByRole('menuitemradio', { name: effort, exact: true })
  const box = await item.boundingBox()
  expect(box).not.toBeNull()
  const x = box!.x + box!.width / 2, y = box!.y + box!.height / 2
  await page.mouse.move(x, y, { steps: 12 })
  await expect.poll(() => page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.closest('[role="menuitemradio"]')?.textContent?.trim(), { x, y })).toBe(effort)
  await page.mouse.down(); await page.mouse.up()
  await expect(item).toHaveAttribute('aria-checked', 'true')
  await page.keyboard.press('Escape'); await page.keyboard.press('Escape')
}

/** Select only the compute host descended from this test-owned Electron app. */
function fixtureComputeHost(appPid: number): number {
  const parents = new Map<number, number>()
  const candidates: number[] = []

  for (const entry of fs.readdirSync('/proc')) {
    if (!/^\d+$/.test(entry)) { continue }
    const pid = Number(entry)

    try {
      const status = fs.readFileSync(`/proc/${pid}/status`, 'utf8')
      const parent = status.match(/^PPid:\s+(\d+)/m)

      if (parent) { parents.set(pid, Number(parent[1])) }
      const args = fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8').split('\0')

      if (args.some((arg, index) => arg === '-m' && args[index + 1] === 'tui_gateway.compute_host')) { candidates.push(pid) }
    } catch { /* A process can exit while enumerating. */ }
  }

  const owned = candidates.filter(pid => {
    const seen = new Set<number>()
    let current = pid

    while (current > 1 && !seen.has(current)) {
      if (current === appPid) { return true }
      seen.add(current); current = parents.get(current) ?? 0
    }

    return false
  })

  if (owned.length !== 1) { throw Error(`expected one test-owned compute host, got ${owned.length}`) }

  return owned[0]
}

// eslint-disable-next-line no-empty-pattern -- Playwright requires destructuring; this test owns its isolated Electron fixture.
test('selector intent survives model changes and a real host rebuild with matching wire fields', async ({}, testInfo) => {
  test.skip(process.platform !== 'linux', 'Linux-only scoped compute-host failure injection')
  test.setTimeout(240000)
  const mock = await startMockServer({ models: [FAST_MODEL, NORMAL_MODEL] })
  const sandbox = createSandbox('model-selector-policy')
  fs.writeFileSync(path.join(sandbox.hermesHome, 'config.yaml'), `model:\n  default: ${FAST_MODEL}\n  provider: mock\nproviders:\n  mock:\n    api: ${mock.url}/v1\n    name: Mock\n    api_mode: chat_completions\n    key_env: MOCK_API_KEY\n    models:\n      ${FAST_MODEL}: {}\n      ${NORMAL_MODEL}: {}\n    context_length: 32768\nagent:\n  reasoning_effort: xhigh\n  service_tier: fast\ndashboard:\n  turn_isolation: true\n`)
  writeEnvFile(sandbox.hermesHome)
  const { app, page } = await launchDesktop(buildAppEnv(sandbox))

  const cleanup = async () => {
    await app.close().catch(() => {})
    await mock.close()
    sandbox.cleanup()
  }

  const evidence: Record<string, unknown> = {}

  try {
    await waitForAppReady({ app, page, mock, mockUrl: mock.url, sandbox, cleanup }, 120000)
    await app.evaluate(({ BrowserWindow }) => {
      const win = BrowserWindow.getAllWindows().find(window => window.webContents.getURL().includes('/dist/index.html'))

      if (!win) { throw Error('isolated main window missing') }
      win.show(); win.focus(); win.webContents.focus()
    })
    await expect.poll(() => page.evaluate(() => document.hasFocus())).toBe(true)
    const composer = page.locator('[data-slot="composer-rich-input"]')

    const send = async (prompt: string): Promise<Record<string, unknown>> => {
      await composer.click(); await composer.pressSequentially(prompt); await page.keyboard.press('Enter')
      // Title generation also includes the user prompt, but is not a chat
      // request and intentionally omits chat runtime options.
      const matchesTurn = (body: Record<string, unknown>) => body.stream === true && lastUserText(body).includes(prompt)

      await expect.poll(() => mock.receivedRequests.some(matchesTurn), { timeout: 60000 }).toBe(true)
      await expect.poll(async () => (await rpc(page, 'session.active_list')).sessions.every((session: any) => !session.running), { timeout: 60000 }).toBe(true)

      return mock.receivedRequests.findLast(matchesTurn)!
    }

    const inherited = await send('selector-policy inherited defaults')
    expect(inherited.model).toBe(FAST_MODEL)
    expect(inherited.service_tier).toBe('priority')
    expect(inherited.reasoning_effort).toBe('xhigh')
    const active = await rpc(page, 'session.active_list')
    expect(active.sessions).toHaveLength(1)
    const sid = active.sessions[0].id
    expect((await rpc(page, 'config.get', { session_id: sid, key: 'fast' })).owner).toBe('compute_host')

    await chooseEffort(page, 'Low')
    await expect.poll(async () => (await rpc(page, 'config.get', { session_id: sid, key: 'reasoning' })).value).toBe('low')
    await chooseModel(page, sid, NORMAL_MODEL)
    await expect.poll(async () => (await rpc(page, 'config.get', { session_id: sid, key: 'reasoning' })).value).toBe('low')
    await expect.poll(async () => (await rpc(page, 'config.get', { session_id: sid, key: 'fast' })).value).toBe('normal')
    const normal = await send('selector-policy unsupported fast model')
    expect(normal.model).toBe(NORMAL_MODEL)
    expect(normal).not.toHaveProperty('service_tier')
    expect(normal).not.toHaveProperty('speed')
    expect(normal.reasoning_effort).toBe('low')

    await chooseModel(page, sid, FAST_MODEL)
    await expect.poll(async () => (await rpc(page, 'config.get', { session_id: sid, key: 'reasoning' })).value).toBe('low')
    await expect.poll(async () => (await rpc(page, 'config.get', { session_id: sid, key: 'fast' })).value).toBe('fast')
    await openSelectedOptions(page)
    const fastSwitch = page.getByRole('menuitem').filter({ hasText: /^Fast$/ }).getByRole('switch')
    await expect(fastSwitch).toBeChecked()
    const fastBox = await fastSwitch.boundingBox()
    expect(fastBox).not.toBeNull()
    const fastX = fastBox!.x + fastBox!.width / 2, fastY = fastBox!.y + fastBox!.height / 2
    // Traverse the same real submenu hover-grace path as the effort picker.
    await page.mouse.move(fastX, fastY, { steps: 12 })
    await expect.poll(() => page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.closest('[role="switch"]')?.getAttribute('aria-checked'), { x: fastX, y: fastY })).toBe('true')
    await page.mouse.down(); await page.mouse.up()
    await expect(fastSwitch).not.toBeChecked()
    await page.keyboard.press('Escape'); await page.keyboard.press('Escape')
    await expect.poll(async () => (await rpc(page, 'config.get', { session_id: sid, key: 'fast' })).value).toBe('normal')

    const before = await rpc(page, 'config.get', { session_id: sid, key: 'reasoning' })
    const hostPid = fixtureComputeHost(app.process().pid!)
    evidence.host = { old_pid: hostPid, old_boot: before.host_boot_id, owner: before.owner }
    process.kill(hostPid, 'SIGKILL')
    await expect.poll(() => fs.existsSync(`/proc/${hostPid}`)).toBe(false)
    const rebuilt = await send('selector-policy after host rebuild')
    expect(rebuilt.model).toBe(FAST_MODEL)
    expect(rebuilt).not.toHaveProperty('service_tier')
    expect(rebuilt).not.toHaveProperty('speed')
    expect(rebuilt.reasoning_effort).toBe('low')
    const after = await rpc(page, 'config.get', { session_id: sid, key: 'reasoning' })
    expect(after.value).toBe('low')
    expect(after.host_boot_id).not.toBe(before.host_boot_id)
    evidence.after = after
    evidence.requests = mock.receivedRequests
    await page.screenshot({ path: testInfo.outputPath('selector-policy-native.png') })
    fs.writeFileSync(testInfo.outputPath('selector-policy-wire.json'), JSON.stringify(evidence, null, 2))
  } finally {
    evidence.requests = mock.receivedRequests
    fs.writeFileSync(testInfo.outputPath('selector-policy-attempt.json'), JSON.stringify(evidence, null, 2))
    const logs = path.join(sandbox.hermesHome, 'logs')

    if (fs.existsSync(logs)) { fs.cpSync(logs, testInfo.outputPath('fixture-backend-logs'), { recursive: true }) }
    await cleanup()
  }
})
