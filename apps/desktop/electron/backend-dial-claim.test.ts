import { describe, expect, it, vi } from 'vitest'

import { createBackendConnectionState } from './backend-connection-state'
import { assertDelegatedLocalDialCurrent, BackendDialClaims, type RegistryBackendDial, resolveRegistryDialOptions } from './backend-dial-claim'
import { backendScopeKey, normalizeRegistry, parseBackendScopeKey } from './connection-registry'

const registry = () => normalizeRegistry({
  primary: 'gateway', connections: [
    { id: 'local', kind: 'local', label: 'This device' },
    { id: 'gateway', kind: 'remote', label: 'Gateway', url: 'http://127.0.0.1:38951' },
    { id: 'other', kind: 'remote', label: 'Other', url: 'http://127.0.0.1:38952' }
  ]
})

const options = (globalRemote = true) => ({ globalRemote, profileRemoteOverride: false, primaryProfile: 'default' })

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })

  return { promise, resolve, reject }
}

describe('resolved registry dial admission', () => {
  for (const first of ['local', 'legacy'] as const) {
    it(`keeps configured remote primary and explicit local independent when ${first} starts first`, async () => {
      const claims = new BackendDialClaims()
      const local = deferred<string>()
      const remote = deferred<string>()

      const localDial = vi.fn((route: RegistryBackendDial) => {
        expect(route.source.kind).toBe('local')
        expect(route.localRoute).toEqual({ delegate: false, poolKey: 'conn:local::default' })

        return local.promise
      })

      const remoteDial = vi.fn(() => remote.promise)
      const startLocal = () => claims.runRegistry(registry(), 'local', 'default', options(), localDial)
      const startRemote = () => claims.run(backendScopeKey(null, 'default'), remoteDial)

      const [localResult, remoteResult] = first === 'local'
        ? [startLocal(), startRemote()] : (() => { const result = startRemote();

 return [startLocal(), result] })()

      local.resolve('native-local39609')
      remote.resolve('remote-primary38951')

      expect(await localResult).toBe('native-local39609')
      expect(await remoteResult).toBe('remote-primary38951')
      expect(localDial).toHaveBeenCalledTimes(1)
      expect(remoteDial).toHaveBeenCalledTimes(1)
      expect(claims.inFlight('default')).toBe(false)
      expect(claims.inFlight('conn:local::default')).toBe(false)
    })
  }

  it('coalesces identical forced-local targets while the legacy remote stays pending', async () => {
    const claims = new BackendDialClaims()
    const local = deferred<string>()
    const dial = vi.fn(() => local.promise)
    const a = claims.runRegistry(registry(), 'local', 'work', options(), dial)
    const b = claims.runRegistry(registry(), 'local', ' work ', options(), dial)
    expect(a).toBe(b)
    expect(dial).toHaveBeenCalledTimes(1)
    local.resolve('local-work')
    expect(await b).toBe('local-work')
  })

  it('coalesces registry-local with the legacy backend only when the route delegates', async () => {
    const claims = new BackendDialClaims()
    const ready = deferred<string>()
    const legacy = claims.run('default', () => ready.promise)
    const extra = vi.fn(() => 'duplicate')
    const local = claims.runRegistry(registry(), 'local', null, options(false), extra)
    expect(local).toBe(legacy)
    expect(extra).not.toHaveBeenCalled()
    ready.resolve('same-local')
    expect(await local).toBe('same-local')
  })

  it('delegated blank-profile registry requests coalesce with the actual selected primary profile', async () => {
    const claims = new BackendDialClaims()
    const ready = deferred<string>()
    const legacy = claims.run('work', () => ready.promise)
    const extra = vi.fn(() => 'duplicate')
    const local = claims.runRegistry(registry(), 'local', null, { ...options(false), primaryProfile: 'work' }, extra)
    expect(local).toBe(legacy)
    expect(extra).not.toHaveBeenCalled()
    ready.resolve('same-work')
    expect(await local).toBe('same-work')
  })

  it('pins the delegated primary profile before an async dial can observe a later selection', async () => {
    const claims = new BackendDialClaims()
    const opts = { ...options(false), primaryProfile: 'work' }
    const ready = deferred<void>()

    const dial = claims.runRegistry(registry(), 'local', null, opts, async route => {
      await ready.promise

      return route.delegatedProfile
    })

    opts.primaryProfile = 'later'
    ready.resolve()
    expect(await dial).toBe('work')
  })

  it('rejects a late remote configuration instead of retargeting an admitted local delegate', async () => {
    const claims = new BackendDialClaims()
    const current = options(false)
    const ready = deferred<void>()
    const transport = vi.fn(() => 'retargeted')

    const old = claims.runRegistry(registry(), 'local', 'default', current, async route => {
      await ready.promise
      assertDelegatedLocalDialCurrent(route, current)

      return transport()
    })

    current.globalRemote = true
    const rejection = expect(old).rejects.toThrow('superseded by a remote route')
    ready.resolve()
    await rejection
    expect(transport).not.toHaveBeenCalled()
    expect(claims.inFlight('default')).toBe(false)
    expect(await claims.runRegistry(registry(), 'local', 'default', current, route => {
      assertDelegatedLocalDialCurrent(route, current)

      return route.localRoute?.poolKey
    })).toBe('conn:local::default')
  })

  it('checks a captured nondefault primary for a late per-profile remote override', async () => {
    const claims = new BackendDialClaims()
    const overrides = new Map<string, boolean>()
    const lookup = vi.fn((profile: string) => overrides.get(profile))
    const initial = resolveRegistryDialOptions(null, 'work', false, lookup)
    const ready = deferred<void>()
    const transport = vi.fn(() => 'retargeted')

    const old = claims.runRegistry(registry(), 'local', null, initial, async route => {
      await ready.promise
      const current = resolveRegistryDialOptions(route.delegatedProfile ?? route.profile, 'later', false, lookup)
      assertDelegatedLocalDialCurrent(route, current)

      return transport()
    })

    overrides.set('work', true)
    const rejection = expect(old).rejects.toThrow('superseded by a remote route')
    ready.resolve()
    await rejection
    expect(lookup.mock.calls.map(([profile]) => profile)).toEqual(['work', 'work'])
    expect(transport).not.toHaveBeenCalled()
    expect(claims.inFlight('work')).toBe(false)
    const subsequent = resolveRegistryDialOptions(null, 'work', false, lookup)
    expect(await claims.runRegistry(registry(), 'local', null, subsequent, route => route.localRoute))
      .toEqual({ delegate: false, poolKey: 'conn:local::default' })
  })

  it('uses the actual local resolver for per-profile remote overrides', async () => {
    const claims = new BackendDialClaims()
    const legacy = deferred<string>()
    const remote = claims.run('work', () => legacy.promise)

    const local = claims.runRegistry(registry(), 'local', 'work',
      { ...options(false), profileRemoteOverride: true }, route => {
        expect(route.localRoute).toEqual({ delegate: false, poolKey: 'conn:local::work' })

        return 'local-work'
      })

    legacy.resolve('remote-work')
    expect(await local).toBe('local-work')
    expect(await remote).toBe('remote-work')
  })

  it('resolves blank connection IDs before admission and keeps source/profile pairs independent', async () => {
    const claims = new BackendDialClaims()
    const ready = deferred<string>()
    const dial = vi.fn(() => ready.promise)
    const a = claims.runRegistry(registry(), '', 'default', options(), dial)
    const b = claims.runRegistry(registry(), 'gateway', 'default', options(), dial)
    expect(a).toBe(b)
    expect(dial).toHaveBeenCalledTimes(1)
    const other = claims.runRegistry(registry(), 'other', 'default', options(), route => route.source.id)
    const work = claims.runRegistry(registry(), 'gateway', 'work', options(), route => route.profile)
    ready.resolve('gateway-default')
    expect(await a).toBe('gateway-default')
    expect(await other).toBe('other')
    expect(await work).toBe('work')
  })

  it('rejects a missing source before it can borrow an existing claim', async () => {
    const claims = new BackendDialClaims()
    const ready = deferred<string>()
    const live = claims.runRegistry(registry(), 'gateway', 'default', options(), () => ready.promise)
    const removed = registry()
    removed.connections = removed.connections.filter(source => source.id !== 'gateway')
    const dial = vi.fn(() => 'wrong')
    await expect(claims.runRegistry(removed, 'gateway', 'default', options(), dial)).rejects.toThrow('No connection')
    expect(dial).not.toHaveBeenCalled()
    ready.resolve('owned')
    expect(await live).toBe('owned')
  })

  it('keeps the resolved route across an async factory wait and later configuration changes', async () => {
    const claims = new BackendDialClaims()
    const opts = options()
    const current = registry()
    const ready = deferred<void>()

    const first = claims.runRegistry(current, 'local', 'default', opts, async route => {
      await ready.promise

      return { id: route.connectionId, source: route.source.kind, localRoute: route.localRoute }
    })

    opts.globalRemote = false
    current.primary = 'other'
    current.connections = current.connections.filter(source => source.id !== 'local')
    const next = claims.runRegistry(registry(), 'local', 'default', opts, route => route.localRoute)
    ready.resolve()
    expect(await first).toEqual({ id: 'local', source: 'local', localRoute: { delegate: false, poolKey: 'conn:local::default' } })
    expect(await next).toEqual({ delegate: true, poolKey: 'default' })
  })

  it('releases cancelled and failed local dials without cancelling the independent primary', async () => {
    const claims = new BackendDialClaims()
    const primaryReady = deferred<string>()
    const primary = claims.run('default', () => primaryReady.promise)
    const controller = new AbortController()

    const cancelled = claims.runRegistry(registry(), 'local', 'default', options(), () => new Promise<string>((_resolve, reject) => {
      controller.signal.addEventListener('abort', () => reject(new Error('dial cancelled')), { once: true })
    }))

    const waiter = claims.runRegistry(registry(), 'local', 'default', options(), () => 'duplicate')
    const outcomes = Promise.allSettled([cancelled, waiter])
    controller.abort()
    expect((await outcomes).every(result => result.status === 'rejected')).toBe(true)
    expect(claims.inFlight('conn:local::default')).toBe(false)
    expect(claims.inFlight('default')).toBe(true)
    await expect(claims.runRegistry(registry(), 'local', 'default', options(), () => { throw new Error('dial failed') })).rejects.toThrow('dial failed')
    expect(claims.inFlight('conn:local::default')).toBe(false)
    expect(await claims.runRegistry(registry(), 'local', 'default', options(), () => 'replacement')).toBe('replacement')
    primaryReady.resolve('unchanged-primary')
    expect(await primary).toBe('unchanged-primary')
  })

  it('retains production generation and process-owner fences through cancellation, replacement and stale cleanup', async () => {
    const claims = new BackendDialClaims()
    const state = createBackendConnectionState<{ id: string }, string>()
    const oldAttempt = state.startAttempt()
    const oldTransport = deferred<string>()
    const old = claims.runRegistry(registry(), 'local', 'default', options(), () => oldTransport.promise)
    state.setPromise(oldAttempt, old)
    const oldOwner = state.attachProcess(oldAttempt, { id: 'old' })!
    const rejection = expect(old).rejects.toThrow('cancelled')
    state.invalidate()
    oldTransport.reject(new Error('cancelled'))
    await rejection
    const nextAttempt = state.startAttempt()
    const nextReady = deferred<string>()
    const next = claims.runRegistry(registry(), 'local', 'default', options(), () => nextReady.promise)
    state.setPromise(nextAttempt, next)
    const nextProcess = { id: 'replacement' }
    const nextOwner = state.attachProcess(nextAttempt, nextProcess)!
    expect(state.attachProcess(oldAttempt, { id: 'late-old' })).toBeNull()
    expect(state.setPromise(oldAttempt, Promise.resolve('late-old'))).toBe(false)
    expect(state.clearPromiseForAttempt(oldAttempt)).toBe(false)
    expect(state.clearForCurrentProcess(oldOwner)).toBe(false)
    expect(state.getPromise()).toBe(next)
    expect(state.getProcess()).toBe(nextProcess)
    expect(claims.inFlight('conn:local::default')).toBe(true)
    nextReady.resolve('replacement')
    expect(await next).toBe('replacement')
    expect(claims.inFlight('conn:local::default')).toBe(false)
    expect(state.clearForCurrentProcess(nextOwner)).toBe(true)
    expect(state.getPromise()).toBeNull()
  })
})

