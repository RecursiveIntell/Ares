// RIC-01 proof harness. Real filesystem, real installer, real registered IPC
// handler, fake `git` executable. It never touches a user profile, the network,
// or any plugin code: plugin.js is only ever copied, never imported or run.
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import fsp from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

import type { DesktopPluginInstallResult } from '../desktop-plugin-install'

export type Fault = 'backup' | 'copy' | 'missing-entry' | 'publish' | 'restore' | 'swap-link'
export type Link = 'dangling-target' | 'desktop-dir' | 'entry' | 'root' | 'source' | 'subdir-component' | 'target'

export interface DestinationCase {
  label: string
  identifier: string
  /** Raw `force` as it would arrive over IPC; a non-boolean must never mean "true". */
  force: unknown
  expected: 'installed' | 'preserved' | 'recovery' | 'refused'
  errorMatch?: RegExp
  fault?: Fault
  link?: Link
  profile?: string
  staleStage?: boolean
  targetIsFile?: boolean
  /** Refusals that must happen before any clone is attempted. */
  preClone?: boolean
}

export const DESTINATION_REVISION = 'a'.repeat(40)

export const REFUSAL = /^Refused unsafe plugin destination: /

const refusedIdentifiers = [
  '/owner/repo',
  'C:/owner/repo',
  'https://example.invalid/o/repo:stream.git',
  'owner/.PLUGIN-INSTALL-x',
  'owner/..',
  'owner/.',
  'owner/...',
  'owner/repo/plugins/..',
  'owner/repo/plugins/.',
  'owner/repo/plugins/./alerts',
  'https://example.invalid/o/repo.git/plugins/..',
  'https://example.invalid/o/repo.git#plugins/..',
  'https://example.invalid/o/repo.git#../escape',
  'https://github.com/o/repo/tree/main/plugins/..',
  'https://example.invalid/o/repo.git#/absolute',
  'https://example.invalid/o/repo.git#//server/share',
  'https://example.invalid/o/repo.git#C:/drive',
  'https://example.invalid/o/repo.git#\\\\server\\share',
  'owner/repo/plugins\\..',
  'owner/repo/C:drive',
  'owner/CON',
  'owner/con.txt',
  'owner/PRN',
  'owner/AUX',
  'owner/NUL',
  'owner/COM1',
  'owner/LPT9.txt',
  'owner/trailing.',
  'owner/repo:stream',
  'owner/ alerts',
  'owner/.plugin-install-x',
  `owner/${'a'.repeat(300)}`,
  'https://example.invalid/o/%2e%2e.git',
  'https://example.invalid/o/a%2f..%2fb.git',
  'owner/bad\u0000name'
]

