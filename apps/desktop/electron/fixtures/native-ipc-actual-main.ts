import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createHash, randomBytes } from 'node:crypto'
import fs from 'node:fs'
import { createServer } from 'node:http'
import os from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import vm from 'node:vm'

import { app, BrowserWindow, ipcMain, session, webContents } from 'electron'
import ts from 'typescript'
import MainWebSocket from 'ws'

import { BackendDialClaims, resolveRegistryDialOptions } from '../backend-dial-claim'
import { privilegedRequestHeaders, registerOwnedBackendAdmission } from '../backend-request-admission'
import { backendScopeKey } from '../connection-registry'
import { adoptServedDashboardToken } from '../dashboard-token'
import { isAppDocument } from '../guest-policy'
import { bindNativeBackendPromise, claimNativeBackendLifetime } from '../native-backend-lifetime'
import { prepareNativeSocketTarget } from '../native-socket-target'
import { NativeSocketTransport } from '../native-socket-transport'
import { createRemoteWsHeaderStore } from '../remote-ws-headers'
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'native-ipc-actual-'))
const children: ReturnType<typeof spawn>[] = []
const fixtureTimers = new Set<ReturnType<typeof setTimeout>>()
const nativeBackendProofs = new Map()
const privilegedAppContents = new Set<number>()
const effects = { claims: 0, dials: 0, opens: 0, messages: 0, closes: 0, denials: 0, reaped: 0 }

const frames: any[] = [],
  workerRequests: any[] = []

const hashes: Record<string, string> = {}

let expected = '',
  transport: NativeSocketTransport

app.disableHardwareAcceleration()
// Fixture owns termination after tracked-child reap and receipt publication.
// Electron otherwise quits when finally closes the last window, before stop().
app.on('window-all-closed', () => {})

function hash(file: string) {
  return createHash('sha256').update(fs.readFileSync(file)).digest('hex')
}

function extract() {
  const file = 'electron/main.ts',
    text = fs.readFileSync(file, 'utf8')

  hashes[file] = hash(file)
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true)

  const names = [
    'trustedAppContents',
    'resolveNativeTransportBackend',
    'claimBackend',
    'claimRegistryBackend',
    'headersForRemoteRequest',
    'installRemoteHeaderRules'
  ]

  const nodes = source.statements.filter(
    node =>
      (ts.isFunctionDeclaration(node) && names.includes(node.name?.text || '')) ||
      (ts.isVariableStatement(node) &&
        node.declarationList.declarations.some(d => d.name.getText(source) === 'nativeSocketTransport')) ||
      (ts.isForOfStatement(node) && node.getText(source).includes('hermes:transport:'))
  )

  assert.equal(nodes.length, 8)
  hashes['main-extracted-statements'] = createHash('sha256')
    .update(nodes.map(n => n.getText(source)).join('\n'))
    .digest('hex')

  return ts.transpile(
    nodes.map(n => n.getText(source)).join('\n') +
      '\n installRemoteHeaderRules(); globalThis.actualTransport = nativeSocketTransport;',
    { target: ts.ScriptTarget.ES2022 }
  )
}

async function js(win: BrowserWindow, code: string) {
  return win.webContents.executeJavaScript(code)
}

const request = (correlation: string) => ({ correlation, route: { kind: 'primary' }, channel: { kind: 'gateway' } })

async function deny(win: BrowserWindow, operation: string, payload: any) {
  assert.equal(
    await js(win, `hermesDesktop.nativeSocket.${operation}(${JSON.stringify(payload)}).then(()=>false,()=>true)`),
    true
  )
  effects.denials++
}

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
  registerOwnedBackendAdmission(nativeBackendProofs, base, child as any, identity)

  return {
    base,
    child,
    identity,
    token,
    url: base.replace('http:', 'ws:') + '/api/events?channel=p2-inert&token=' + encodeURIComponent(token)
  }
}

