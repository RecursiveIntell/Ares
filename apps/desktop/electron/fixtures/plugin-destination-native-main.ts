// Native RIC-01 fixture: genuine ipcMain/ipcRenderer transport, production
// handler/installer imports, shared adversarial cases, fake git, no plugin eval.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { app, BrowserWindow, ipcMain } from 'electron'

import { registerFsIpc } from '../fs-ipc'

import { DESTINATION_REVISION, destinationCases, exerciseDestinationCase } from './plugin-destination-cases'
import { exerciseRevisionCases } from './plugin-revision-cases'

const evidence = process.env.P1A_EVIDENCE_DIR!
const fixtureNode = process.env.P1A_FIXTURE_NODE!
const results: { label: string; passed: boolean; error?: string }[] = []
const channels = new Set<string>()
const originalHandle = ipcMain.handle.bind(ipcMain)

// Re-register genuine handlers for each disposable home; transport is never mocked.
ipcMain.handle = (channel, handler) => {
  if (channels.has(channel)) {
    ipcMain.removeHandler(channel)
  }

  channels.add(channel)
  originalHandle(channel, handler)
}

app.disableHardwareAcceleration()
app.on('window-all-closed', () => {})
const deadline = setTimeout(() => app.exit(124), 120_000)

async function run(): Promise<void> {
  assert.ok(path.isAbsolute(evidence) && path.isAbsolute(fixtureNode))
  const browserRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'ric01-browser-'))
  const preload = path.join(browserRoot, 'preload.cjs')
  fs.writeFileSync(
    preload,
    "const {contextBridge,ipcRenderer}=require('electron'); contextBridge.exposeInMainWorld('ric01',{install:p=>ipcRenderer.invoke('hermes:plugin:installDesktop',p),probe:p=>ipcRenderer.invoke('hermes:plugin:probe',p)})"
  )

  const window = new BrowserWindow({
    show: false,
    webPreferences: { preload, contextIsolation: true, sandbox: true, nodeIntegration: false, offscreen: true }
  })

  await window.loadURL('data:text/html,<p>Disposable IPC caller</p>')

  for (const testCase of destinationCases) {
    try {
      const witness = await exerciseDestinationCase(
        testCase,
        async (home, git, identifier, force, profile) => {
          registerFsIpc({
            hermesHome: home,
            readActiveDesktopProfile: () => profile ?? null,
            expandUserPath: value => value,
            resolveRequestedPathForIpc: value => value,
            directoryExists: () => true,
            resolveGitBinary: () => git
          })

          return window.webContents.executeJavaScript(
            `window.ric01.install(${JSON.stringify({ identifier, force, revision: DESTINATION_REVISION })})`
          )
        },
        fixtureNode
      )

      assert.equal(witness.outsideWrites, 0)
      results.push({ label: testCase.label, passed: true })
    } catch (error) {
      results.push({ label: testCase.label, passed: false, error: String(error) })

      break
    }
  }

  let revisions: Awaited<ReturnType<typeof exerciseRevisionCases>> | null = null
  let revisionError: string | null = null

  try {
    revisions = await exerciseRevisionCases(home => {
      registerFsIpc({
        hermesHome: home,
        readActiveDesktopProfile: () => null,
        expandUserPath: value => value,
        resolveRequestedPathForIpc: value => value,
        directoryExists: () => true,
        resolveGitBinary: () => 'git'
      })

      return {
        probe: identifier =>
          window.webContents.executeJavaScript(`window.ric01.probe(${JSON.stringify({ identifier })})`),
        install: (identifier, revision) =>
          window.webContents.executeJavaScript(
            `window.ric01.install(${JSON.stringify({ identifier, revision, force: true })})`
          )
      }
    })
  } catch (error) {
    revisionError = String(error)
  }

  fs.writeFileSync(
    path.join(evidence, 'native-matrix.json'),
    JSON.stringify(
      {
        electron: process.versions.electron,
        chrome: process.versions.chrome,
        expectedCases: destinationCases.length,
        observedCases: results.length,
        rendererSandbox: true,
        contextIsolation: true,
        nodeIntegration: false,
        chromiumSandboxDisabled: process.argv.includes('--no-sandbox'),
        results,
        revisions,
        revisionError,
        passed:
          results.length === destinationCases.length &&
          results.every(result => result.passed) &&
          revisions !== null &&
          revisionError === null
      },
      null,
      2
    )
  )
  window.destroy()
  fs.rmSync(browserRoot, { recursive: true, force: true })
  clearTimeout(deadline)
  app.exit(
    results.length === destinationCases.length &&
      results.every(result => result.passed) &&
      revisions !== null &&
      revisionError === null
      ? 0
      : 1
  )
}

app
  .whenReady()
  .then(run)
  .catch(error => {
    fs.writeFileSync(path.join(evidence, 'native-startup-error.txt'), String(error))
    clearTimeout(deadline)
    app.exit(2)
  })
