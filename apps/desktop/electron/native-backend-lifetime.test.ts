import { expect, test, vi } from 'vitest'

import {
  bindNativeBackendLifetime,
  bindNativeBackendPromise,
  claimNativeBackendLifetime
} from './native-backend-lifetime'
const descriptor = { baseUrl: 'https://inert.invalid', token: 'synthetic', authMode: 'token', headers: {} }

function fixture() {
  const promise = Promise.resolve(descriptor)
  let current: Promise<any> | null = promise
  let source = 'source_1'
  const bound = () => bindNativeBackendPromise(promise, () => current)

  const deps = {
    primary: vi.fn(bound),
    registry: vi.fn(async () => ({ ...(await bound()) })),
    currentDocument: vi.fn(),
    source: () => source
  }

  return {
    deps,
    replace: () => {
      current = Promise.resolve({ ...descriptor })
    },
    edit: () => {
      source = 'source_2'
    }
  }
}

test.each([
  { kind: 'primary' as const },
  { kind: 'profile' as const, profile: 'research' },
  { kind: 'registry' as const, connectionId: 'source', profile: 'research' }
])('route %j pins its actual owner even through descriptor spreads', async route => {
  const f = fixture()
  const pinned = await claimNativeBackendLifetime(route, f.deps)

  if (route.kind === 'registry') {
    expect(f.deps.registry).toHaveBeenCalledWith('source', 'research')
    expect(f.deps.primary).not.toHaveBeenCalled()
  } else {
    expect(f.deps.primary).toHaveBeenCalledWith(route.kind === 'profile' ? 'research' : null)
    expect(f.deps.registry).not.toHaveBeenCalled()
  }

  expect(pinned.lifetimeCurrent()).toBe(true)
  f.replace()
  expect(pinned.lifetimeCurrent()).toBe(false)
  f.edit()
  expect(pinned.sourceCurrent()).toBe(false)
})
test('duplicate descriptors retire with the claimed pool, not an unrelated primary', async () => {
  const primary = Promise.resolve({ ...descriptor })
  const pool = Promise.resolve({ ...descriptor })
  let primaryCurrent: Promise<any> | null = primary
  let poolCurrent: Promise<any> | null = pool

  const deps = {
    primary: async () => bindNativeBackendPromise(pool, () => poolCurrent),
    registry: async () => bindNativeBackendPromise(primary, () => primaryCurrent),
    currentDocument: () => {},
    source: () => 'stable'
  }

  const pinned = await claimNativeBackendLifetime({ kind: 'profile', profile: 'pool' }, deps)
  primaryCurrent = null
  expect(pinned.lifetimeCurrent()).toBe(true)
  poolCurrent = null
  expect(pinned.lifetimeCurrent()).toBe(false)
})
test('unbound or stale value-equal descriptors cannot acquire a lifetime', async () => {
  const f = fixture()
  f.deps.primary.mockImplementation(async () => descriptor)
  await expect(claimNativeBackendLifetime({ kind: 'primary' }, f.deps)).rejects.toThrow('lifetime unavailable')
  const stale = fixture()
  stale.replace()
  await expect(claimNativeBackendLifetime({ kind: 'primary' }, stale.deps)).rejects.toThrow('lifetime unavailable')
})
test('revalidation preserves the replacement producer, not the retired outer cache', async () => {
  const retired = Promise.resolve(descriptor),
    replacement = Promise.resolve({ ...descriptor })

  const fresh = await bindNativeBackendPromise(replacement, () => replacement)
  const connection = bindNativeBackendLifetime(fresh, { promise: retired, current: () => null })

  const pinned = await claimNativeBackendLifetime(
    { kind: 'primary' },
    {
      primary: async () => connection,
      registry: async () => connection,
      currentDocument: () => {},
      source: () => 'stable'
    }
  )

  expect(pinned.lifetimeCurrent()).toBe(true)
})
test('private owner binding does not cross structured clone or JSON', async () => {
  const connection = await fixture().deps.primary()
  expect(structuredClone(connection)).toEqual(descriptor)
  expect(JSON.parse(JSON.stringify(connection))).toEqual(descriptor)
})
test('document and material source changes after claim deny lifetime publication', async () => {
  const f = fixture()
  f.deps.primary.mockImplementation(async () => {
    f.edit()

    return descriptor
  })
  await expect(claimNativeBackendLifetime({ kind: 'primary' }, f.deps)).rejects.toThrow('source changed')
  const second = fixture()
  second.deps.currentDocument
    .mockImplementationOnce(() => {})
    .mockImplementation(() => {
      throw new Error('document retired')
    })
  await expect(claimNativeBackendLifetime({ kind: 'primary' }, second.deps)).rejects.toThrow('document retired')
})
