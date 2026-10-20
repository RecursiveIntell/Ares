import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import os from 'node:os'
import path from 'node:path'

import { buildSync } from 'esbuild'
import { expect, test } from 'vitest'

// Executes extracted production functions, never source-shape assertions.
test.skipIf(process.platform !== 'linux')(
  'NativeIPCActual1 normal sandbox actual Electron IPC / Python seam',
  async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'native-ipc-actual-test-'))
    const home = path.join(root, 'home')
    fs.mkdirSync(home)

    const main = path.join(root, 'main.cjs'),
      preload = path.join(root, 'preload.cjs'),
      renderer = path.join(root, 'renderer.js')

    const require = createRequire(import.meta.url)
    const wsVersion = require('ws/package.json').version
    expect(wsVersion).toBe('8.22.0')
    const fixture = new URL('./fixtures/native-ipc-actual-main.ts', import.meta.url).pathname
    buildSync({
      entryPoints: [fixture],
      bundle: true,
      platform: 'node',
      format: 'cjs',
      external: ['electron'],
      outfile: main
    })
    buildSync({
      entryPoints: [new URL('./preload.ts', import.meta.url).pathname],
      bundle: true,
      platform: 'node',
      format: 'cjs',
      external: ['electron'],
      outfile: preload
    })
    buildSync({
      entryPoints: [new URL('../src/api/native-socket.ts', import.meta.url).pathname],
      bundle: true,
      platform: 'browser',
      format: 'iife',
      globalName: 'NativeActual',
      outfile: renderer
    })
    const binary = require('electron') as string
    const args = ['--headless', '--ozone-platform=headless', '--disable-gpu', '--disable-dev-shm-usage', main]
    const evidence = process.env.P1A_EVIDENCE_DIR ?? path.join(root, 'evidence')
    fs.mkdirSync(evidence, { recursive: true })

    const fixturePython =
      process.env.P2_FIXTURE_PYTHON ?? new URL('../../../.venv/bin/python', import.meta.url).pathname

    expect(
      fs.existsSync(fixturePython),
      'Run uv sync to provision the repository Python before native security tests'
    ).toBe(true)

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
        P2_FIXTURE_PYTHON: fixturePython,
        NATIVE_PRELOAD_BUNDLE: preload,
        NATIVE_RENDERER_BUNDLE: renderer,
        NATIVE_WS_VERSION: wsVersion
      }
    })

    // Native library diagnostics are retained locally; no raw backend credentials emitted.
    const logs: string[] = []
    child.stdout.on('data', b => logs.push(String(b)))
    child.stderr.on('data', b => logs.push(String(b)))

    try {
      const code = await new Promise<number | null>((resolve, reject) => {
        const timer = setTimeout(() => {
          try {
            process.kill(-child.pid!, 'SIGKILL')
          } catch {
            // Best-effort fixture teardown; the namespace runner owns the hard boundary.
          }

          reject(Error('native seam deadline'))
        }, 220_000)

        child.once('error', e => {
          clearTimeout(timer)
          reject(e)
        })
        child.once('close', c => {
          clearTimeout(timer)
          resolve(c)
        })
      })

      fs.writeFileSync(path.join(evidence!, 'native-ipc-process.log'), logs.join(''))
      fs.writeFileSync(
        path.join(evidence!, 'native-ipc-command.json'),
        JSON.stringify(
          {
            binary,
            args,
            exitCode: code,
            normalSandbox: true,
            nodeVersion: process.version,
            wsVersion,
            bundleHashes: Object.fromEntries(
              [main, preload, renderer].map(f => [
                path.basename(f),
                createHash('sha256').update(fs.readFileSync(f)).digest('hex')
              ])
            )
          },
          null,
          2
        )
      )
      expect(code, logs.join('')).toBe(0)
      const result = JSON.parse(fs.readFileSync(path.join(evidence!, 'native-ipc-actual.json'), 'utf8'))
      expect(result.effects.reaped).toBe(2)
      expect(result.effects.opens).toBe(4)
      expect(result.effects.messages).toBe(8)
      expect(result.effects.closes).toBe(4)
      expect(result.frames.some((f: any) => !f.top)).toBe(false)
      expect(result.wholeDesktopApplicationQualification).toBe(false)
    } finally {
      if (child.exitCode === null) {
        try {
          process.kill(-child.pid!, 'SIGKILL')
        } catch {
          // Best-effort fixture teardown; the namespace runner owns the hard boundary.
        }
      }

      fs.rmSync(root, { recursive: true, force: true })
    }
  },
  240_000
)
