import { afterEach, describe, expect, it, vi } from 'vitest'

const handlers = vi.hoisted(() => new Map<string, (...args: any[]) => any>())
vi.mock('electron', () => ({
  ipcMain: { handle: (name: string, fn: (...args: any[]) => any) => handlers.set(name, fn) },
  shell: {}
}))

import { DESTINATION_REVISION, destinationCases, exerciseDestinationCase } from './fixtures/plugin-destination-cases'
import { registerFsIpc } from './fs-ipc'

// RIC-01 gate. `electron` is replaced only by a registration facade so the REAL
// `hermes:plugin:installDesktop` handler from fs-ipc.ts is captured; the
// installer, child spawn, clone staging, filesystem and rollback are the
// production modules. This proves the handler logic, not Electron's IPC
// transport/sender validation (that needs the native E2E lane, not claimed here).
afterEach(() => handlers.clear())

describe.skipIf(process.platform === 'win32')('RIC-01 registered IPC plugin destination', () => {
  it('registers the real installDesktop handler', () => {
    registerFsIpc({
      hermesHome: '/nonexistent',
      readActiveDesktopProfile: () => null,
      expandUserPath: v => v,
      resolveRequestedPathForIpc: v => v,
      directoryExists: () => true,
      resolveGitBinary: () => '/nonexistent'
    })
    expect(handlers.get('hermes:plugin:installDesktop')).toBeTypeOf('function')
  })

  it('refuses a concurrent owner without a second clone or mutation', async () => {
    await exerciseDestinationCase(
      { label: 'concurrent-owner', identifier: 'owner/alerts', force: true, expected: 'installed' },
      async (home, git, identifier, force) => {
        registerFsIpc({
          hermesHome: home,
          readActiveDesktopProfile: () => null,
          expandUserPath: value => value,
          resolveRequestedPathForIpc: value => value,
          directoryExists: () => true,
          resolveGitBinary: () => git
        })

        const invoke = handlers.get('hermes:plugin:installDesktop')!
        const first = invoke({}, { identifier, force, revision: DESTINATION_REVISION })
        const denied = await invoke({}, { identifier, force, revision: DESTINATION_REVISION })

        expect(denied).toMatchObject({ ok: false })
        expect(denied.error).toMatch(/already in progress/)

        return first
      }
    )
  }, 15_000)

  for (const testCase of destinationCases) {
    it(
      testCase.label,
      async () => {
        const witness = await exerciseDestinationCase(testCase, async (home, git, identifier, force, profile) => {
          registerFsIpc({
            hermesHome: home,
            readActiveDesktopProfile: () => profile ?? null,
            expandUserPath: value => value,
            resolveRequestedPathForIpc: value => value,
            directoryExists: () => true,
            resolveGitBinary: () => git
          })

          return handlers.get('hermes:plugin:installDesktop')!(
            {},
            { identifier, force, revision: DESTINATION_REVISION }
          )
        })

        expect(witness.outsideWrites).toBe(0)
      },
      15_000
    )
  }
})

describe.skipIf(process.platform === 'win32')('RIC-03 inspected revision through real registered adapters', () => {
  it('installs inspected bytes after default branch moves and refuses foreign/malformed commits', async () => {
    const { exerciseRevisionCases } = await import('./fixtures/plugin-revision-cases')

    const witness = await exerciseRevisionCases(home => {
      registerFsIpc({
        hermesHome: home,
        readActiveDesktopProfile: () => null,
        expandUserPath: value => value,
        resolveRequestedPathForIpc: value => value,
        directoryExists: () => true,
        resolveGitBinary: () => 'git'
      })

      return {
        probe: identifier => handlers.get('hermes:plugin:probe')!({}, { identifier }),
        install: (identifier, revision) =>
          handlers.get('hermes:plugin:installDesktop')!({}, { identifier, revision, force: true })
      }
    })

    expect(witness).toEqual({ layouts: 4, imports: 0, branchMoves: 4, denials: 36 })
  }, 30_000)
})

it('does not trim malformed source commit tokens at the IPC boundary', async () => {
  registerFsIpc({
    hermesHome: '/nonexistent',
    readActiveDesktopProfile: () => null,
    expandUserPath: v => v,
    resolveRequestedPathForIpc: v => v,
    directoryExists: () => true,
    resolveGitBinary: () => '/nonexistent-git'
  })
  const identifier = `https://github.com/o/repo/commit/${'a'.repeat(40)} `
  const result = await handlers.get('hermes:plugin:probe')!({}, { identifier })
  expect(result).toMatchObject({ ok: false })
  expect(result.error).toMatch(/unsupported or ambiguous/)
})