async function run() {
  assert.ok(process.env.P2_FIXTURE_PYTHON)
  assert.equal(app.commandLine.hasSwitch('no-sandbox'), false)
  await app.whenReady()

  const primary = await ownedBackend(),
    pool = await ownedBackend()

  const descriptor = (b: any, profile: string) => ({ baseUrl: b.base, token: b.token, mode: 'local', profile })
  // MOCKED OWNERS: canonical cache/config only. No fabricated credential response.
  let primaryPromise = Promise.resolve(descriptor(primary, 'default'))
  const backendPool = new Map([['pool', { connectionPromise: Promise.resolve(descriptor(pool, 'pool')) }]])
  const config: any = { mode: 'local', profiles: {} }
  let release: (() => void) | undefined, gate: Promise<void> | undefined
  const backendDialClaims = new BackendDialClaims()

  const context: any = {
    URL,
    NativeSocketTransport,
    prepareNativeSocketTarget,
    claimNativeBackendLifetime,
    MainWebSocket,
    trustedAppContents: undefined,
    privilegedAppContents,
    isAppDocument,
    appDocumentUrl: () => expected,
    backendDialClaims,
    backendScopeKey,
    resolveRegistryDialOptions,
    primaryProfileKey: () => 'default',
    globalRemoteActive: () => false,
    profileHasRemoteOverride: () => false,
    readDesktopConnectionConfig: () => config,
    readDesktopConnectionsRegistry: () => ({ primary: 'local', connections: [{ id: 'local', kind: 'local' }] }),
    ensureBackend: async (profile: string) => {
      effects.claims++

      if (gate) {
        await gate
      }

      return profile === 'pool'
        ? bindNativeBackendPromise(
            backendPool.get('pool')!.connectionPromise,
            () => backendPool.get('pool')?.connectionPromise || null
          )
        : bindNativeBackendPromise(primaryPromise, () => primaryPromise)
    },
    ensureRegistryBackend: async () => {
      effects.claims++

      return bindNativeBackendPromise(primaryPromise, () => primaryPromise)
    },
    backendConnectionState: { getPromise: () => primaryPromise },
    backendPool,
    poolStopper: { inFlight: () => false },
    nativeBackendProofs,
    sshConnections: new Map(),
    remoteHeaderRulesInstalled: false,
    electronWebContents: webContents,
    privilegedRequestHeaders,
    remoteWsHeaderStore: createRemoteWsHeaderStore(),
    modeIsRemoteLike: () => false,
    session: {
      defaultSession: {
        resolveProxy: (url: string) => session.defaultSession.resolveProxy(url),
        webRequest: {
          onBeforeSendHeaders: (listener: any) => {
            session.defaultSession.webRequest.onBeforeSendHeaders((details, callback) =>
              listener(details, (result: any) => {
                if (details.resourceType === 'webSocket') {
                  workerRequests.push({
                    urlPath: new URL(details.url).pathname,
                    origin: result.requestHeaders?.Origin,
                    frameUrl: details.frame?.url ?? null,
                    frameProcessId: details.frame?.processId ?? null,
                    owningMainFrame: details.frame === webContents.fromId(details.webContentsId!)?.mainFrame,
                    webContentsId: details.webContentsId,
                    proxy: !!result.requestHeaders?.['x-synthetic-proxy']
                  })
                }

                callback(result)
              })
            )
          }
        }
      }
    },
    ipcMain: {
      handle: (channel: string, listener: any) =>
        ipcMain.handle(channel, (event, value) => {
          frames.push({
            channel,
            contents: event.sender.id,
            url: event.senderFrame?.url,
            top: event.senderFrame === event.sender.mainFrame,
            processId: event.senderFrame?.processId,
            routingId: event.senderFrame?.routingId
          })

          return listener(event, value)
        })
    },
    mintGatewayWsTicket: () => {
      throw new Error('OAuth outside witness')
    }
  }

  // Count actual ws construction without altering responses or sender frames.
  context.MainWebSocket = class extends MainWebSocket {
    constructor(...args: any[]) {
      super(...(args as [any, any]))
      effects.dials++
    }
  }
  vm.runInNewContext(extract(), context)
  transport = context.actualTransport
  ipcMain.on('hermes:translucency:support', event => {
    event.returnValue = {}
  })
  ipcMain.on('hermes:hud:windowing', event => {
    event.returnValue = {}
  })
  const renderer = fs.readFileSync(process.env.NATIVE_RENDERER_BUNDLE!, 'utf8')
  const peerRows: any[] = []
  const peer = createServer((_req, res) => res.end('<html>hostile frame</html>'))
  peer.on('upgrade', (req, socket) => {
    peerRows.push({ origin: req.headers.origin, proxy: !!req.headers['x-synthetic-proxy'] })
    socket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n')
  })
  await new Promise<void>(resolve => peer.listen(0, '127.0.0.1', resolve))
  const peerBase = `http://127.0.0.1:${(peer.address() as any).port}`
  const html = `<html><iframe src="${peerBase}/frame"></iframe></html>`
  const dev = createServer((_req, res) => res.end(html))
  await new Promise<void>(resolve => dev.listen(0, '127.0.0.1', resolve))
  const file = path.join(root, 'index.html')
  fs.writeFileSync(file, html)

  // Production installRemoteHeaderRules owns the real listener above; no fixture substitute.
  const make = (trusted: boolean) => {
    const win = new BrowserWindow({
      show: false,
      webPreferences: {
        sandbox: true,
        contextIsolation: true,
        nodeIntegration: false,
        offscreen: true,
        preload: process.env.NATIVE_PRELOAD_BUNDLE!
      }
    })

    if (trusted) {
      privilegedAppContents.add(win.webContents.id)
    }

    transport.register(win.webContents)

    return win
  }

  const win = make(true),
    other = make(true),
    guest = make(false)

  try {
    for (const mode of ['file', 'dev']) {
      expected = mode === 'file' ? pathToFileURL(file).toString() : `http://127.0.0.1:${(dev.address() as any).port}/`
      await Promise.all([win.loadURL(expected), other.loadURL(expected), guest.loadURL(expected)])
      await js(win, renderer)

      for (const route of [{ kind: 'primary' }, { kind: 'profile', profile: 'pool' }]) {
        const result = await js(
          win,
          `new Promise((resolve,reject)=>{
          const s=new NativeActual.NativeRendererSocket(hermesDesktop.nativeSocket,${JSON.stringify(route)});
          const asynchronous=s.readyState===0, ids=[];
          const timer=setTimeout(()=>reject(Error('RPC deadline')),10000);
          s.onopen=()=>{s.send(JSON.stringify({jsonrpc:'2.0',id:11,method:'native.actual.unknown'}));s.send(JSON.stringify({jsonrpc:'2.0',id:12,method:'native.actual.unknown'}))};
          s.onmessage=e=>{const row=JSON.parse(e.data);if(row.id){ids.push(row.id);if(ids.length===2)s.close(1000)}};
          s.onerror=()=>reject(Error('adapter error'));
          s.onclose=e=>{clearTimeout(timer);resolve({asynchronous,ids,code:e.code,state:s.readyState})};
        })`
        )

        assert.deepEqual(result.ids, [11, 12])
        assert.equal(result.asynchronous, true)
        assert.equal(result.state, 3)
        assert.equal(result.code, 1000)
        effects.opens++
        effects.messages += 2
        effects.closes++
      }

      const before = effects.claims
      await deny(guest, 'open', request('guest'))
      await deny(win, 'open', { ...request('extra'), url: primary.url, headers: {} })
      await deny(win, 'send', { handle: 'forged', data: 'inert' })
      const hostile = win.webContents.mainFrame.frames.find(f => f.url.startsWith(peerBase))
      assert.ok(hostile)
      assert.equal(await hostile.executeJavaScript('typeof window.hermesDesktop'), 'undefined')
      assert.equal(effects.claims, before)
      const handle = await js(win, `hermesDesktop.nativeSocket.open(${JSON.stringify(request('owned'))})`)
      assert.deepEqual(Object.keys(handle), ['handle'])
      await deny(other, 'send', { handle: handle.handle, data: 'inert' })
      await new Promise<void>(resolve => {
        win.webContents.once('did-finish-load', () => resolve())
        win.reload()
      })
      await deny(win, 'send', { handle: handle.handle, data: 'inert' })
      const worker = `onmessage=e=>{const s=new WebSocket(e.data);s.onopen=()=>postMessage('unexpected-open');s.onerror=()=>postMessage('rejected')}`
      assert.equal(
        await js(
          win,
          `new Promise(resolve=>{const w=new Worker(URL.createObjectURL(new Blob([${JSON.stringify(worker)}],{type:'text/javascript'})));const t=setTimeout(()=>{w.terminate();resolve('deadline')},5000);w.onmessage=e=>{clearTimeout(t);w.terminate();resolve(e.data)};w.postMessage(${JSON.stringify(primary.url)})})`
        ),
        'rejected'
      )
      assert.equal(
        await js(
          win,
          `new Promise(resolve=>{const s=new WebSocket(${JSON.stringify(peerBase.replace('http:', 'ws:') + '/worker?token=synthetic-bearer')});s.onerror=()=>resolve('rejected');s.onopen=()=>resolve('unexpected-open')})`
        ),
        'rejected'
      )
    }

    // A pending claim is invalidated by real navigation before credentials/dial.
    gate = new Promise<void>(resolve => {
      release = resolve
    })
    const beforeDial = effects.dials
    await js(win, `hermesDesktop.nativeSocket.open(${JSON.stringify(request('pending'))})`)
    await new Promise<void>(resolve => {
      win.webContents.once('did-finish-load', () => resolve())
      win.reload()
    })
    release!()
    gate = undefined
    await new Promise(resolve => setTimeout(resolve, 300))
    assert.equal(effects.dials, beforeDial, 'navigation must retire pending open before dial')
    await win.loadURL(expected)
    await js(win, renderer)
    const oldLifetime = await context.resolveNativeTransportBackend({ kind: 'primary' }, () => {})
    primaryPromise = Promise.resolve(descriptor(primary, 'default'))
    assert.equal(oldLifetime.alive(), false)
    const fresh = await context.resolveNativeTransportBackend({ kind: 'primary' }, () => {})
    assert.equal(fresh.alive(), true)
    backendPool.get('pool')!.connectionPromise = Promise.resolve(descriptor(pool, 'pool'))
    assert.equal(fresh.alive(), true)
    const pcm = await js(win, 'Array.from(new Uint8Array(new Int16Array([0x1234,-2]).buffer))')
    assert.deepEqual(pcm, [52, 18, 254, 255])
    assert.ok(workerRequests.length >= 4)
    assert.ok(workerRequests.every(row => row.origin !== primary.base && row.origin !== pool.base && !row.proxy))
    assert.ok(peerRows.every(row => row.origin !== peerBase && !row.proxy))

    for (const f of [
      'electron/preload.ts',
      'src/api/native-socket.ts',
      'electron/native-socket-transport.ts',
      'electron/native-socket-target.ts',
      'electron/native-backend-lifetime.ts',
      'electron/backend-dial-claim.ts',
      'electron/fixtures/p2-bootstrap-backend.py'
    ]) {
      hashes[f] = hash(f)
    }

    return {
      normalSandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      actualOwnedPythonBackends: 2,
      ownerMocks: 'canonical child caches and local configuration; ensureBackend factories',
      effects,
      frames,
      workerRequests,
      peerRows,
      hashes,
      versions: process.versions,
      wsVersion: process.env.NATIVE_WS_VERSION,
      pcm,
      wholeDesktopApplicationQualification: false,
      pendingDialBaseline: beforeDial
    }
  } finally {
    win.destroy()
    other.destroy()
    guest.destroy()
    peer.closeAllConnections()
    dev.closeAllConnections()
    peer.close()
    dev.close()
  }
}

