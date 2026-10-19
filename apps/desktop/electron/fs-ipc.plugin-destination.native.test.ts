import { spawn } from 'node:child_process'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { buildSync } from 'esbuild'
import { describe, expect, it } from 'vitest'

const require = createRequire(import.meta.url)
const here = path.dirname(fileURLToPath(import.meta.url))

// Real Linux Electron caller; no synthetic platform selection. Other native
// platform jobs must provide their own normal-sandbox runner before qualification.
describe.skipIf(process.platform !== 'linux')('RIC-01 native normal-sandbox IPC matrix', () => {
  it('preserves sentinels and rollback through actual native IPC', async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ric01-native-test-'))

    const evidence = process.env.P1A_EVIDENCE_DIR
      ? path.join(process.env.P1A_EVIDENCE_DIR, 'native-matrix')
      : path.join(root, 'evidence')

    fs.mkdirSync(evidence, { recursive: true })
    const home = path.join(root, 'home')
    fs.mkdirSync(home)
    const bundle = path.join(root, 'main.cjs')
    buildSync({
      entryPoints: [path.join(here, 'fixtures/plugin-destination-native-main.ts')],
      bundle: true,
      platform: 'node',
      format: 'cjs',
      external: ['electron'],
      outfile: bundle
    })
    const electronBinary = require('electron') as string
    const args = ['--headless', '--ozone-platform=headless', '--disable-gpu', '--disable-dev-shm-usage', bundle]
    expect(args).not.toContain('--no-sandbox')
    const logs: string[] = []
    let timeout = false

    const child = spawn(electronBinary, args, {
      env: {
        PATH: process.env.PATH,
        HOME: home,
        TMPDIR: os.tmpdir(),
        TZ: 'UTC',
        LANG: 'C.UTF-8',
        XDG_CONFIG_HOME: path.join(home, '.config'),
        XDG_CACHE_HOME: path.join(home, '.cache'),
        XDG_RUNTIME_DIR: path.join(root, 'runtime'),
        P1A_EVIDENCE_DIR: evidence,
        P1A_FIXTURE_NODE: process.execPath
      },
      detached: true,
      stdio: ['ignore', 'pipe', 'pipe']
    })

    child.stdout.on('data', bytes => logs.push(String(bytes)))
    child.stderr.on('data', bytes => logs.push(String(bytes)))
    let code: number | null = null

    try {
      code = await new Promise<number | null>((resolve, reject) => {
        const timer = setTimeout(() => {
          timeout = true

          try {
            process.kill(-child.pid!, 'SIGKILL')
          } catch (error) {
            if ((error as NodeJS.ErrnoException).code !== 'ESRCH') {
              throw error
            }
          }
        }, 150_000)

        child.once('error', error => {
          clearTimeout(timer)
          reject(error)
        })
        child.once('close', result => {
          clearTimeout(timer)
          resolve(result)
        })
      })
      fs.writeFileSync(path.join(evidence, 'native-process.log'), logs.join(''))
      fs.writeFileSync(
        path.join(evidence, 'native-command.json'),
        JSON.stringify(
          {
            binary: electronBinary,
            args,
            exitCode: code,
            timedOut: timeout,
            normalSandbox: true,
            fakeNodeExecutable: process.execPath
          },
          null,
          2
        )
      )
      expect(timeout, logs.join('')).toBe(false)
      expect(code, logs.join('')).toBe(0)
      const result = JSON.parse(fs.readFileSync(path.join(evidence, 'native-matrix.json'), 'utf8'))
      expect(result.passed).toBe(true)
      expect(result.chromiumSandboxDisabled).toBe(false)
      expect(result.rendererSandbox).toBe(true)
      expect(result.contextIsolation).toBe(true)
      expect(result.nodeIntegration).toBe(false)
      expect(result.observedCases).toBe(result.expectedCases)
      expect(result.revisionError).toBeNull()
      expect(result.revisions).toEqual({ layouts: 4, imports: 0, branchMoves: 4, denials: 36 })
    } finally {
      try {
        process.kill(-child.pid!, 'SIGKILL')
      } catch (error) {
        expect((error as NodeJS.ErrnoException).code, 'tracked fixture group cleanup').toBe('ESRCH')
      }

      fs.rmSync(root, { recursive: true, force: true })
    }
  }, 180_000)
})
