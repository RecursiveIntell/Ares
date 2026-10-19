import { spawn } from 'node:child_process'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import os from 'node:os'
import path from 'node:path'

import { buildSync } from 'esbuild'
import { expect, test } from 'vitest'

test.skipIf(process.platform !== 'linux')('normal-sandbox Electron fake-device REQUEST/CHECK guest matrix', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'p2-native-'))
  const bundle = path.join(root, 'main.cjs')
  const result = path.join(root, 'result.json')
  const home = path.join(root, 'home')
  fs.mkdirSync(home)
  buildSync({
    entryPoints: [new URL('./fixtures/p2-permissions-native-main.ts', import.meta.url).pathname],
    bundle: true,
    platform: 'node',
    format: 'cjs',
    external: ['electron'],
    outfile: bundle
  })
  const binary = createRequire(import.meta.url)('electron') as string
  const args = ['--headless', '--ozone-platform=headless', '--disable-gpu', '--disable-dev-shm-usage', bundle]
  expect(args).not.toContain('--no-sandbox')
  const logs: string[] = []

  const child = spawn(binary, args, {
    detached: true,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: {
      PATH: process.env.PATH,
      HOME: home,
      TMPDIR: os.tmpdir(),
      LANG: 'C.UTF-8',
      TZ: 'UTC',
      XDG_CONFIG_HOME: path.join(home, '.config'),
      XDG_CACHE_HOME: path.join(home, '.cache'),
      XDG_RUNTIME_DIR: root,
      P2_NATIVE_RESULT: result
    }
  })

  child.stdout.on('data', bytes => logs.push(String(bytes)))
  child.stderr.on('data', bytes => logs.push(String(bytes)))

  try {
    const code = await new Promise<number | null>((resolve, reject) => {
      const deadline = setTimeout(() => {
        process.kill(-child.pid!, 'SIGKILL')
        reject(new Error(`native fixture deadline\n${logs.join('')}`))
      }, 90_000)

      child.once('error', error => {
        clearTimeout(deadline)
        reject(error)
      })
      child.once('close', code => {
        clearTimeout(deadline)
        resolve(code)
      })
    })

    expect(code, logs.join('')).toBe(0)
    const receipt = JSON.parse(fs.readFileSync(result, 'utf8'))
    expect(receipt.normalSandbox).toBe(true)
    expect(receipt.fakeDevices).toBe(true)
    expect(
      receipt.surfaces.some(row => row.label === 'trusted-voice' && row.kind === 'media' && row.result === 'allowed')
    ).toBe(true)

    if (process.env.P1A_EVIDENCE_DIR) {
      fs.writeFileSync(
        path.join(process.env.P1A_EVIDENCE_DIR, 'p2-native-result.json'),
        JSON.stringify(receipt, null, 2)
      )
    }
  } finally {
    if (process.env.P1A_EVIDENCE_DIR) {
      fs.writeFileSync(path.join(process.env.P1A_EVIDENCE_DIR, 'p2-native-process.log'), logs.join(''))
    }

    if (child.exitCode === null) {
      try {
        process.kill(-child.pid!, 'SIGKILL')
      } catch {
        /* exited */
      }
    }

    fs.rmSync(root, { recursive: true, force: true })
  }
}, 100_000)