async function stop() {
  for (const timer of fixtureTimers) {
    clearTimeout(timer)
  }

  await Promise.all(
    children.map(
      child =>
        new Promise<void>(resolve => {
          if (child.exitCode !== null) {
            effects.reaped++
            resolve()

            return
          }

          const timer = setTimeout(() => child.kill('SIGKILL'), 8000)
          child.once('exit', () => {
            clearTimeout(timer)
            effects.reaped++
            resolve()
          })
          child.kill('SIGTERM')
        })
    )
  )
}

const evidenceDir = process.env.P1A_EVIDENCE_DIR || '/evidence'

run()
  .then(async receipt => {
    await stop()
    assert.equal(effects.reaped, 2)
    fs.mkdirSync(evidenceDir, { recursive: true })
    fs.writeFileSync(path.join(evidenceDir, 'native-ipc-actual.json'), JSON.stringify(receipt, null, 2))
    app.exit(0)
  })
  .catch(async error => {
    console.error('NativeIPCActual1 failed:', error.message)
    await stop()
    fs.mkdirSync(evidenceDir, { recursive: true })
    fs.writeFileSync(
      path.join(evidenceDir, 'native-ipc-actual-failure.json'),
      JSON.stringify({ error: error.message, effects, frames, workerRequests }, null, 2)
    )
    app.exit(1)
  })
