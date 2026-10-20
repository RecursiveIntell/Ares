import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { afterEach, describe, expect, it } from 'vitest'

import {
  assertPortableChildName,
  desktopPluginFolderName,
  detectPluginComponents,
  findDesktopEntry,
  repoNameFromUrl,
  resolvePluginGitUrl,
  resolveSubdirWithin
} from './desktop-plugin-install'

const here = path.dirname(fileURLToPath(import.meta.url))

function mkdtemp(prefix: string) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix))
}

describe('resolvePluginGitUrl', () => {
  it('maps owner/repo shorthand to github git url', () => {
    expect(resolvePluginGitUrl('NousResearch/hermes-example-plugins')).toEqual({
      gitUrl: 'https://github.com/NousResearch/hermes-example-plugins.git',
      subdir: null
    })
  })

  it('supports monorepo subdir shorthand', () => {
    expect(resolvePluginGitUrl('owner/repo/plugins/foo')).toEqual({
      gitUrl: 'https://github.com/owner/repo.git',
      subdir: 'plugins/foo'
    })
  })

  it('supports hash subdir fragment', () => {
    expect(resolvePluginGitUrl('https://github.com/o/r.git#nested/plugin')).toEqual({
      gitUrl: 'https://github.com/o/r.git',
      subdir: 'nested/plugin'
    })
  })
})

describe('repoNameFromUrl', () => {
  it('strips .git suffix', () => {
    expect(repoNameFromUrl('https://github.com/o/my-plugin.git')).toBe('my-plugin')
  })
})

describe('desktopPluginFolderName', () => {
  it('uses the repo name for a root-level plugin, not the clone path', () => {
    expect(desktopPluginFolderName('https://github.com/o/my-plugin.git', null)).toBe('my-plugin')
  })

  it('uses the last meaningful subdir, not a generic desktop folder', () => {
    expect(desktopPluginFolderName('https://github.com/o/monorepo.git', 'plugins/alerts/desktop')).toBe('alerts')
  })
})

describe('resolveSubdirWithin', () => {
  it('rejects path traversal', () => {
    const root = mkdtemp('hermes-plugin-root-')

    expect(() => resolveSubdirWithin(root, '../escape')).toThrow(/escapes/)
  })
})

describe('findDesktopEntry', () => {
  it('finds root plugin.js', () => {
    const root = mkdtemp('hermes-plugin-detect-')
    fs.mkdirSync(path.join(root, 'desktop'), { recursive: true })
    fs.writeFileSync(path.join(root, 'plugin.js'), 'export default {}')

    expect(findDesktopEntry(root)).toEqual({ entryFile: path.join(root, 'plugin.js'), sourceSubdir: '.' })
  })

  it('finds desktop/plugin.js', () => {
    const root = mkdtemp('hermes-plugin-detect-')
    fs.mkdirSync(path.join(root, 'desktop'), { recursive: true })
    fs.writeFileSync(path.join(root, 'desktop', 'plugin.js'), 'export default {}')

    expect(findDesktopEntry(root)).toEqual({
      entryFile: path.join(root, 'desktop', 'plugin.js'),
      sourceSubdir: 'desktop'
    })
  })
})

describe('detectPluginComponents', () => {
  const roots: string[] = []

  afterEach(() => {
    for (const root of roots.splice(0)) {
      fs.rmSync(root, { recursive: true, force: true })
    }
  })

  it('detects agent-only layout', async () => {
    const root = mkdtemp('hermes-plugin-agent-')
    roots.push(root)
    fs.writeFileSync(path.join(root, 'plugin.yaml'), 'name: hello-agent\n')
    fs.writeFileSync(path.join(root, '__init__.py'), 'def register(ctx): pass\n')

    await expect(detectPluginComponents(root)).resolves.toMatchObject({
      agent: true,
      desktop: false,
      agentName: 'hello-agent'
    })
  })

  it('detects dual layout', async () => {
    const root = mkdtemp('hermes-plugin-dual-')
    roots.push(root)
    fs.mkdirSync(path.join(root, 'desktop'), { recursive: true })
    fs.writeFileSync(path.join(root, 'plugin.yaml'), 'name: dual\n')
    fs.writeFileSync(path.join(root, '__init__.py'), 'def register(ctx): pass\n')
    fs.writeFileSync(path.join(root, 'desktop', 'plugin.js'), 'export default { id: "dual-ui" }')

    await expect(detectPluginComponents(root)).resolves.toMatchObject({
      agent: true,
      desktop: true,
      agentName: 'dual',
      desktopName: 'desktop'
    })
  })
})

const UNSAFE_NAMES = [
  '',
  '.',
  '..',
  '...',
  '/',
  'a/b',
  'a\\b',
  '/abs',
  'C:',
  'C:drive',
  'C:\\x',
  '\\\\server\\share',
  'name:stream',
  'CON',
  'con',
  'con.txt',
  'PRN',
  'AUX',
  'NUL',
  'COM1',
  'lpt9.log',
  'trailing.',
  'trailing ',
  ' leading',
  'a\u0000b',
  'a\nb',
  'a*b',
  'a?b',
  'a<b',
  'a|b',
  '.plugin-install-x',
  'a'.repeat(300)
]

