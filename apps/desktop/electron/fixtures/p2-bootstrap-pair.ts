import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { writeFileSync } from 'node:fs'
import { createServer } from 'node:http'
import path from 'node:path'

import { adoptServedDashboardToken } from '../dashboard-token'

async function websocket(base: string, token: string) {
  await new Promise<void>((resolve, reject) => {
    const socket = new WebSocket(
      base.replace('http:', 'ws:') + '/api/events?channel=p2-inert&token=' + encodeURIComponent(token)
    )

    const timer = setTimeout(() => {
      socket.close()
      reject(new Error('inert WebSocket deadline'))
    }, 5000)

    socket.addEventListener('open', () => socket.close(1000))
    socket.addEventListener('error', () => {
      clearTimeout(timer)
      reject(new Error('inert WebSocket denied'))
    })
    socket.addEventListener('close', event => {
      clearTimeout(timer)
      event.code === 1000 ? resolve() : reject(new Error('inert WebSocket close mismatch'))
    })
  })
}

async function run() {
  const python = process.env.P2_FIXTURE_PYTHON
  assert.ok(python, 'Provisioned isolated Python is required; no skip/substitution')
  const key = randomBytes(32).toString('base64url')
  const token = randomBytes(32).toString('base64url')
  const identity = randomBytes(16).toString('hex')

  const child = spawn(python, ['-I', '-B', path.resolve('electron/fixtures/p2-bootstrap-backend.py')], {
    stdio: ['ignore', 'pipe', 'pipe'],
    env: {
      PATH: process.env.PATH,
      HOME: process.env.HOME,
      TMPDIR: process.env.TMPDIR,
      HERMES_HOME: process.env.HERMES_HOME,
      ARES_HOME: process.env.ARES_HOME,
      LANG: 'C.UTF-8',
      TZ: 'UTC',
      HERMES_SERVE_HEADLESS: '1',
      HERMES_DESKTOP_BOOTSTRAP_KEY: key,
      HERMES_DESKTOP_BOOTSTRAP_ID: identity,
      HERMES_DASHBOARD_SESSION_TOKEN: token
    }
  })

  let stderr = ''
  child.stderr.on('data', data => {
    stderr = (stderr + String(data)).slice(-8192)
  })
  const stopped = new Promise<void>(resolve => child.once('exit', () => resolve()))
  const seen: string[] = []
  let base = ''

  try {
    const port = await new Promise<number>((resolve, reject) => {
      let text = ''
      const timer = setTimeout(() => reject(new Error('owned backend ready deadline: ' + stderr)), 15000)
      child.once('error', error => {
        clearTimeout(timer)
        reject(error)
      })
      child.once('exit', () => {
        clearTimeout(timer)
        reject(new Error('owned backend exited before ready: ' + stderr))
      })
      child.stdout.on('data', data => {
        text += String(data)

        if (text.length > 8192) {
          clearTimeout(timer)
          reject(new Error('oversized readiness output'))

          return
        }

        for (const line of text.split('\n').slice(0, -1)) {
          try {
            const row = JSON.parse(line)

            if (
              row.pid === child.pid &&
              row.identity === identity &&
              Number.isInteger(row.port) &&
              row.port > 0 &&
              row.port <= 65535
            ) {
              clearTimeout(timer)
              resolve(row.port)
            }
          } catch {
            /* bounded unrelated stdout is not readiness */
          }
        }
      })
    })

    base = `http://127.0.0.1:${port}`
    const owned = { bootstrapKey: key, identity, childAlive: () => child.exitCode === null && !child.killed }
    assert.equal(
      await adoptServedDashboardToken(base, token, {
        ...owned,
        fetch: async (url, options) => {
          seen.push(...(Object.values(options.headers) as string[]))

          return fetch(url, options)
        }
      }),
      token
    )
    await websocket(base, token)
    await websocket(base, token)

    const fake = createServer((req, res) => {
      seen.push(
        ...Object.values(req.headers)
          .flat()
          .filter((v): v is string => typeof v === 'string')
      )
      res.setHeader('content-type', 'application/json')
      res.end(JSON.stringify({ identity: req.headers['x-hermes-native-identity'], token: 'attacker-selected' }))
    })

    await new Promise<void>(resolve => fake.listen(0, '127.0.0.1', resolve))
    const address = fake.address()
    assert.ok(address && typeof address !== 'string')

    try {
      assert.ok(owned.childAlive())
      await assert.rejects(() => adoptServedDashboardToken(`http://127.0.0.1:${address.port}`, token, owned))
    } finally {
      fake.closeAllConnections()
      await new Promise<void>(resolve => fake.close(() => resolve()))
    }

    assert.ok(!seen.includes(key) && !seen.includes(token))
    writeFileSync(
      '/evidence/native-pair.json',
      JSON.stringify(
        {
          actual_owned_python_backend: true,
          actual_node_bootstrap: true,
          token_free_response: true,
          bootstrap_secret_not_transmitted: true,
          admitted_websocket_connections: 2,
          reflected_impostor_denied_while_child_alive: true,
          whole_desktop_application_qualification: false
        },
        null,
        2
      )
    )
  } finally {
    if (child.exitCode === null) {child.kill('SIGTERM')}
    let timer: ReturnType<typeof setTimeout> | undefined
    await Promise.race([
      stopped,
      new Promise<void>((resolve, reject) => {
        timer = setTimeout(() => {
          child.kill('SIGKILL')
          reject(new Error('owned fixture teardown deadline'))
        }, 10000)
      })
    ]).finally(() => {
      if (timer) {clearTimeout(timer)}
    })
  }

  await assert.rejects(
    () => adoptServedDashboardToken(base, token, { bootstrapKey: key, identity, childAlive: () => false }),
    /exited/
  )
  console.log('Actual Node/Python native bootstrap pair PASS; owned child reaped')
}

run().catch(error => {
  console.error(error)
  process.exitCode = 1
})