describe('BackendDialClaims (#90812)', () => {
  it('coalesces two concurrent dials for the same (connectionId, profile) onto ONE backend spawn', async () => {
    const claims = new BackendDialClaims()
    let spawns = 0
    let resolveSpawn: ((value: { baseUrl: string }) => void) | undefined

    const dial = vi.fn(() => {
      spawns += 1

      return new Promise<{ baseUrl: string }>(resolve => {
        resolveSpawn = resolve
      })
    })

    // Two renderer windows race the same reconnect: reconnectGateway()'s
    // in-flight lock is per-renderer, so BOTH invoke the main-process dial.
    const first = claims.run('conn:office-ssh::default', dial)
    const second = claims.run('conn:office-ssh::default', dial)

    expect(spawns).toBe(1)

    resolveSpawn?.({ baseUrl: 'http://127.0.0.1:53150' })

    const [firstResult, secondResult] = await Promise.all([first, second])

    // The second caller receives the FIRST dial's result, not its own spawn.
    expect(firstResult).toBe(secondResult)
    expect(firstResult).toEqual({ baseUrl: 'http://127.0.0.1:53150' })
    expect(dial).toHaveBeenCalledTimes(1)
  })

  it('scopes claims by key: different (connectionId, profile) pairs dial independently', async () => {
    const claims = new BackendDialClaims()
    const dialA = vi.fn(async () => 'a')
    const dialB = vi.fn(async () => 'b')

    const [a, b] = await Promise.all([
      claims.run('conn:office-ssh::default', dialA),
      claims.run('conn:office-ssh::work', dialB)
    ])

    expect(a).toBe('a')
    expect(b).toBe('b')
    expect(dialA).toHaveBeenCalledTimes(1)
    expect(dialB).toHaveBeenCalledTimes(1)
  })

  it('releases the claim once the dial settles so a later reconnect can dial again (bounded, not latched)', async () => {
    const claims = new BackendDialClaims()
    const dial = vi.fn(async () => 'fresh')

    await claims.run('default', dial)
    expect(claims.inFlight('default')).toBe(false)

    await claims.run('default', dial)
    expect(dial).toHaveBeenCalledTimes(2)
  })

  it('propagates a failed dial to every coalesced waiter and never caches the rejection', async () => {
    const claims = new BackendDialClaims()
    let rejectSpawn: ((error: Error) => void) | undefined

    const failingDial = vi.fn(
      () =>
        new Promise<never>((_resolve, reject) => {
          rejectSpawn = reject
        })
    )

    const first = claims.run('conn:office-ssh::default', failingDial)
    const second = claims.run('conn:office-ssh::default', failingDial)
    expect(failingDial).toHaveBeenCalledTimes(1)

    rejectSpawn?.(new Error('ssh dial failed'))

    await expect(first).rejects.toThrow('ssh dial failed')
    await expect(second).rejects.toThrow('ssh dial failed')

    // Fail closed but not latched: the NEXT dial attempt runs fresh.
    const recovered = vi.fn(async () => 'recovered')
    await expect(claims.run('conn:office-ssh::default', recovered)).resolves.toBe('recovered')
    expect(recovered).toHaveBeenCalledTimes(1)
  })

  it('a synchronously-throwing dial rejects the claim instead of escaping the coalescing seam', async () => {
    const claims = new BackendDialClaims()

    await expect(
      claims.run('default', () => {
        throw new Error('spawn refused')
      })
    ).rejects.toThrow('spawn refused')

    expect(claims.inFlight('default')).toBe(false)
  })
})

describe('parseBackendScopeKey (#90812/#93910)', () => {
  it('round-trips the composite pool key back to (connectionId, profile)', () => {
    expect(parseBackendScopeKey('conn:office-ssh::default')).toEqual({
      connectionId: 'office-ssh',
      profile: 'default'
    })
    expect(parseBackendScopeKey('conn:office-ssh::work')).toEqual({ connectionId: 'office-ssh', profile: 'work' })
  })

  it('treats a bare profile key as the local/primary scope', () => {
    expect(parseBackendScopeKey('default')).toEqual({ connectionId: null, profile: 'default' })
    expect(parseBackendScopeKey('work')).toEqual({ connectionId: null, profile: 'work' })
  })
})