export const destinationCases: DestinationCase[] = [
  ...refusedIdentifiers.flatMap((identifier, index) =>
    [false, true].map(force => ({
      label: `invalid-${String(index).padStart(2, '0')}-${force}`,
      identifier,
      force,
      expected: 'refused' as const,
      preClone: true
    }))
  ),
  ...[
    'owner/alerts',
    'owner/repo/plugins/alerts/desktop',
    'https://example.invalid/o/repo.git/plugins/alerts/desktop',
    'https://example.invalid/o/repo.git#plugins/alerts/desktop',
    'https://example.invalid/o/repo.git#plugins/alerts/desktop/', // relative with trailing slash
    'https://github.com/o/repo/tree/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/plugins/alerts/desktop'
  ].flatMap((identifier, index) =>
    [false, true].map(force => ({
      label: `valid-${index}-${force}`,
      identifier,
      force,
      expected: force ? ('installed' as const) : ('preserved' as const),
      errorMatch: force ? undefined : /already exists/
    }))
  ),
  {
    label: 'force-string-false',
    identifier: 'owner/alerts',
    force: 'false',
    expected: 'preserved',
    errorMatch: /already exists/
  },
  {
    label: 'force-undefined',
    identifier: 'owner/alerts',
    force: undefined,
    expected: 'preserved',
    errorMatch: /already exists/
  },
  // A symlinked destination or subdir component is refused whether or not force is set. A hostile
  // source tree (link inside the clone) is inspected only after the existing-plugin check, so with
  // force=false the plugin is simply preserved; either way nothing is mutated and no link is copied.
  ...(['target', 'dangling-target', 'source', 'entry', 'subdir-component', 'desktop-dir'] as const).flatMap(link =>
    [false, true].map(force => {
      const sourceChecked = link === 'source' || link === 'entry' || link === 'desktop-dir'

      return {
        label: `link-${link}-${force}`,
        link,
        force,
        identifier: link === 'subdir-component' ? 'owner/repo/plugins/alerts/desktop' : 'owner/alerts',
        expected: sourceChecked && !force ? ('preserved' as const) : ('refused' as const),
        errorMatch: sourceChecked && !force ? /already exists/ : REFUSAL
      }
    })
  ),
  ...[false, true].map(force => ({
    label: `rootlink-${force}`,
    identifier: 'owner/alerts',
    force,
    link: 'root' as const,
    expected: 'refused' as const,
    errorMatch: REFUSAL
  })),
  ...[false, true].map(force => ({
    label: `target-file-${force}`,
    identifier: 'owner/alerts',
    force,
    targetIsFile: true,
    expected: 'refused' as const,
    errorMatch: REFUSAL
  })),
  { label: 'stale-stage', identifier: 'owner/alerts', force: true, staleStage: true, expected: 'installed' },
  {
    label: 'fault-copy',
    identifier: 'owner/alerts',
    force: true,
    fault: 'copy',
    expected: 'preserved',
    errorMatch: /injected partial copy/
  },
  {
    label: 'fault-backup',
    identifier: 'owner/alerts',
    force: true,
    fault: 'backup',
    expected: 'preserved',
    errorMatch: /injected backup/
  },
  {
    label: 'fault-publish',
    identifier: 'owner/alerts',
    force: true,
    fault: 'publish',
    expected: 'preserved',
    errorMatch: /injected publish/
  },
  {
    label: 'fault-restore',
    identifier: 'owner/alerts',
    force: true,
    fault: 'restore',
    expected: 'recovery',
    errorMatch: /could not be restored.*recovery at/s
  },
  {
    label: 'fault-missing-entry',
    identifier: 'owner/alerts',
    force: true,
    fault: 'missing-entry',
    expected: 'preserved',
    errorMatch: /does not match/
  },
  {
    label: 'fault-swap-link',
    identifier: 'owner/alerts',
    force: true,
    fault: 'swap-link',
    expected: 'preserved',
    errorMatch: /symbolic link/
  },
  { label: 'named-profile', identifier: 'owner/alerts', force: true, profile: 'fixture', expected: 'installed' }
]

export interface Effect {
  operation: string
  paths: string[]
  outside: boolean
  injected?: boolean
}
export interface CaseWitness {
  label: string
  fixtureBase: string
  result: DesktopPluginInstallResult
  sentinelsBefore: Record<string, string>
  sentinelsAfter: Record<string, string>
  oldBefore: string
  oldAfter: string | null
  effects: Effect[]
  effectCounts: Record<string, number>
  outsideDestructive: number
  outsideWrites: number
  cloneCalls: number
  faultTriggered: boolean
  stagingBefore: string[]
  stagingAfter: string[]
}

const DESTRUCTIVE = new Set(['rm', 'rename', 'unlink', 'rmdir', 'truncate'])
type Name =
  | 'appendFile'
  | 'chmod'
  | 'chown'
  | 'copyFile'
  | 'cp'
  | 'link'
  | 'mkdir'
  | 'mkdtemp'
  | 'open'
  | 'rename'
  | 'rm'
  | 'rmdir'
  | 'symlink'
  | 'truncate'
  | 'unlink'
  | 'utimes'
  | 'writeFile'

const NAMES: Name[] = [
  'appendFile',
  'chmod',
  'chown',
  'copyFile',
  'cp',
  'link',
  'mkdir',
  'mkdtemp',
  'open',
  'rename',
  'rm',
  'rmdir',
  'symlink',
  'truncate',
  'unlink',
  'utimes',
  'writeFile'
]

// Which argument positions are mutated (read-only sources are not listed).
const WRITE_ARGS: Partial<Record<Name, number[]>> = {
  appendFile: [0],
  chmod: [0],
  chown: [0],
  copyFile: [1],
  cp: [1],
  link: [1],
  mkdir: [0],
  rename: [0, 1],
  rm: [0],
  rmdir: [0],
  symlink: [1],
  truncate: [0],
  unlink: [0],
  utimes: [0],
  writeFile: [0]
}

// Node's fs.promises.cp keeps the function references it first sees, so
// per-case monkey patching leaks the first case's closure into every later
// case. Install ONE wrapper set for the module's life and swap the active
// context; with no active context every wrapper is transparent.
type Hook = (name: Name, args: any[]) => void | Promise<void>
const REAL = Object.fromEntries(NAMES.map(n => [n, (fsp as any)[n]])) as Record<Name, (...a: any[]) => Promise<any>>
let activeHook: Hook | null = null
let activeAfter: ((name: Name, args: any[], result: unknown) => Promise<void>) | null = null

