/**
 * Probe and install desktop runtime plugins from Git repositories.
 * Pure helpers are exported for unit tests; IPC handlers in main.ts call the
 * async entry points with a resolved git binary.
 */

import { execFile, spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import fsp from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

const GITHUB_BROWSER_SEGMENTS = new Set([
  'tree',
  'blob',
  'commit',
  'commits',
  'branches',
  'tags',
  'releases',
  'pull',
  'pulls',
  'issues',
  'actions',
  'compare',
  'blame',
  'wiki'
])

const REFUSED = 'Refused unsafe plugin destination'
const STAGE_PREFIX = '.plugin-install-'
const MAX_NAME_BYTES = 255
const MAX_TREE_ENTRIES = 50_000
const MAX_TREE_DEPTH = 64
const GITHUB_SEGMENT = /^[A-Za-z0-9._-]+$/
const DRIVE_PREFIX = /^[A-Za-z]:/
// Windows device names are reserved with or without an extension (CON, con.txt).
const WINDOWS_RESERVED_BASE = /^(?:con|prn|aux|nul|conin\$|conout\$|(?:com|lpt)[0-9\u00b9\u00b2\u00b3])$/i
const WINDOWS_FORBIDDEN_CHARS = '<>:"/\\|?*'

function refuse(detail: string): Error {
  return new Error(`${REFUSED}: ${detail}.`)
}

function hasControlCharacter(value: string): boolean {
  for (const ch of value) {
    const code = ch.codePointAt(0) ?? 0

    if (code < 0x20 || (code >= 0x7f && code <= 0x9f)) {
      return true
    }
  }

  return false
}

/**
 * The one strict definition of a directory name this installer may create or
 * replace directly under the plugin root. It is deliberately portable (POSIX
 * and Windows rules together). It is applied to the final derived name, and
 * again to the final destination immediately before any filesystem mutation,
 * independent of how the source identifier was parsed.
 */
export function assertPortableChildName(value: unknown, label = 'plugin folder name'): string {
  if (typeof value !== 'string') {
    throw refuse(`${label} must be a string`)
  }

  const name = value

  if (!name) {
    throw refuse(`${label} is empty`)
  }

  if (Buffer.byteLength(name, 'utf8') > MAX_NAME_BYTES) {
    throw refuse(`${label} is too long`)
  }

  if (/^\.+$/.test(name)) {
    throw refuse(`${label} is a dot segment`)
  }

  if (hasControlCharacter(name)) {
    throw refuse(`${label} contains a control character`)
  }

  for (const ch of name) {
    if (WINDOWS_FORBIDDEN_CHARS.includes(ch)) {
      throw refuse(`${label} contains a separator, drive, stream or wildcard character`)
    }
  }

  if (path.posix.isAbsolute(name) || path.win32.isAbsolute(name) || DRIVE_PREFIX.test(name)) {
    throw refuse(`${label} is absolute or drive-qualified`)
  }

  if (name.startsWith(' ') || name.endsWith(' ') || name.endsWith('.')) {
    throw refuse(`${label} has leading/trailing whitespace or a trailing dot`)
  }

  if (WINDOWS_RESERVED_BASE.test(name.split('.')[0].replace(/ +$/, ''))) {
    throw refuse(`${label} is a reserved device name`)
  }

  if (name.toLowerCase().startsWith(STAGE_PREFIX)) {
    throw refuse(`${label} collides with installer staging`)
  }

  return name
}

/** Source subdirectories must already be relative; never strip an absolute
 *  prefix into an apparently safe identity. Trailing slashes are tolerated. */
function normalizeSubdir(raw: string): string | null {
  if (path.posix.isAbsolute(raw) || path.win32.isAbsolute(raw) || DRIVE_PREFIX.test(raw)) {
    throw refuse('plugin subdirectory is absolute or drive-qualified')
  }

  const stripped = raw.replace(/\/+$/g, '')

  if (!stripped) {
    return null
  }

  if (stripped.includes('\\')) {
    throw refuse('plugin subdirectory contains a backslash')
  }

  if (hasControlCharacter(stripped)) {
    throw refuse('plugin subdirectory contains a control character')
  }

  for (const segment of stripped.split('/')) {
    if (!segment || segment === '.' || segment === '..') {
      throw refuse('plugin subdirectory contains an empty or dot segment')
    }

    if (DRIVE_PREFIX.test(segment)) {
      throw refuse('plugin subdirectory contains a drive-qualified segment')
    }
  }

  return stripped
}

function assertGithubSlug(owner: string, repo: string): void {
  for (const part of [owner, repo]) {
    if (!GITHUB_SEGMENT.test(part) || /^\.+$/.test(part)) {
      throw refuse('repository owner/name is not a valid slug')
    }
  }
}

export interface ResolvedGitUrl {
  gitUrl: string
  subdir: string | null
  revision?: string
}

export interface PluginComponentDetection {
  agent: boolean
  desktop: boolean
  agentName: string | null
  desktopName: string | null
  desktopSourceSubdir: string | null
}

export interface PluginProbeResult {
  ok: boolean
  agent: boolean
  desktop: boolean
  agentName?: string | null
  desktopName?: string | null
  warnings: string[]
  insecure: boolean
  error?: string
  revision?: string
}

export interface DesktopPluginInstallResult {
  ok: boolean
  pluginName?: string
  path?: string
  error?: string
  revision?: string
}

function exactRevision(value: unknown): string {
  if (typeof value !== 'string' || value.length !== 40 || !/^[0-9a-fA-F]{40}$/.test(value)) {
    throw refuse('revision must be a full 40-character commit SHA')
  }

  return value.toLowerCase()
}

export function resolvePluginGitUrl(identifier: string): ResolvedGitUrl {
  const trimmed = identifier.trim()

  if (!trimmed) {
    throw new Error('Plugin identifier is required.')
  }

  if (/^(https?:\/\/|git@|ssh:\/\/|file:\/\/)/.test(trimmed)) {
    if (trimmed.startsWith('https://github.com/')) {
      const rest = trimmed.slice('https://github.com/'.length).split(/[?#]/)[0].replace(/\/+$/, '')
      const parts = rest.split('/')

      if (parts.length >= 3 && parts[2] && GITHUB_BROWSER_SEGMENTS.has(parts[2])) {
        const repo = parts[1].replace(/\.git$/, '')
        let subdir: string | null = null

        assertGithubSlug(parts[0], repo)

        if (!['tree', 'blob', 'commit'].includes(parts[2]) || trimmed !== identifier || /[?#]/.test(trimmed)) {
          throw refuse('unsupported or ambiguous GitHub revision link')
        }

        const revision = exactRevision(parts[3])

        if (parts[2] === 'commit' && parts.length !== 4) {
          throw refuse('unsupported GitHub commit path')
        }

        if (parts[2] === 'blob' && parts.length < 5) {
          throw refuse('GitHub blob link requires a file path')
        }

        const sourceParts = parts[2] === 'blob' ? parts.slice(4, -1) : parts.slice(4)

        // Validate the file token too; it cannot conceal an unsafe path.
        if (parts[2] === 'blob') {
          normalizeSubdir(parts.slice(4).join('/'))
        }

        subdir = normalizeSubdir(sourceParts.join('/'))

        return { gitUrl: `https://github.com/${parts[0]}/${repo}.git`, subdir, revision }
      }
    }

    if (trimmed.includes('#')) {
      const hashIdx = trimmed.indexOf('#')
      const gitUrl = trimmed.slice(0, hashIdx)
      const subdir = normalizeSubdir(trimmed.slice(hashIdx + 1))

      return { gitUrl, subdir }
    }

    const marker = '.git/'

    if (trimmed.includes(marker)) {
      const idx = trimmed.indexOf(marker)
      const gitUrl = trimmed.slice(0, idx + marker.length - 1)
      const subdir = normalizeSubdir(trimmed.slice(idx + marker.length))

      return { gitUrl, subdir }
    }

    return { gitUrl: trimmed, subdir: null }
  }

  if (path.posix.isAbsolute(trimmed) || path.win32.isAbsolute(trimmed) || DRIVE_PREFIX.test(trimmed)) {
    throw refuse('repository shorthand is absolute or drive-qualified')
  }

  const parts = trimmed.split('/').filter(Boolean)

  if (parts.length >= 2) {
    const [owner, repo, ...rest] = parts

    assertGithubSlug(owner, repo)

    const gitUrl = `https://github.com/${owner}/${repo}.git`
    const subdir = normalizeSubdir(rest.join('/'))

    return { gitUrl, subdir }
  }

  throw new Error("Invalid plugin identifier. Use a Git URL or 'owner/repo' (optionally with a subdirectory).")
}

export function repoNameFromUrl(url: string): string {
  // Strip only the scp-style transport prefix, not colons in the actual repo
  // name. A drive/stream spelling must reach the name validator unchanged.
  let name = url.replace(/^git@[^:]+:/, '').replace(/\/+$/, '')

  if (name.endsWith('.git')) {
    name = name.slice(0, -4)
  }

  name = name.split('/').pop() || name

  return name
}

/** Stable on-disk folder for a desktop plugin. Never the clone temp dir or a generic `desktop/` folder.
 *  The result is always one validated portable child name, or this throws. */
export function desktopPluginFolderName(gitUrl: string, subdir: string | null): string {
  const derived = subdir
    ? subdir
        .split('/')
        .filter(part => part && part !== 'desktop')
        .pop()
    : undefined

  const name = assertPortableChildName(derived || repoNameFromUrl(gitUrl))

  if (name.includes('%')) {
    // A percent-encoded dot or separator must not become a destination later.
    let decoded: string | null = null

    try {
      decoded = decodeURIComponent(name)
    } catch {
      decoded = null
    }

    if (decoded !== null) {
      assertPortableChildName(decoded, 'decoded plugin folder name')
    }
  }

  return name
}

export function resolveSubdirWithin(cloneRoot: string, subdir: string): string {
  const root = path.resolve(cloneRoot)
  const candidate = path.resolve(root, subdir)

  if (candidate !== root && !candidate.startsWith(root + path.sep)) {
    throw new Error(`Plugin subdirectory '${subdir}' escapes the repository.`)
  }

  return candidate
}

function pathExistsSync(filePath: string): boolean {
  try {
    fs.accessSync(filePath)

    return true
  } catch {
    return false
  }
}

async function pathIsDirectory(filePath: string): Promise<boolean> {
  try {
    const stat = await fsp.stat(filePath)

    return stat.isDirectory()
  } catch {
    return false
  }
}

export function findDesktopEntry(pluginRoot: string): { entryFile: string; sourceSubdir: string } | null {
  const rootPlugin = path.join(pluginRoot, 'plugin.js')

  if (pathExistsSync(rootPlugin)) {
    return { entryFile: rootPlugin, sourceSubdir: '.' }
  }

  const nestedPlugin = path.join(pluginRoot, 'desktop', 'plugin.js')

  if (pathExistsSync(nestedPlugin)) {
    return { entryFile: nestedPlugin, sourceSubdir: 'desktop' }
  }

  return null
}

export async function detectPluginComponents(pluginRoot: string): Promise<PluginComponentDetection> {
  const hasYaml =
    pathExistsSync(path.join(pluginRoot, 'plugin.yaml')) || pathExistsSync(path.join(pluginRoot, 'plugin.yml'))

  const hasInit = pathExistsSync(path.join(pluginRoot, '__init__.py'))
  const hasPortable = pathExistsSync(path.join(pluginRoot, 'plugin.json'))
  const agent = (hasYaml && hasInit) || hasPortable

  const desktopEntry = findDesktopEntry(pluginRoot)
  const desktop = desktopEntry !== null

  let agentName: string | null = null

  if (agent) {
    agentName = path.basename(pluginRoot)

    if (hasYaml) {
      try {
        const yamlPath = pathExistsSync(path.join(pluginRoot, 'plugin.yaml'))
          ? path.join(pluginRoot, 'plugin.yaml')
          : path.join(pluginRoot, 'plugin.yml')

        const text = await fsp.readFile(yamlPath, 'utf8')
        const match = text.match(/^name:\s*['"]?([^'"\n]+)['"]?\s*$/m)

        if (match?.[1]) {
          agentName = match[1].trim()
        }
      } catch {
        // Fall back to directory name.
      }
    } else if (hasPortable) {
      try {
        const raw = await fsp.readFile(path.join(pluginRoot, 'plugin.json'), 'utf8')
        const parsed = JSON.parse(raw) as { name?: string }

        if (parsed.name) {
          agentName = parsed.name
        }
      } catch {
        // Fall back to directory name.
      }
    }
  }

  const desktopName = desktop
    ? desktopEntry!.sourceSubdir === '.'
      ? path.basename(pluginRoot)
      : path.basename(path.dirname(desktopEntry!.entryFile))
    : null

  return {
    agent,
    desktop,
    agentName,
    desktopName,
    desktopSourceSubdir: desktopEntry?.sourceSubdir ?? null
  }
}

function noninteractiveGitEnv(): NodeJS.ProcessEnv {
  return {
    ...process.env,
    GIT_TERMINAL_PROMPT: '0',
    GIT_ASKPASS: 'echo',
    SSH_ASKPASS: 'echo'
  }
}

function runGit(
  gitBin: string,
  args: string[],
  cwd?: string
): Promise<{ code: number; stderr: string; stdout: string }> {
  return new Promise((resolve, reject) => {
    const child = spawn(gitBin, args, {
      cwd,
      env: noninteractiveGitEnv(),
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true
    })

    let stderr = ''
    let stdout = ''

    const timer = setTimeout(() => {
      child.kill('SIGKILL')
      reject(new Error('Git clone timed out after 60 seconds.'))
    }, 60_000)

    child.stderr?.on('data', chunk => {
      stderr += String(chunk)
    })
    child.stdout?.on('data', chunk => {
      stdout += String(chunk)
    })

    child.on('error', err => {
      clearTimeout(timer)
      reject(err)
    })

    child.on('close', code => {
      clearTimeout(timer)
      resolve({ code: code ?? 1, stderr, stdout })
    })
  })
}

async function headRevision(gitBin: string, root: string): Promise<string> {
  const result = await runGit(gitBin, ['rev-parse', 'HEAD'], root)

  if (result.code !== 0) {
    throw new Error(`Git revision lookup failed:\n${result.stderr.trim()}`)
  }

  return exactRevision(result.stdout.replace(/\r?\n$/, ''))
}

async function cloneToTemp(gitBin: string, gitUrl: string, revision?: string): Promise<string> {
  const tmpRoot = await fsp.mkdtemp(path.join(os.tmpdir(), 'hermes-plugin-'))

  try {
    const args = ['clone', '--depth', '1']

    if (revision) {
      args.push('--no-checkout')
    }

    const { code, stderr } = await runGit(gitBin, [...args, gitUrl, tmpRoot])

    if (code !== 0) {
      throw new Error(`Git clone failed:\n${stderr.trim()}`)
    }

    if (revision) {
      for (const args of [
        ['fetch', '--depth', '1', 'origin', revision],
        ['checkout', '--detach', revision]
      ]) {
        const result = await runGit(gitBin, args, tmpRoot)

        if (result.code !== 0) {
          throw new Error(`Git exact-revision checkout failed:\n${result.stderr.trim()}`)
        }
      }

      if ((await headRevision(gitBin, tmpRoot)) !== revision) {
        throw refuse('checked-out revision differs from approval')
      }
    }

    return tmpRoot
  } catch (err) {
    await fsp.rm(tmpRoot, { recursive: true, force: true }).catch(() => undefined)
    throw err
  }
}

async function resolvePluginRoot(cloneRoot: string, subdir: string | null): Promise<string> {
  if (!subdir) {
    return cloneRoot
  }

  const resolved = resolveSubdirWithin(cloneRoot, subdir)
  let current = path.resolve(cloneRoot)

  // Walk component by component without following links: a symlinked component
  // would let a hostile repository point the "plugin" at an arbitrary directory.
  for (const segment of subdir.split('/')) {
    current = path.join(current, segment)

    const stat = await fsp.lstat(current).catch(() => null)

    if (stat?.isSymbolicLink()) {
      throw refuse('plugin subdirectory traverses a symbolic link')
    }

    if (!stat?.isDirectory()) {
      throw new Error(`Plugin subdirectory '${subdir}' does not exist in the repository.`)
    }
  }

  return resolved
}

function insecureSchemeWarnings(gitUrl: string): { warnings: string[]; insecure: boolean } {
  if (gitUrl.startsWith('http://') || gitUrl.startsWith('file://')) {
    return {
      warnings: ['This URL uses an insecure or local scheme. Prefer https:// or git@ for production installs.'],
      insecure: true
    }
  }

  return { warnings: [], insecure: false }
}

export async function probePluginRepo(gitBin: string, identifier: string): Promise<PluginProbeResult> {
  try {
    const { gitUrl, subdir, revision } = resolvePluginGitUrl(identifier)
    const { warnings, insecure } = insecureSchemeWarnings(gitUrl)
    const cloneRoot = await cloneToTemp(gitBin, gitUrl, revision)

    try {
      const pluginRoot = await resolvePluginRoot(cloneRoot, subdir)
      const detected = await detectPluginComponents(pluginRoot)
      const repoFallback = repoNameFromUrl(gitUrl)

      if (!detected.agent && !detected.desktop) {
        return {
          ok: false,
          agent: false,
          desktop: false,
          warnings,
          insecure,
          error: 'No agent or desktop plugin artifacts found in this repository.'
        }
      }

      return {
        ok: true,
        agent: detected.agent,
        desktop: detected.desktop,
        agentName: detected.agentName ?? (detected.agent ? repoFallback : null),
        desktopName: detected.desktop ? desktopPluginFolderName(gitUrl, subdir) : null,
        revision: await headRevision(gitBin, cloneRoot),
        warnings,
        insecure
      }
    } finally {
      await fsp.rm(cloneRoot, { recursive: true, force: true }).catch(() => undefined)
    }
  } catch (err) {
    return {
      ok: false,
      agent: false,
      desktop: false,
      warnings: [],
      insecure: false,
      error: err instanceof Error ? err.message : String(err)
    }
  }
}

interface TreeEntry {
  kind: 'dir' | 'file'
  rel: string
  size: number
}

/** Inventory a tree without following links. Symlinks, devices, FIFOs and
 *  sockets are refused: a plugin is a plain tree of regular files, and a link
 *  could be redirected outside the plugin directory after it is installed. */
async function inspectPluginTree(root: string): Promise<TreeEntry[]> {
  if (!(await lstatOrNull(root))?.isDirectory()) {
    throw refuse('plugin tree root is not a plain directory')
  }

  const entries: TreeEntry[] = []

  async function walk(dir: string, rel: string, depth: number): Promise<void> {
    if (depth > MAX_TREE_DEPTH) {
      throw refuse('plugin tree is nested too deeply')
    }

    for (const dirent of await fsp.readdir(dir, { withFileTypes: true })) {
      const childRel = rel ? `${rel}/${dirent.name}` : dirent.name
      const child = path.join(dir, dirent.name)
      const stat = await fsp.lstat(child)

      if (stat.isSymbolicLink()) {
        throw refuse(`plugin tree contains a symbolic link (${childRel})`)
      }

      if (stat.isDirectory()) {
        entries.push({ kind: 'dir', rel: childRel, size: 0 })
        await walk(child, childRel, depth + 1)
      } else if (stat.isFile()) {
        entries.push({ kind: 'file', rel: childRel, size: stat.size })
      } else {
        throw refuse(`plugin tree contains a special file (${childRel})`)
      }

      if (entries.length > MAX_TREE_ENTRIES) {
        throw refuse('plugin tree has too many entries')
      }
    }
  }

  await walk(root, '', 0)

  return entries.sort((a, b) => (a.rel < b.rel ? -1 : a.rel > b.rel ? 1 : 0))
}

function sameTree(a: TreeEntry[], b: TreeEntry[]): boolean {
  return a.length === b.length && a.every((e, i) => e.rel === b[i].rel && e.kind === b[i].kind && e.size === b[i].size)
}

async function fileDigest(file: string): Promise<string> {
  return createHash('sha256')
    .update(await fsp.readFile(file))
    .digest('hex')
}

async function lstatOrNull(target: string): Promise<fs.Stats | null> {
  try {
    return await fsp.lstat(target)
  } catch (err) {
    if ((err as NodeJS.ErrnoException)?.code === 'ENOENT') {
      return null
    }

    throw err
  }
}

/** Refuse linked root/parent components before creating or mutating a tree.
 * Missing components may be created, but inaccessible or non-directory parents
 * are errors, never a reason to fall back to another root. */
async function assertPlainDirectoryPath(directory: string): Promise<void> {
  let current = path.resolve(directory)

  while (true) {
    const stat = await lstatOrNull(current)

    if (stat && (stat.isSymbolicLink() || !stat.isDirectory())) {
      throw refuse('plugin root or its parent is a symbolic link or non-directory')
    }

    const parent = path.dirname(current)

    if (parent === current) {
      return
    }

    current = parent
  }
}

/**
 * Validate the destination as one strict child of the canonical plugin root.
 * Called before staging and again immediately before the swap. It trusts
 * nothing about how the folder name was derived.
 */
async function validatedDestination(
  desktopPluginsRoot: string,
  pluginName: string,
  canonicalRoot: string,
  rootIdentity: fs.Stats
): Promise<{ stat: fs.Stats | null; targetDir: string }> {
  const name = assertPortableChildName(pluginName)

  await assertPlainDirectoryPath(desktopPluginsRoot)

  const rootStat = await lstatOrNull(canonicalRoot)

  if (!rootStat?.isDirectory() || rootStat.dev !== rootIdentity.dev || rootStat.ino !== rootIdentity.ino) {
    throw refuse('plugin root identity changed during install')
  }

  if ((await fsp.realpath(desktopPluginsRoot)) !== canonicalRoot) {
    throw refuse('plugin root changed during install')
  }

  const targetDir = path.join(canonicalRoot, name)

  if (path.dirname(targetDir) !== canonicalRoot || path.basename(targetDir) !== name) {
    throw refuse('destination is not a direct child of the plugin root')
  }

  const stat = await lstatOrNull(targetDir)

  if (stat?.isSymbolicLink()) {
    throw refuse('destination is a symbolic link')
  }

  if (stat && !stat.isDirectory()) {
    throw refuse('destination exists and is not a directory')
  }

  return { stat, targetDir }
}

const EXISTS = (name: string) => `Desktop plugin '${name}' already exists. Enable force reinstall to replace it.`

/**
 * Stage, verify, then swap with a recoverable previous tree. The destination
 * child is the only directory ever renamed or replaced; the only recursive
 * removal is this installer's own private staging directory.
 */
async function replaceDesktopPlugin(
  desktopPluginsRoot: string,
  pluginName: string,
  sourceDir: string,
  force: boolean
): Promise<DesktopPluginInstallResult> {
  assertPortableChildName(pluginName)
  await assertPlainDirectoryPath(desktopPluginsRoot)
  await fsp.mkdir(desktopPluginsRoot, { recursive: true })

  const canonicalRoot = await fsp.realpath(desktopPluginsRoot)

  if (!(await pathIsDirectory(canonicalRoot))) {
    throw refuse('plugin root is not a directory')
  }

  const rootIdentity = await fsp.lstat(canonicalRoot)
  const first = await validatedDestination(desktopPluginsRoot, pluginName, canonicalRoot, rootIdentity)

  if (first.stat && !force) {
    return { ok: false, error: EXISTS(pluginName) }
  }

  const sourceStat = await lstatOrNull(sourceDir)

  if (!sourceStat?.isDirectory()) {
    throw refuse('plugin source is not a plain directory')
  }

  const sourceTree = await inspectPluginTree(sourceDir)

  // Private (0700) same-filesystem staging inside the plugin root, so the final
  // renames are atomic. STAGE_PREFIX can never be a valid plugin name.
  const stage = await fsp.mkdtemp(path.join(canonicalRoot, STAGE_PREFIX))
  const stageIdentity = await fsp.lstat(stage)
  const candidate = path.join(stage, 'candidate')
  const previous = path.join(stage, 'previous')
  let retained = false

  async function validateStage(): Promise<void> {
    await assertPlainDirectoryPath(desktopPluginsRoot)

    const rootStat = await lstatOrNull(canonicalRoot)
    const stat = await lstatOrNull(stage)

    if (
      !rootStat?.isDirectory() ||
      rootStat.dev !== rootIdentity.dev ||
      rootStat.ino !== rootIdentity.ino ||
      !stat?.isDirectory() ||
      stat.dev !== stageIdentity.dev ||
      stat.ino !== stageIdentity.ino
    ) {
      throw refuse(`installer staging or root changed; retained state requires review at ${stage}`)
    }
  }

  try {
    await validateStage()
    await fsp.cp(sourceDir, candidate, {
      recursive: true,
      force: false,
      errorOnExist: true,
      dereference: false,
      verbatimSymlinks: true
    })

    if (!sameTree(sourceTree, await inspectPluginTree(candidate))) {
      throw new Error('Staged plugin does not match the cloned source.')
    }

    const candidateEntry = path.join(candidate, 'plugin.js')

    if (!(await lstatOrNull(candidateEntry))?.isFile()) {
      return { ok: false, error: 'Staged plugin is missing plugin.js; the existing plugin was not changed.' }
    }

    if ((await fileDigest(path.join(sourceDir, 'plugin.js'))) !== (await fileDigest(candidateEntry))) {
      throw new Error('Staged plugin.js does not match the cloned source.')
    }

    // Authoritative re-check immediately before the swap.
    const second = await validatedDestination(desktopPluginsRoot, pluginName, canonicalRoot, rootIdentity)

    if (second.stat && !force) {
      return { ok: false, error: EXISTS(pluginName) }
    }

    if (second.stat) {
      await fsp.rename(second.targetDir, previous)
    }

    try {
      await validateStage()

      if (!(await lstatOrNull(candidate))?.isDirectory()) {
        throw refuse('staged candidate is not a plain directory')
      }

      await validatedDestination(desktopPluginsRoot, pluginName, canonicalRoot, rootIdentity)
      await fsp.rename(candidate, second.targetDir)
    } catch (publishErr) {
      if (second.stat) {
        try {
          await validateStage()
          await validatedDestination(desktopPluginsRoot, pluginName, canonicalRoot, rootIdentity)

          if (await lstatOrNull(second.targetDir)) {
            throw refuse('destination appeared before rollback; previous plugin retained')
          }

          await fsp.rename(previous, second.targetDir)
        } catch {
          retained = true

          return {
            ok: false,
            error:
              'Install failed and the previous plugin could not be restored automatically. ' +
              `It is preserved for recovery at ${previous}.`
          }
        }
      }

      throw publishErr
    }

    return { ok: true, pluginName, path: path.join(desktopPluginsRoot, pluginName) }
  } finally {
    if (!retained) {
      // Cleanup never follows a changed parent or a staging directory whose
      // identity we no longer own. Retain ambiguous state for review.
      await validateStage()
      await fsp.rm(stage, { recursive: true, force: true })
    }
  }
}

// One main-process installer may own a target at a time. This is not a claim
// of cross-process exclusion against a user who can mutate the profile home.
const activeDesktopInstallTargets = new Set<string>()

export async function installDesktopPluginFromGit(
  gitBin: string,
  identifier: string,
  desktopPluginsRoot: string,
  force = false,
  approvedRevision?: unknown
): Promise<DesktopPluginInstallResult> {
  let ownedTarget: string | null = null

  try {
    const { gitUrl, subdir, revision } = resolvePluginGitUrl(identifier)
    // Refuse an unsafe destination before any clone or filesystem work.
    const pluginName = desktopPluginFolderName(gitUrl, subdir)
    const approved = exactRevision(approvedRevision)

    if (revision && revision !== approved) {
      throw refuse('source revision differs from inspected approval')
    }

    const targetKey = path.join(path.resolve(desktopPluginsRoot), pluginName)

    if (activeDesktopInstallTargets.has(targetKey)) {
      return { ok: false, error: `An installation for '${pluginName}' is already in progress.` }
    }

    activeDesktopInstallTargets.add(targetKey)
    ownedTarget = targetKey

    await assertPlainDirectoryPath(desktopPluginsRoot)

    const cloneRoot = await cloneToTemp(gitBin, gitUrl, approved)

    try {
      const pluginRoot = await resolvePluginRoot(cloneRoot, subdir)
      const detected = await detectPluginComponents(pluginRoot)

      if (!detected.desktop || !detected.desktopSourceSubdir) {
        return { ok: false, error: 'No desktop plugin.js found in this repository.' }
      }

      const sourceDir =
        detected.desktopSourceSubdir === '.' ? pluginRoot : path.join(pluginRoot, detected.desktopSourceSubdir)

      const result = await replaceDesktopPlugin(desktopPluginsRoot, pluginName, sourceDir, force)

      return result.ok ? { ...result, revision: approved } : result
    } finally {
      await fsp.rm(cloneRoot, { recursive: true, force: true }).catch(() => undefined)
    }
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : String(err) }
  } finally {
    if (ownedTarget) {
      activeDesktopInstallTargets.delete(ownedTarget)
    }
  }
}

/** Resolve git binary via execFile which path on unix; caller passes Windows-resolved path. */
export function runGitVersion(gitBin: string): Promise<boolean> {
  return new Promise(resolve => {
    execFile(gitBin, ['--version'], { windowsHide: true, timeout: 5_000 }, err => {
      resolve(!err)
    })
  })
}
