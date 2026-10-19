import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createHash, randomBytes } from 'node:crypto'
import fs from 'node:fs'
import { createServer } from 'node:http'
import os from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import vm from 'node:vm'

import { app, BrowserWindow, session, webContents } from 'electron'
import ts from 'typescript'

import { privilegedRequestHeaders, registerOwnedBackendAdmission } from '../backend-request-admission'
import { adoptServedDashboardToken } from '../dashboard-token'
import { isAppDocument } from '../guest-policy'
import { createRemoteWsHeaderStore } from '../remote-ws-headers'

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'p2-admission-native-'))
const children: ReturnType<typeof spawn>[] = []
let expected = ''
let phase = 'setup'
const rows: any[] = []
const admissions = new Map()
const privileged = new Set<number>()
const remembered = createRemoteWsHeaderStore()
const fixtureTimers = new Set<ReturnType<typeof setTimeout>>()
app.disableHardwareAcceleration()

function progress(stage: string) {
  console.log('P2 admission stage:', stage)
  fs.writeFileSync('/evidence/native-admission-stage.json', JSON.stringify({ stage, phase, rows }, null, 2))
}

async function bounded<T>(promise: Promise<T>, label: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined

  return await Promise.race([
    promise,
    new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => reject(new Error('native admission step deadline: ' + label)), 10000)
    })
  ]).finally(() => {
    if (timer) {clearTimeout(timer)}
  })
}

progress('loaded')

async function ownedBackend() {
  const key = randomBytes(32).toString('base64url')
  const token = randomBytes(32).toString('base64url')
  const identity = randomBytes(16).toString('hex')
  const home = fs.mkdtempSync(path.join(root, 'child-'))

  const child = spawn(
    process.env.P2_FIXTURE_PYTHON!,
    ['-I', '-B', path.resolve('electron/fixtures/p2-bootstrap-backend.py')],
    {
      stdio: ['ignore', 'pipe', 'pipe'],
      env: {
        PATH: process.env.PATH,
        HOME: home,
        TMPDIR: os.tmpdir(),
        HERMES_HOME: path.join(home, '.hermes'),
        ARES_HOME: path.join(home, '.ares'),
        HERMES_SERVE_HEADLESS: '1',
        HERMES_DESKTOP_BOOTSTRAP_KEY: key,
        HERMES_DESKTOP_BOOTSTRAP_ID: identity,
        HERMES_DASHBOARD_SESSION_TOKEN: token
      }
    }
  )

  children.push(child)
  let stderr = ''
  child.stderr!.on('data', value => {
    stderr = (stderr + String(value)).slice(-8192)
  })

  const port = await new Promise<number>((resolve, reject) => {
    let text = ''
    const timer = setTimeout(() => reject(new Error('owned ready deadline: ' + stderr)), 15000)
    fixtureTimers.add(timer)
    child.once('error', reject)
    child.once('exit', () => {
      clearTimeout(timer)
      fixtureTimers.delete(timer)
      reject(new Error('owned child early exit: ' + stderr))
    })
    child.stdout!.on('data', value => {
      text += String(value)

      if (text.length > 8192) {
        reject(new Error('readiness overflow'))

        return
      }

      for (const line of text.split('\n').slice(0, -1)) {
        try {
          const row = JSON.parse(line)

          if (row.pid === child.pid && row.identity === identity && Number.isInteger(row.port)) {
            clearTimeout(timer)
            fixtureTimers.delete(timer)
            resolve(row.port)
          }
        } catch {
          /* only exact owned JSON is readiness */
        }
      }
    })
  })

  const base = `http://127.0.0.1:${port}`
  assert.equal(
    await adoptServedDashboardToken(base, token, {
      bootstrapKey: key,
      identity,
      childAlive: () => child.exitCode === null && !child.killed
    }),
    token
  )
  registerOwnedBackendAdmission(admissions, base, child as any, identity)

  return {
    base,
    child,
    identity,
    token,
    url: base.replace('http:', 'ws:') + '/api/events?channel=p2-inert&token=' + encodeURIComponent(token)
  }
}

