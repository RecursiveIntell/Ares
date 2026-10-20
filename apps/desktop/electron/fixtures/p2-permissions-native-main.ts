// Real Electron REQUEST/CHECK API calls, fake devices, synthetic home, inert
// loopback origins. This fixture does not import/start the installed app.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import http from 'node:http'

import { app, BrowserWindow, session } from 'electron'

import { admitPreviewGuest, installSessionPermissions } from '../guest-policy'

app.commandLine.appendSwitch('use-fake-device-for-media-stream')
app.disableHardwareAcceleration()
const witness: any[] = []
const surfaces: any[] = []
const guarded = new WeakSet<object>()
let popupRequests = 0
let privileged
let appOrigin = ''

function guard(sess) {
  if (guarded.has(sess)) {
    return
  }

  guarded.add(sess)
  const requestSetter = sess.setPermissionRequestHandler.bind(sess)
  const checkSetter = sess.setPermissionCheckHandler.bind(sess)
  sess.setPermissionRequestHandler = handler =>
    requestSetter((contents, permission, callback, details) =>
      handler(
        contents,
        permission,
        allowed => {
          witness.push({ rail: 'REQUEST', permission, allowed, main: details.isMainFrame, url: details.requestingUrl })
          callback(allowed)
        },
        details
      )
    )
  sess.setPermissionCheckHandler = handler =>
    checkSetter((contents, permission, origin, details) => {
      const allowed = handler(contents, permission, origin, details)
      witness.push({ rail: 'CHECK', permission, allowed, main: details.isMainFrame, url: details.requestingUrl })

      return allowed
    })
  installSessionPermissions(
    sess,
    contents => contents === privileged,
    () => appOrigin
  )
}

app.on('session-created', guard)
app.on('web-contents-created', (_event, contents) => {
  guard(contents.session)
  contents.on('will-attach-webview', (event, prefs, params) => {
    admitPreviewGuest(event, prefs, params, () => session.fromPartition('persist:hermes-preview'))
  })

  if (contents.getType() === 'webview') {
    contents.setWindowOpenHandler(() => {
      popupRequests += 1

      return { action: 'deny' }
    })
  }
})

const apiScript = `window.tryApi = async kind => {
  try {
    if (kind === 'media') { const s = await navigator.mediaDevices.getUserMedia({audio:true}); s.getTracks().forEach(t=>t.stop()); return 'allowed'; }
    if (kind === 'geolocation') return await new Promise(r=>navigator.geolocation.getCurrentPosition(()=>r('allowed'),e=>r('denied:'+e.code),{timeout:1000}));
    if (kind === 'notifications') return await Notification.requestPermission();
    if (kind === 'clipboard-read') { if (!navigator.clipboard) return 'unavailable'; await navigator.clipboard.readText(); return 'allowed'; }
  } catch(e) { return 'denied:'+e.name; }
}`

async function server() {
  const srv = http.createServer((_req, res) => {
    res.setHeader('Content-Type', 'text/html')
    res.end(`<html><body><script>${apiScript}</script></body></html>`)
  })

  await new Promise<void>(resolve => srv.listen(0, '127.0.0.1', resolve))

  return { srv, origin: `http://127.0.0.1:${(srv.address() as any).port}` }
}

async function probe(contents, label) {
  for (const kind of ['media', 'geolocation', 'notifications', 'clipboard-read']) {
    console.log(`P2 API start ${label}/${kind}`)
    let timer: ReturnType<typeof setTimeout> | undefined

    const result = await Promise.race([
      contents.executeJavaScript(`window.tryApi(${JSON.stringify(kind)})`, true),
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error(`P2 API deadline ${label}/${kind}`)), 5000)
      })
    ]).finally(() => {
      if (timer) {
        clearTimeout(timer)
      }
    })

    console.log(`P2 API result ${label}/${kind}: ${result}`)
    surfaces.push({ label, kind, result })

    if (label !== 'trusted-voice' || kind !== 'media') {
      assert.notEqual(result, 'allowed', `${label}/${kind}`)
    } else {
      assert.equal(result, 'allowed', 'fake audio voice positive must work')
    }
  }
}

app
  .whenReady()
  .then(async () => {
    console.log('P2 native app ready')
    const trusted = await server()
    const hostile = await server()
    console.log('P2 native inert servers ready')
    appOrigin = trusted.origin
    guard(session.defaultSession)

    const win = new BrowserWindow({
      show: false,
      webPreferences: {
        sandbox: true,
        contextIsolation: true,
        nodeIntegration: false,
        webviewTag: true,
        offscreen: true
      }
    })

    privileged = win.webContents
    await win.loadURL(trusted.origin)
    await probe(privileged, 'trusted-voice')

    for (const label of ['fresh-preview', 'reused-preview']) {
      const attached = new Promise<any>(resolve =>
        privileged.once('did-attach-webview', (_event, guest) => resolve(guest))
      )

      await privileged.executeJavaScript(
        `document.body.innerHTML='<webview allowpopups partition="persist:hermes-preview" src="${hostile.origin}"></webview>'`
      )
      const guest = await attached
      await new Promise<void>(resolve => (guest.isLoading() ? guest.once('did-finish-load', resolve) : resolve()))
      await probe(guest, label)
      assert.equal(await guest.executeJavaScript(`window.open(${JSON.stringify(hostile.origin)}) === null`, true), true)
      assert.equal(guest.getLastWebPreferences().sandbox, true)
      assert.equal(guest.getLastWebPreferences().contextIsolation, true)
      assert.equal(guest.getLastWebPreferences().nodeIntegration, false)
      await privileged.executeJavaScript("document.querySelector('webview').remove()")
    }

    await privileged.executeJavaScript(`document.body.innerHTML='<iframe src="${hostile.origin}"></iframe>'`)
    const frame = privileged.mainFrame.frames[0]
    await new Promise(resolve => setTimeout(resolve, 200))

    for (const kind of ['media', 'geolocation', 'notifications', 'clipboard-read']) {
      const result = await frame.executeJavaScript(`window.tryApi(${JSON.stringify(kind)})`, true)
      surfaces.push({ label: 'hostile-subframe', kind, result })
      assert.notEqual(result, 'allowed')
    }

    assert(popupRequests >= 2, 'popup admission must be reached, not inferred')
    assert(witness.some(entry => entry.rail === 'CHECK' && entry.permission === 'media'))
    assert(witness.some(entry => entry.rail === 'REQUEST' && entry.permission === 'media'))
    assert(
      witness.every(
        entry =>
          !entry.allowed || (entry.main === true && entry.permission === 'media' && entry.url.startsWith(appOrigin))
      )
    )
    fs.writeFileSync(
      process.env.P2_NATIVE_RESULT!,
      JSON.stringify(
        {
          electron: process.versions.electron,
          witness,
          surfaces,
          popupRequests,
          fakeDevices: true,
          normalSandbox: !process.argv.includes('--no-sandbox'),
          limitations:
            'Clipboard/notifications/geolocation may expose only one rail or be unavailable; no write/capture of physical devices.'
        },
        null,
        2
      )
    )
    win.destroy()
    trusted.srv.close()
    hostile.srv.close()
    app.exit(0)
  })
  .catch(error => {
    console.error(error)
    app.exit(1)
  })