for (const name of NAMES) {
  ;

(fsp as any)[name] = async (...args: any[]) => {
    if (!activeHook) {
      return REAL[name](...args)
    }

    await activeHook(name, args)
    const result = await REAL[name](...args)

    if (activeAfter) {
      await activeAfter(name, args, result)
    }

    return result
  }
}

function digest(bytes: Buffer | string) {
  return createHash('sha256').update(bytes).digest('hex')
}

function beneath(root: string, p: string) {
  return p === root || p.startsWith(root + path.sep)
}

function isWriteOpen(flags: unknown) {
  if (typeof flags === 'number') {
    return (
      (flags &
        (fs.constants.O_WRONLY |
          fs.constants.O_RDWR |
          fs.constants.O_CREAT |
          fs.constants.O_TRUNC |
          fs.constants.O_APPEND)) !==
      0
    )
  }

  return typeof flags === 'string' && /[wa+]/.test(flags)
}

function tryDigest(p: string) {
  try {
    return fs.statSync(p).isFile() ? digest(fs.readFileSync(p)) : null
  } catch {
    return null
  }
}

function stages(root: string) {
  try {
    return fs
      .readdirSync(root)
      .filter(n => n.startsWith('.plugin-install-'))
      .sort()
  } catch {
    return []
  }
}