describe('assertPortableChildName', () => {
  it.each(UNSAFE_NAMES)('refuses %j', name => {
    expect(() => assertPortableChildName(name)).toThrow(/^Refused unsafe plugin destination: /)
  })

  it.each(['alerts', 'my-plugin', 'plugin_2', '.hidden-ok', 'a.b', 'CONSOLE', 'communicate', 'com10', 'Ünïcode'])(
    'accepts ordinary name %j',
    name => {
      expect(assertPortableChildName(name)).toBe(name)
    }
  )

  it('refuses non-string input', () => {
    expect(() => assertPortableChildName(undefined)).toThrow(/Refused unsafe/)
    expect(() => assertPortableChildName(7)).toThrow(/Refused unsafe/)
  })
})

describe('identifier forms never yield an unsafe destination name', () => {
  const FORMS = [
    'owner/..',
    'owner/.',
    'owner/repo/plugins/..',
    'owner/repo/plugins/./alerts',
    'owner/repo/../../x',
    'https://example.invalid/o/repo.git/..',
    'https://example.invalid/o/repo.git#..',
    'https://example.invalid/o/repo.git#a/../../b',
    'https://example.invalid/o/repo.git#/absolute',
    'https://example.invalid/o/repo.git#//server/share',
    'https://example.invalid/o/repo.git#C:/x',
    'https://example.invalid/o/repo.git#a\\b',
    'https://github.com/o/repo/tree/main/..',
    'https://github.com/o/repo/tree/main/a/../..',
    'https://github.com/../repo/tree/main/x',
    'https://github.com/o/../tree/main/x',
    'https://example.invalid/o/%2e%2e.git',
    'https://example.invalid/o/a%2f..%2fb.git',
    'owner/CON',
    'owner/repo/plugins/NUL'
  ]

  it.each(FORMS)('%s is refused before any clone', identifier => {
    expect(() => {
      const { gitUrl, subdir } = resolvePluginGitUrl(identifier)
      desktopPluginFolderName(gitUrl, subdir)
    }).toThrow(/^Refused unsafe plugin destination: /)
  })

  it.each([
    ['owner/alerts', 'alerts'],
    ['owner/repo/plugins/alerts/desktop', 'alerts'],
    ['https://example.invalid/o/repo.git#plugins/alerts/desktop', 'alerts'],
    ['https://example.invalid/o/repo.git/plugins/alerts/desktop/', 'alerts'],
    ['https://github.com/o/repo/tree/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/plugins/alerts', 'alerts'],
    ['git@example.invalid:o/my-plugin.git', 'my-plugin']
  ])('%s keeps its historical folder name %s', (identifier, expected) => {
    const { gitUrl, subdir } = resolvePluginGitUrl(identifier)

    expect(desktopPluginFolderName(gitUrl, subdir)).toBe(expected)
  })
})

describe('RIC-03 exact browser revision forms', () => {
  const sha = 'A'.repeat(40)
  it.each([
    [`https://github.com/o/repo/tree/${sha}/plugins/alerts`, 'plugins/alerts'],
    [`https://github.com/o/repo/tree/${sha}`, null],
    [`https://github.com/o/repo/blob/${sha}/plugins/alerts/plugin.js`, 'plugins/alerts'],
    [`https://github.com/o/repo/blob/${sha}/plugin.js`, null],
    [`https://github.com/o/repo/commit/${sha}`, null]
  ])('retains source revision in %s', (identifier, subdir) => {
    expect(resolvePluginGitUrl(identifier!)).toEqual({
      gitUrl: 'https://github.com/o/repo.git',
      subdir,
      revision: sha.toLowerCase()
    })
  })
  it.each([
    'tree/main/plugins/alerts',
    'tree/feature/slash/plugins/alerts',
    `tree/${'a'.repeat(39)}/plugin`,
    `tree/${'g'.repeat(40)}/plugin`,
    `tree/${'a'.repeat(40)}%20/plugin`,
    `tree/${'a'.repeat(40)} /plugin`,
    `tree//${'a'.repeat(40)}/plugin`,
    `commit/${'a'.repeat(40)}/extra`,
    `blob/${'a'.repeat(40)}`,
    'issues/123',
    'pull/123',
    'releases/latest'
  ])('refuses unsupported/ambiguous link %s before lookup', suffix => {
    expect(() => resolvePluginGitUrl(`https://github.com/o/repo/${suffix}`)).toThrow(/^Refused/)
  })
  it('never trims an invalid commit token into an accepted revision', () => {
    expect(() => resolvePluginGitUrl(`https://github.com/o/repo/commit/${sha} `)).toThrow(/^Refused/)
  })
})