function functionsFromMain() {
  const text = fs.readFileSync('electron/main.ts', 'utf8')
  const source = ts.createSourceFile('main.ts', text, ts.ScriptTarget.Latest, true)

  return ['trustedAppContents', 'headersForRemoteRequest', 'installRemoteHeaderRules'].map(name => {
    const node = source.statements.find(row => ts.isFunctionDeclaration(row) && row.name?.text === name)
    assert.ok(node, 'production function missing: ' + name)

    return { name, text: node.getText(source) }
  })
}

const socketScript = (url: string) => `new Promise(resolve => {
  const socket=new WebSocket(${JSON.stringify(url)});
  const timer=setTimeout(()=>{socket.close();resolve(false)},4000);
  socket.onopen=()=>{clearTimeout(timer);socket.close(1000);resolve(true)};
  socket.onerror=()=>{clearTimeout(timer);resolve(false)};
})`

async function run() {
  assert.ok(process.env.P2_FIXTURE_PYTHON, 'actual isolated Python required')
  assert.equal(app.commandLine.hasSwitch('no-sandbox'), false)
  await app.whenReady()
  progress('app-ready')
  const primary = await ownedBackend()
  progress('primary-ready')
  const pool = await ownedBackend()
  progress('pool-ready')
  const peerRows: any[] = []

  const peer = createServer((_req, res) => {
    res.end('<html><body>inert</body></html>')
  })

  peer.on('upgrade', (req, socket) => {
    peerRows.push({ phase, origin: req.headers.origin, proxy: req.headers['x-synthetic-proxy'] })
    socket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n')
  })
  await new Promise<void>(resolve => peer.listen(0, '127.0.0.1', resolve))
  const address = peer.address()
  assert.ok(address && typeof address !== 'string')
  const peerOrigin = `http://127.0.0.1:${address.port}`

  const dev = createServer((_req, res) => {
    res.end(`<html><body><iframe src="${peerOrigin}/frame"></iframe></body></html>`)
  })

  await new Promise<void>(resolve => dev.listen(0, '127.0.0.1', resolve))
  const devAddress = dev.address()
  assert.ok(devAddress && typeof devAddress !== 'string')
  const devOrigin = `http://127.0.0.1:${devAddress.port}`
  const file = path.join(root, 'index.html')
  fs.writeFileSync(file, `<html><body><iframe src="${peerOrigin}/frame"></iframe></body></html>`)
  const prod = functionsFromMain()

  const context = {
    remoteHeaderRulesInstalled: false,
    session: {
      defaultSession: {
        webRequest: {
          onBeforeSendHeaders: listener => {
            session.defaultSession.webRequest.onBeforeSendHeaders((details, callback) =>
              listener(details, result => {
                if (details.resourceType === 'webSocket')
                  {rows.push({
                    phase,
                    mainFrame: details.frame === webContents.fromId(details.webContentsId!)?.mainFrame,
                    rewritten: result.requestHeaders?.Origin === new URL(details.url.replace(/^ws/, 'http')).origin,
                    rawSecretHeader: !!result.requestHeaders?.['X-Hermes-Native-Bootstrap']
                  })}

                fs.writeFileSync('/evidence/native-admission-progress.json', JSON.stringify({ phase, rows }, null, 2))
                callback(result)
              })
            )
          }
        }
      }
    },
    electronWebContents: webContents,
    nativeBackendProofs: admissions,
    privilegedAppContents: privileged,
    isAppDocument,
    appDocumentUrl: () => expected,
    remoteWsHeaderStore: remembered,
    privilegedRequestHeaders,
    readDesktopConnectionConfig: () => ({ mode: 'local' }),
    modeIsRemoteLike: () => false
  }

  vm.runInNewContext(prod.map(row => row.text).join('\n') + '\ninstallRemoteHeaderRules();', context)
  progress('production-hooks-installed')

  const win = new BrowserWindow({
    show: false,
    webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false, offscreen: true }
  })

  privileged.add(win.webContents.id)
  win.webContents.once('destroyed', () => privileged.delete(win.webContents.id))

  try {
    for (const mode of ['file', 'dev']) {
      expected = mode === 'file' ? pathToFileURL(file).toString() : devOrigin + '/'
      progress(mode + '-load-start')
      await bounded(win.loadURL(expected), mode + '-load')
      progress(mode + '-loaded')

      for (const target of [primary, pool]) {
        phase = mode + (target === primary ? '-primary' : '-pool')
        assert.equal(await bounded(win.webContents.executeJavaScript(socketScript(target.url)), phase), true, phase)
        progress(phase + '-passed')
      }

      phase = mode + '-subframe'
      const subframe = win.webContents.mainFrame.frames.find(frame => frame.url.startsWith(peerOrigin))
      assert.ok(subframe, 'real hostile subframe required')
      assert.equal(await bounded(subframe.executeJavaScript(socketScript(primary.url)), phase), false, phase)
      progress(phase + '-passed')
      phase = mode + '-worker'
      const worker = `onmessage=async event=>{const url=event.data;const socket=new WebSocket(url);const timer=setTimeout(()=>{socket.close();postMessage(false)},4000);socket.onopen=()=>{clearTimeout(timer);socket.close(1000);postMessage(true)};socket.onerror=()=>{clearTimeout(timer);postMessage(false)}}`
      assert.equal(
        await bounded(
          win.webContents.executeJavaScript(
            `new Promise(resolve=>{const worker=new Worker(URL.createObjectURL(new Blob([${JSON.stringify(worker)}],{type:'application/javascript'})));worker.onerror=()=>{worker.terminate();resolve('construction-denied')};worker.onmessage=event=>{worker.terminate();resolve(event.data)};worker.postMessage(${JSON.stringify(primary.url)})})`
          ),
          phase
        ),
        false,
        phase
      )
      progress(phase + '-passed')
      phase = mode + '-unknown-destination'
      assert.equal(
        await win.webContents.executeJavaScript(socketScript(peerOrigin.replace('http:', 'ws:') + '/unadmitted')),
        false
      )
    }

    phase = 'remembered-token-reconnect'
    admissions.delete(primary.base)
    remembered.remember(primary.url, {})
    assert.equal(await win.webContents.executeJavaScript(socketScript(primary.url)), true)
    assert.equal(await win.webContents.executeJavaScript(socketScript(primary.url)), true)
    phase = 'guest'

    const guest = new BrowserWindow({
      show: false,
      webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false, offscreen: true }
    })

    try {
      await guest.loadURL(expected)
      assert.equal(await guest.webContents.executeJavaScript(socketScript(pool.url)), false)
    } finally {
      guest.destroy()
    }

    assert.ok(rows.length)
    assert.ok(rows.every(row => !row.rawSecretHeader))
    assert.ok(
      rows.filter(row => /subframe|worker|guest|unknown-destination/.test(row.phase)).every(row => !row.rewritten)
    )
    assert.ok(peerRows.every(row => row.origin !== peerOrigin && !row.proxy))
    fs.writeFileSync(
      '/evidence/native-admission.json',
      JSON.stringify(
        {
          normalSandbox: true,
          actualOwnedPythonBackends: 2,
          actualElectronFrames: true,
          productionFunctions: prod.map(row => ({
            name: row.name,
            sha256: createHash('sha256').update(row.text).digest('hex')
          })),
          rows,
          unknownDestinationReceivers: peerRows,
          wholeDesktopApplicationQualification: false
        },
        null,
        2
      )
    )
  } finally {
    win.destroy()
    peer.closeAllConnections()
    dev.closeAllConnections()
    await Promise.all([
      new Promise<void>(resolve => peer.close(() => resolve())),
      new Promise<void>(resolve => dev.close(() => resolve()))
    ])
  }
}

async function stop() {
  for (const timer of fixtureTimers) {clearTimeout(timer)}
  await Promise.all(
    children.map(
      child =>
        new Promise<void>(resolve => {
          if (child.exitCode !== null) {
            resolve()

            return
          }

          const timer = setTimeout(() => child.kill('SIGKILL'), 8000)
          child.once('exit', () => {
            clearTimeout(timer)
            resolve()
          })
          child.kill('SIGTERM')
        })
    )
  )
}

run()
  .then(async () => {
    await stop()
    app.exit(0)
  })
  .catch(async error => {
    console.error(error)
    await stop()
    app.exit(1)
  })
