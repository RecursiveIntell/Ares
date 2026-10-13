import fs from 'node:fs'
import path from 'node:path'
import type { Page } from '@playwright/test'
import { buildAppEnv, createSandbox, launchDesktop, waitForAppReady, writeEnvFile } from './fixtures'
import { startMockServer } from './mock-server'
import { expect, test } from './test'

async function rpc(page: Page, method: string, params: Record<string, unknown> = {}): Promise<any> {
  return page.evaluate(async ({ method, params }) => {
    const descriptor = await (window as any).hermesDesktop.getGatewayWsUrl('default')
    if (typeof descriptor !== 'string' && !descriptor.ok) { throw Error('synthetic gateway unavailable') }
    const url = typeof descriptor === 'string' ? descriptor : descriptor.wsUrl
    const id = crypto.randomUUID()
    return new Promise((resolve, reject) => {
      const socket = new WebSocket(url)
      const timer = setTimeout(() => { socket.close(); reject(Error('synthetic RPC deadline')) }, 60000)
      let answered = false
      socket.onopen = () => socket.send(JSON.stringify({ jsonrpc: '2.0', id, method, params }))
      socket.onmessage = event => {
        const reply = JSON.parse(String(event.data))
        if (reply.id !== id) { return }
        answered = true; clearTimeout(timer); socket.close()
        reply.error ? reject(Error(JSON.stringify(reply.error))) : resolve(reply.result)
      }
      socket.onerror = () => { clearTimeout(timer); socket.close(); reject(Error('synthetic gateway transport failure')) }
      socket.onclose = () => { if (!answered) { clearTimeout(timer); reject(Error('synthetic RPC closed before reply')) } }
    })
  }, { method, params })
}

let fixture: any

test.beforeAll(async () => {
  const mock = await startMockServer({ models: ['mock-model', 'mock-next'] })
  const sandbox = createSandbox('ui-root-repair')
  fs.writeFileSync(path.join(sandbox.hermesHome, 'config.yaml'), `model:\n  default: mock-model\n  provider: mock\nproviders:\n  mock:\n    api: ${mock.url}/v1\n    name: Mock\n    api_mode: chat_completions\n    key_env: MOCK_API_KEY\n    models:\n      mock-model: {}\n      mock-next: {}\n    context_length: 4096\nagent:\n  reasoning_effort: medium\n`)
  writeEnvFile(sandbox.hermesHome)
  const { app, page } = await launchDesktop(buildAppEnv(sandbox))
  fixture = { app, page, mock, sandbox }
  await waitForAppReady(fixture, 120000)
  // Admit input against the foreground window in this private Xvfb display.
  // Never change throttling or focus the operator's live Desktop as a test fix.
  await app.evaluate(({ BrowserWindow }) => {
    const win = BrowserWindow.getAllWindows().find(window => window.webContents.getURL().includes('/dist/index.html'))
    if (!win) { throw Error('isolated main window missing') }
    win.show(); win.focus(); win.webContents.focus()
  })
  await expect.poll(() => page.evaluate(() => document.hasFocus())).toBe(true)
  await expect.poll(() => page.evaluate(() => document.visibilityState)).toBe('visible')
})
test.afterAll(async () => {
  await fixture?.app.close().catch(() => {})
  await fixture?.mock.close()
  fixture?.sandbox.cleanup()
})

test('first model and effort choices survive real backend readback and another turn', async ({}, testInfo) => {
  test.setTimeout(180000)
  const page: Page = fixture.page
  const composer = page.locator('[data-slot="composer-rich-input"]')
  await composer.click(); await composer.pressSequentially('UI repair synthetic first turn'); await page.keyboard.press('Enter')
  await expect(page.getByText(/Hello from the mock inference server!/).first()).toBeVisible({ timeout: 60000 })
  const active = await rpc(page, 'session.active_list')
  expect(active.sessions).toHaveLength(1)
  const sid = active.sessions[0].id
  const pill = page.getByRole('button', { name: 'Model · mock: mock-model', exact: true })
  await pill.click()
  // The catalog intentionally disables hover until actual pointer movement.
  // Exercise its supported keyboard-first path rather than forcing a click.
  await page.getByRole('textbox', { name: 'Search models' }).fill('mock-next')
  await page.keyboard.press('Enter')
  await expect.poll(async () => (await rpc(page, 'model.options', { session_id: sid })).model).toBe('mock-next')
  const selected = page.getByRole('button', { name: 'Model · mock: mock-next', exact: true })
  await selected.click()
  const row = page.getByRole('menuitem').filter({ hasText: 'Mock Next' }).first()
  await row.scrollIntoViewIfNeeded()
  await page.mouse.move(0, 0)
  await expect.poll(() => row.evaluate(element => getComputedStyle(element).pointerEvents)).toBe('auto')
  await row.hover()
  await expect(row).toHaveAttribute('data-state', 'open')
  const high = page.getByRole('menuitemradio', { name: 'High', exact: true })
  const highBox = await high.boundingBox()
  expect(highBox).not.toBeNull()
  const x = highBox!.x + highBox!.width / 2, y = highBox!.y + highBox!.height / 2
  // Traverse the real Radix hover-grace path, rather than teleporting from the
  // parent row straight into mousedown while its submenu is being repositioned.
  await page.mouse.move(x, y, { steps: 12 })
  await expect.poll(() => page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.closest('[role="menuitemradio"]')?.textContent?.trim(), { x, y })).toBe('High')
  await page.mouse.down(); await page.mouse.up()
  await expect(high).toHaveAttribute('aria-checked', 'true')
  await page.keyboard.press('Escape'); await page.keyboard.press('Escape')
  await expect.poll(async () => (await rpc(page, 'config.get', { session_id: sid, key: 'reasoning' })).value).toBe('high')
  await composer.click(); await composer.pressSequentially('UI repair synthetic second turn'); await page.keyboard.press('Enter')
  await expect.poll(() => fixture.mock.receivedPrompts.some((p: string) => p.includes('synthetic second turn'))).toBe(true)
  await expect.poll(async () => (await rpc(page, 'model.options', { session_id: sid })).model).toBe('mock-next')
  await expect.poll(async () => (await rpc(page, 'config.get', { session_id: sid, key: 'reasoning' })).value).toBe('high')
  await expect(selected).toContainText('High')
  // Capture before runner teardown; its automatic Electron screenshot can be
  // a stale surface even when live DOM/RPC checks have passed.
  await page.screenshot({ path: testInfo.outputPath('native-body-before-teardown.png') })
  const bodyEvidence = await page.evaluate(() => ({ title: document.title, focused: document.hasFocus(), visibility: document.visibilityState,
    models: [...document.querySelectorAll('button[aria-label^="Model ·"]')].map(button => ({ label: button.getAttribute('aria-label'), text: button.textContent })) }))
  fs.writeFileSync(testInfo.outputPath('native-body-dom.json'), JSON.stringify(bodyEvidence, null, 2))
})

test('explicit terminal shortcut focuses terminal, then prompt retains focus during passive work', async () => {
  const page: Page = fixture.page
  const composer = page.locator('[data-slot="composer-rich-input"]')
  await composer.click()
  await page.keyboard.press('Control+Backquote')
  await expect.poll(() => page.evaluate(() => !!document.activeElement?.closest('[data-terminal]'))).toBe(true)
  await composer.click()
  await expect(composer).toBeFocused()
  // A real passive gateway read triggers session metadata/config work but is not focus intent.
  await rpc(page, 'model.options')
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
  await expect(composer).toBeFocused()
})
