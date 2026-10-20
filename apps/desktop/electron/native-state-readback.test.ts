import { type ChildProcessWithoutNullStreams, spawn, spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { expect, test } from 'vitest'

const PYTHON = process.platform === 'win32' ? 'python' : 'python3'
const OBSERVER = fileURLToPath(new URL('../e2e/native-state-readback.py', import.meta.url))
const LOAD = `import importlib.util,json,sys\nspec=importlib.util.spec_from_file_location('native_readback',${JSON.stringify(OBSERVER)})\nm=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(m)\n`

function collect(child: ChildProcessWithoutNullStreams) {
  let stdout = '', stderr = ''
  child.stdout.on('data', chunk => { stdout += String(chunk) })
  child.stderr.on('data', chunk => { stderr += String(chunk) })

  return new Promise<{ code: number | null; stdout: string; stderr: string }>((resolve, reject) => {
    child.on('error', reject)
    child.on('close', code => resolve({ code, stdout, stderr }))
  })
}

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-e2e-readback-'))
  const home = path.join(root, 'hermes-home')
  fs.mkdirSync(home)

  const env = {
    PATH: process.env.PATH,
    SystemRoot: process.env.SystemRoot,
    HOME: root,
    USERPROFILE: root,
    HERMES_HOME: home,
    TMPDIR: root,
    TEMP: root,
    TMP: root,
    LANG: 'C.UTF-8',
    TZ: 'UTC',
  }

  const setup = spawnSync(PYTHON, ['-P', '-c', `import sqlite3,json,sys\nfrom pathlib import Path\np=Path(sys.argv[1])/'state.db'\ndb=sqlite3.connect(p)\ndb.execute('PRAGMA journal_mode=DELETE')\ndb.execute('create table state_meta(key text primary key,value text)')\ndb.execute('insert into state_meta values (?,?)',('fixture:event:1',json.dumps({'schema':'SessionDBContextInputV1','content':'neutral retained intent'})))\ndb.commit()\ndb.close()`, home], { env, encoding: 'utf8', timeout: 10_000 })
  expect(setup.status, setup.stderr).toBe(0)

  return { root, home, env, cleanup: () => fs.rmSync(root, { recursive: true, force: true }) }
}

const SNAPSHOT = LOAD + "\nfrom pathlib import Path\nprint(json.dumps(m.read_snapshot(Path(sys.argv[1]).resolve(),include_processes=False)))\n"

test('argv identity rejects observer script text and accepts actual module tokens', () => {
  const f = fixture()

  try {
    const result = spawnSync(PYTHON, ['-P', '-c', LOAD + `\nprint(json.dumps({
      'observer':m.process_role([b'python',b'-c',b"role = 'host' if b'tui_gateway.compute_host' in cmd else None"]),
      'foreign':m.process_role([b'python',b'-m',b'other.module',b'tui_gateway.compute_host']),
      'host':m.process_role([b'python',b'-u',b'-m',b'tui_gateway.compute_host',b'--socket',b'fixture']),
      'serve':m.process_role([b'python',b'-m',b'hermes_cli.main',b'serve',b'--port',b'1234'])
    }))`], { env: f.env, encoding: 'utf8', timeout: 10_000 })

    expect(result.status, result.stderr).toBe(0)
    expect(JSON.parse(result.stdout)).toEqual({ observer: null, foreign: null, host: 'host', serve: 'serve' })
  } finally { f.cleanup() }
})

for (const releaseAfterBusy of [true, false]) {
  test(releaseAfterBusy ? 'transient contention yields a complete snapshot within original observation budget' : 'permanent contention refuses instead of returning empty success', async () => {
    const f = fixture()
    let reader: ChildProcessWithoutNullStreams | null = null
    const writer = spawn(PYTHON, ['-P', '-c', "import sqlite3,sys\nfrom pathlib import Path\ndb=sqlite3.connect(Path(sys.argv[1])/'state.db')\ndb.execute('BEGIN EXCLUSIVE')\nprint('LOCKED',flush=True)\nsys.stdin.read()\ndb.rollback()\ndb.close()", f.home], { env: f.env })
    const writerDone = collect(writer)

    try {
      await Promise.race([
        new Promise<void>(resolve => writer.stdout.on('data', chunk => { if (String(chunk).includes('LOCKED')) {resolve()} })),
        writerDone.then(result => { throw new Error(`writer exited before barrier: ${result.stderr}`) }),
      ])
      const start = performance.now()
      reader = spawn(PYTHON, ['-P', '-c', SNAPSHOT, f.home], { env: f.env })
      const done = collect(reader)
      reader.stderr.on('data', chunk => {
        if (releaseAfterBusy && String(chunk).includes('SQLITE_READBACK_BUSY')) {writer.stdin.end()}
      })
      const result = await done
      expect(performance.now() - start).toBeLessThan(10_000)

      if (releaseAfterBusy) {
        expect(result.code, result.stderr).toBe(0)
        const value = JSON.parse(result.stdout)
        expect(value.receipts).toHaveLength(1)
        expect(value.receipts[0].receipt.content).toBe('neutral retained intent')
        expect(value.busy_observations).toBeGreaterThan(0)
        expect(value.processes).toEqual([])
      } else {
        expect(result.code).not.toBe(0)
        expect(result.stdout.trim()).toBe('')
        expect(result.stderr).toContain('database is locked')
      }
    } finally {
      if (reader && reader.exitCode === null) {reader.kill('SIGKILL')}
      writer.stdin.end()
      await writerDone
      f.cleanup()
    }
  }, 20_000)
}

test('non-contention database errors refuse without busy retry or partial output', () => {
  const f = fixture()

  try {
    fs.writeFileSync(path.join(f.home, 'state.db'), 'not a SQLite database')
    const result = spawnSync(PYTHON, ['-P', '-c', SNAPSHOT, f.home], { env: f.env, encoding: 'utf8', timeout: 10_000 })
    expect(result.status).not.toBe(0)
    expect(result.stdout.trim()).toBe('')
    expect(result.stderr).not.toContain('SQLITE_READBACK_BUSY')
    expect(result.stderr).toContain('not a database')
  } finally { f.cleanup() }
})
