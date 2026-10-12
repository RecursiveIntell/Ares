import { expect, test } from './test'
import { setupMockBackend, waitForAppReady } from './fixtures'
import { MOCK_REPLY } from './mock-server'

// Actual isolated Electron app.quit(), not a systemd stop or app.close().
// Mock inference is local; this does not qualify active-work quit behavior.
for (const repetition of [1, 2, 3]) {
  test(`idle guarded quit exits normally (${repetition})`, async ({}, testInfo) => {
    const fixture = await setupMockBackend({ skipQuitConfirm: false })
    try {
      await waitForAppReady(fixture, 120_000)
      const environment = await fixture.app.evaluate(() => ({
        home: process.env.HERMES_HOME,
        quitBypass: process.env.HERMES_DESKTOP_SKIP_QUIT_CONFIRM,
      }))
      expect(environment.home).toBe(fixture.sandbox.hermesHome)
      expect(environment.quitBypass).toBe('0')
      const child = fixture.app.process()
      let output = ''
      child.stdout?.on('data', chunk => { output += String(chunk) })
      const exit = new Promise<{ code: number | null; signal: NodeJS.Signals | null }>(resolve => {
        child.once('exit', (code, signal) => resolve({ code, signal }))
      })
      await fixture.app.evaluate(({ app }) => {
        app.once('before-quit', () => process.stdout.write('CLOSURE_BEFORE_QUIT\n'))
        app.once('will-quit', () => process.stdout.write('CLOSURE_WILL_QUIT\n'))
      })
      const closed = fixture.app.waitForEvent('close', { timeout: 15_000 })
      let evaluationError: string | null = null
      await fixture.app.evaluate(({ app }) => app.quit()).catch(error => {
        // Disconnection can race the RPC response; only the independently
        // observed exit and lifecycle markers determine acceptance below.
        evaluationError = String(error)
      })
      await closed
      const observed = await exit
      expect(observed.code).toBe(0)
      expect(observed.signal).toBeNull()
      expect(output).toContain('CLOSURE_BEFORE_QUIT')
      expect(output).toContain('CLOSURE_WILL_QUIT')
      await testInfo.attach('idle-quit-observation.json', {
        body: Buffer.from(JSON.stringify({ repetition, ...observed, evaluationError,
          scope: 'isolated idle dev-Electron guarded quit; not active-work or permanent hang repair' })),
        contentType: 'application/json',
      })
    } finally {
      await fixture.cleanup()
    }
  })
}

// Actual quit-handler integration with a synthetic reported-work snapshot and
// controlled native-dialog responses; not a real operator-task observation.
for (const response of [0, 1]) {
  for (const repetition of [1, 2, 3]) {
    test(`repeated pending quit, response=${response} (${repetition})`, async ({}, testInfo) => {
      test.setTimeout(180_000)
      const fixture = await setupMockBackend({ skipQuitConfirm: false })
      try {
        await waitForAppReady(fixture, 120_000)
        const child = fixture.app.process()
        const exit = new Promise<{ code: number | null; signal: NodeJS.Signals | null }>(resolve => {
          child.once('exit', (code, signal) => resolve({ code, signal }))
        })
        await fixture.app.evaluate(({ app, BrowserWindow, ipcMain, dialog }) => {
          const state = { calls: 0, resolve: undefined as undefined | ((response: number) => void) }
          Object.assign(globalThis, { closureQuitState: state })
          dialog.showMessageBox = () => new Promise<{ response: number; checkboxChecked: boolean }>(resolve => {
            state.calls += 1
            state.resolve = response => resolve({ response, checkboxChecked: false })
          })
          ipcMain.emit('hermes:active-work', { sender: BrowserWindow.getAllWindows()[0].webContents },
            { count: 1, titles: ['Isolated reported-work fixture'] })
          app.quit()
          app.quit()
        })
        const prompts = await fixture.app.evaluate(() => (globalThis as unknown as {
          closureQuitState: { calls: number }
        }).closureQuitState.calls)
        expect(prompts).toBe(1)
        const closed = response === 1 ? fixture.app.waitForEvent('close', { timeout: 15_000 }) : null
        await fixture.app.evaluate((_, response) => {
          (globalThis as unknown as { closureQuitState: { resolve: (response: number) => void } })
            .closureQuitState.resolve(response)
        }, response).catch(error => {
          if (response !== 1) throw error
        })
        if (closed) {
          await closed
          const observed = await exit
          expect(observed.code).toBe(0)
          expect(observed.signal).toBeNull()
        } else {
          const composer = fixture.page.locator('[contenteditable="true"]').first()
          await composer.click()
          await composer.type('Fixture request after Keep Running', { delay: 10 })
          await fixture.page.keyboard.press('Enter')
          await fixture.page.waitForFunction(reply => document.body.textContent?.includes(reply), MOCK_REPLY,
            { timeout: 60_000 })
          expect(child.exitCode).toBeNull()
        }
        await testInfo.attach('reported-work-quit-observation.json', {
          body: Buffer.from(JSON.stringify({ repetition, response, prompts,
            scope: 'real isolated quit handler; synthetic reported work and local mock inference' })),
          contentType: 'application/json',
        })
      } finally {
        await fixture.cleanup()
      }
    })
  }
}
