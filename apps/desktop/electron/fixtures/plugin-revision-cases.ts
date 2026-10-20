// RIC-03 fixture shared by registered IPC and normal-sandbox native IPC.
// Local Git only, disposable homes, plugin code is an effect sentinel.
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

export interface RevisionCaller {
  probe: (identifier: string) => Promise<{ ok: boolean; revision?: string; error?: string }>
  install: (
    identifier: string,
    revision?: unknown
  ) => Promise<{ ok: boolean; revision?: string; path?: string; error?: string }>
}

export async function exerciseRevisionCases(
  connect: (home: string) => RevisionCaller
): Promise<{ layouts: number; imports: number; branchMoves: number; denials: number }> {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'ric03-revisions-'))
  let denials = 0

  try {
    for (const [index, layout] of ['root', 'desktop', 'fragment', 'git-boundary'].entries()) {
      const root = path.join(base, String(index))
      const home = path.join(root, 'home')
      const repo = path.join(root, 'alerts.git')
      fs.mkdirSync(repo, { recursive: true })
      fs.mkdirSync(home)
      const git = (...args: string[]) => execFileSync('git', args, { cwd: repo, encoding: 'utf8' }).trim()
      git('init', '-q', '-b', 'main')
      git('config', 'user.email', 'fixture@example.invalid')
      git('config', 'user.name', 'Revision fixture')
      const subdir = layout === 'fragment' || layout === 'git-boundary' ? 'plugins/alerts/desktop' : ''
      const source = path.join(repo, subdir || (layout === 'desktop' ? 'desktop' : ''))
      fs.mkdirSync(source, { recursive: true })
      const effect = path.join(root, 'import-effects')
      const oldBytes = `import fs from 'node:fs'; fs.writeFileSync(${JSON.stringify(effect)}, 'imported'); throw new Error('plugin must never be imported'); // inspected\n`
      fs.writeFileSync(path.join(source, 'plugin.js'), oldBytes)
      git('add', '.')
      git('commit', '-qm', 'inspected fixture')
      const old = git('rev-parse', 'HEAD')
      const url = pathToFileURL(repo).href

      const identifier =
        layout === 'fragment' ? `${url}#${subdir}` : layout === 'git-boundary' ? `${url}/${subdir}` : url

      const caller = connect(home)
      const inspected = await caller.probe(identifier)
      assert.equal(inspected.ok, true, inspected.error)
      assert.equal(inspected.revision, old)
      fs.writeFileSync(path.join(source, 'plugin.js'), '// different default branch bytes\n')
      git('add', '.')
      git('commit', '-qm', 'moved default fixture')
      const latest = git('rev-parse', 'HEAD')
      assert.notEqual(old, latest)
      const installed = await caller.install(identifier, inspected.revision)
      assert.equal(installed.ok, true, installed.error)
      assert.equal(installed.revision, old)
      assert.equal(fs.readFileSync(path.join(installed.path!, 'plugin.js'), 'utf8'), oldBytes)
      assert.equal((await caller.probe(identifier)).revision, latest)

      // Invalid approvals never repair their syntax or replace old installed bytes.
      for (const revision of [
        undefined,
        '',
        'main',
        old.slice(0, 39),
        `${old} `,
        ` ${old}`,
        old + '\n',
        'g'.repeat(40)
      ]) {
        const denied = await caller.install(identifier, revision)
        assert.equal(denied.ok, false)
        assert.equal(fs.readFileSync(path.join(installed.path!, 'plugin.js'), 'utf8'), oldBytes)
        denials++
      }

      const other = path.join(root, 'foreign.git')
      fs.mkdirSync(other)
      const foreignGit = (...args: string[]) => execFileSync('git', args, { cwd: other, encoding: 'utf8' }).trim()
      foreignGit('init', '-q', '-b', 'main')
      foreignGit('config', 'user.email', 'foreign@example.invalid')
      foreignGit('config', 'user.name', 'Foreign fixture')
      fs.writeFileSync(path.join(other, 'plugin.js'), '// foreign bytes\n')
      foreignGit('add', '.')
      foreignGit('commit', '-qm', 'foreign fixture')
      // A full, syntactically valid commit from another repo is not fetched
      // from anywhere except the declared origin and cannot replace the target.
      const denied = await caller.install(identifier, foreignGit('rev-parse', 'HEAD'))
      assert.equal(denied.ok, false)
      assert.equal(fs.readFileSync(path.join(installed.path!, 'plugin.js'), 'utf8'), oldBytes)
      assert.equal(fs.existsSync(effect), false)
      denials++
    }

    return { layouts: 4, imports: 0, branchMoves: 4, denials }
  } finally {
    fs.rmSync(base, { recursive: true, force: true })
  }
}