export async function exerciseDestinationCase(
  testCase: DestinationCase,
  invoke: (
    home: string,
    git: string,
    identifier: string,
    force: unknown,
    profile?: string
  ) => Promise<DesktopPluginInstallResult>,
  fixtureNodeExecutable = process.execPath
): Promise<CaseWitness> {
  assert.equal(activeHook, null, 'a previous case context is still active')
  const base = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'ric01-fixture-')))
  const home = path.join(base, 'home')
  const profileHome = testCase.profile ? path.join(home, 'profiles', testCase.profile) : home
  const root = path.join(profileHome, 'desktop-plugins')
  const realRoot = testCase.link === 'root' ? path.join(base, 'relocated', 'desktop-plugins') : root
  const target = path.join(root, 'alerts')
  const outside = path.join(base, 'outside')
  fs.mkdirSync(path.join(realRoot, 'unrelated'), { recursive: true })
  fs.mkdirSync(outside)
  fs.mkdirSync(path.join(outside, 'plugins-target', 'alerts', 'desktop'), { recursive: true })
  fs.writeFileSync(path.join(outside, 'plugins-target', 'alerts', 'desktop', 'plugin.js'), 'outside-plugin-bytes')

  if (testCase.link === 'root') {
    fs.mkdirSync(profileHome, { recursive: true })
    fs.symlinkSync(realRoot, root, 'dir')
  }

  const realTarget = path.join(realRoot, 'alerts')
  let oldFile = path.join(realTarget, 'plugin.js')

  if (testCase.targetIsFile) {
    fs.writeFileSync(realTarget, 'regular-file-not-a-directory')
    oldFile = realTarget
  } else {
    fs.mkdirSync(realTarget)
    fs.writeFileSync(path.join(realTarget, 'plugin.js'), 'old-plugin-bytes')
    fs.writeFileSync(path.join(realTarget, 'grants.json'), '{"fixture":"old-grants"}')
  }

  if (testCase.staleStage) {
    fs.mkdirSync(path.join(realRoot, '.plugin-install-STALE', 'candidate'), { recursive: true })
    fs.writeFileSync(path.join(realRoot, '.plugin-install-STALE', 'candidate', 'plugin.js'), 'stale-stage-bytes')
    fs.writeFileSync(path.join(realRoot, '.plugin-install-STALE', 'stale.sentinel'), 'stale')
  }

  const sentinels = [
    path.join(base, 'parent.sentinel'),
    path.join(profileHome, 'home.sentinel'),
    path.join(realRoot, 'unrelated', 'sibling.sentinel'),
    path.join(outside, 'outside.sentinel')
  ]

  for (const p of sentinels) {
    fs.writeFileSync(p, `unrelated:${path.basename(p)}`)
  }

  if (testCase.targetIsFile) {
    sentinels.push(realTarget)
  }

  if (testCase.staleStage) {
    sentinels.push(path.join(realRoot, '.plugin-install-STALE', 'stale.sentinel'))
  }

  if (testCase.link === 'target' || testCase.link === 'dangling-target') {
    fs.renameSync(target, path.join(root, 'original'))
    fs.symlinkSync(testCase.link === 'target' ? outside : path.join(base, 'absent'), target, 'dir')
    oldFile = path.join(root, 'original', 'plugin.js')
  }

  const snapshots = () => Object.fromEntries(sentinels.map(p => [path.basename(p), digest(fs.readFileSync(p))]))
  const sentinelsBefore = snapshots()
  const oldBefore = tryDigest(oldFile)!
  const stagingBefore = stages(realRoot)
  const log = path.join(base, 'clone-calls.jsonl')
  const git = path.join(base, 'fake-git')

  const cloneScript = `#!${fixtureNodeExecutable}
const fs = require('node:fs'); const path = require('node:path');
const command = process.argv[2];
if (command === 'rev-parse') { process.stdout.write('${DESTINATION_REVISION}\\n'); process.exit(0); }
if (command !== 'clone') process.exit(0);
const dest = process.argv.at(-1);
fs.appendFileSync(${JSON.stringify(log)}, JSON.stringify(process.argv.slice(2)) + '\\n');
for (const rel of ['', 'plugins/alerts/desktop', 'plugins', 'absolute', 'C:/drive', 'alerts']) {
  const dir = path.join(dest, rel); fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'plugin.js'), 'new-plugin-bytes');
}
${testCase.link === 'desktop-dir' ? `fs.rmSync(path.join(dest, 'plugin.js')); fs.rmSync(path.join(dest, 'alerts'), { recursive: true }); fs.rmSync(path.join(dest, 'plugins'), { recursive: true }); fs.symlinkSync(${JSON.stringify(path.join(outside, 'plugins-target', 'alerts', 'desktop'))}, path.join(dest, 'desktop'), 'dir');` : ''}
${testCase.link === 'source' ? `fs.symlinkSync(${JSON.stringify(outside)}, path.join(dest, 'escape'), 'dir');` : ''}
${testCase.link === 'entry' ? `fs.unlinkSync(path.join(dest, 'plugin.js')); fs.symlinkSync(${JSON.stringify(sentinels[3])}, path.join(dest, 'plugin.js'));` : ''}
${testCase.link === 'subdir-component' ? `fs.rmSync(path.join(dest, 'plugins'), { recursive: true }); fs.symlinkSync(${JSON.stringify(path.join(outside, 'plugins-target'))}, path.join(dest, 'plugins'), 'dir');` : ''}
`

  fs.writeFileSync(git, cloneScript, { mode: 0o700 })

  const effects: Effect[] = []
  const ownedTemps: string[] = []
  let faultTriggered = false
  const allowed = (p: string) => beneath(root, p) || beneath(realRoot, p) || ownedTemps.some(r => beneath(r, p))
  fs.mkdirSync(path.join(base, 'case-window-open')) // traced mkdir bounds the installer window in strace output

  activeHook = async (name, args) => {
    const writeIdx = name === 'open' ? (isWriteOpen(args[1]) ? [0] : []) : (WRITE_ARGS[name] ?? [])

    const paths = (name === 'rename' || name === 'cp' || name === 'copyFile' ? [args[0], args[1]] : [args[0]])
      .filter(v => typeof v === 'string' || v instanceof URL)
      .map(v => path.resolve(String(v)))

    const writes = writeIdx
      .map(i => args[i])
      .filter(v => typeof v === 'string')
      .map(v => path.resolve(v))

    const unsafe = writes.some(p => !allowed(p))

    if (name !== 'mkdtemp') {
      effects.push({ operation: name, paths, outside: unsafe })
    }

    // Even against the unfixed baseline an outside mutation is witnessed but
    // refused, so no unrelated disposable sentinel can actually be destroyed.
    if (unsafe) {
      throw new Error(`[${testCase.label} root=${root}] Fixture blocked outside-target ${name}: ${paths.join(', ')}`)
    }

    if (name === 'cp' && testCase.fault === 'copy') {
      faultTriggered = true
      await REAL.mkdir(String(args[1]), { recursive: true })
      fs.writeFileSync(path.join(String(args[1]), 'partial'), 'partial-copy')
      throw new Error('injected partial copy failure')
    }

    if (name === 'rename') {
      const from = path.resolve(String(args[0]))
      const to = path.resolve(String(args[1]))
      const backup = from === realTarget
      const publish = to === realTarget && path.basename(from) === 'candidate'
      const restore = to === realTarget && path.basename(from) === 'previous'

      if (
        (testCase.fault === 'backup' && backup) ||
        (testCase.fault === 'publish' && publish) ||
        (testCase.fault === 'restore' && (publish || restore))
      ) {
        faultTriggered = true
        effects.at(-1)!.injected = true
        throw new Error(`injected ${restore ? 'restore' : publish ? 'publish' : 'backup'} rename failure`)
      }
    }
  }

  activeAfter = async (name, args, result) => {
    if (name === 'mkdtemp') {
      ownedTemps.push(String(result))
      effects.push({ operation: 'mkdtemp', paths: [String(result)], outside: false })
    }

    if (name === 'cp' && testCase.fault === 'missing-entry') {
      faultTriggered = true
      fs.unlinkSync(path.join(String(args[1]), 'plugin.js'))
    }

    if (name === 'cp' && testCase.fault === 'swap-link') {
      faultTriggered = true
      fs.renameSync(realTarget, path.join(root, 'original'))
      fs.symlinkSync(outside, realTarget, 'dir')
      oldFile = path.join(root, 'original', 'plugin.js')
    }
  }

  try {
    const result = await invoke(home, git, testCase.identifier, testCase.force, testCase.profile)
    activeHook = null
    activeAfter = null
    fs.mkdirSync(path.join(base, 'case-window-close'))
    const sentinelsAfter = snapshots()
    const effectCounts: Record<string, number> = {}

    for (const e of effects) {
      effectCounts[e.operation] = (effectCounts[e.operation] ?? 0) + 1
    }

    const witness: CaseWitness = {
      label: testCase.label,
      fixtureBase: base,
      result,
      sentinelsBefore,
      sentinelsAfter,
      oldBefore,
      oldAfter: tryDigest(oldFile),
      effects,
      effectCounts,
      outsideDestructive: effects.filter(e => e.outside && DESTRUCTIVE.has(e.operation)).length,
      outsideWrites: effects.filter(e => e.outside).length,
      cloneCalls: fs.existsSync(log) ? fs.readFileSync(log, 'utf8').trim().split('\n').length : 0,
      faultTriggered,
      stagingBefore,
      stagingAfter: stages(realRoot)
    }

    const evidence = process.env.P1A_EVIDENCE_DIR

    if (evidence) {
      fs.writeFileSync(path.join(evidence, `${testCase.label}.json`), JSON.stringify(witness, null, 2))
    }

    assert.deepEqual(sentinelsAfter, sentinelsBefore, 'unrelated sentinels changed')
    assert.equal(witness.outsideDestructive, 0, 'outside-target destructive attempt')
    assert.equal(witness.outsideWrites, 0, 'outside-target write attempt')

    if (testCase.errorMatch) {
      assert.match(result.error ?? '', testCase.errorMatch)
    }

    if (testCase.fault) {
      assert.equal(faultTriggered, true, 'failure injection not reached')
    }

    if (testCase.preClone) {
      assert.equal(witness.cloneCalls, 0, 'refusal must precede any clone')
    }

    switch (testCase.expected) {
      case 'refused':
        assert.equal(result.ok, false, JSON.stringify(result))
        assert.match(result.error ?? '', REFUSAL)
        assert.equal(witness.oldAfter, oldBefore, 'old plugin not preserved')
        assert.deepEqual(witness.stagingAfter, stagingBefore, 'staging leaked')

        break

      case 'preserved':
        assert.equal(result.ok, false, JSON.stringify(result))
        assert.equal(witness.oldAfter, oldBefore, 'old plugin not preserved')
        assert.equal(
          fs.readFileSync(path.join(path.dirname(oldFile), 'grants.json'), 'utf8'),
          '{"fixture":"old-grants"}'
        )
        assert.deepEqual(witness.stagingAfter, stagingBefore, 'staging leaked')

        break

      case 'installed':
        assert.equal(result.ok, true, JSON.stringify(result))
        assert.equal(fs.readFileSync(path.join(target, 'plugin.js'), 'utf8'), 'new-plugin-bytes')
        assert.equal(fs.lstatSync(realTarget).isDirectory(), true)
        assert.deepEqual(witness.stagingAfter, stagingBefore, 'staging leaked')

        break
      case 'recovery': {
        assert.equal(result.ok, false)
        const retained = witness.stagingAfter.filter(n => !stagingBefore.includes(n))
        assert.equal(retained.length, 1, 'exactly one retained recovery directory')
        const previous = path.join(realRoot, retained[0], 'previous', 'plugin.js')
        assert.equal(tryDigest(previous), oldBefore, 'retained previous tree must be byte-identical')
        assert.ok(
          (result.error ?? '').includes(path.join(realRoot, retained[0], 'previous')),
          'error must name the recovery path'
        )

        break
      }
    }

    return witness
  } finally {
    activeHook = null
    activeAfter = null
    fs.rmSync(base, { recursive: true, force: true })
  }
}
