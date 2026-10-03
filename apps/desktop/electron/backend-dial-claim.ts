/**
 * backend-dial-claim.ts
 *
 * Single-owner reconnect/dial claim for backend spawns, keyed by the pool
 * resolved pool scope (#90812). Registry-local can have a different backend
 * from the legacy primary even though backendScopeKey aliases their labels.
 *
 * Why this exists: reconnectGateway()'s in-flight lock lives at renderer
 * module scope, so it only dedupes reconnects INSIDE one window. Two windows
 * (main + a session pop-out) racing the same wake both invoke the main-process
 * dial IPC, and for a pooled SSH connection the loser of the pool-entry race
 * could bootstrap a duplicate remote backend. Electron main is the single
 * owner of backend lifecycles, so the claim belongs here: the first dial for a
 * (connectionId, profile) key runs; every concurrent caller for the same key
 * awaits and receives that first dial's result.
 *
 * Bounded by construction: a claim exists only while its dial promise is
 * unsettled — both outcomes release it, so a failed dial is never cached and
 * the next reconnect attempt runs fresh (fail closed, not latched).
 */
import {
  backendScopeKey,
  type ConnectionRegistry,
  type RegistryConnection,
  type RegistryLocalRoute,
  resolveRegistryLocalRoute
} from './connection-registry'

export type RegistryBackendDial = {
  registry: ConnectionRegistry
  source: RegistryConnection
  connectionId: string
  profile: string
  localRoute: RegistryLocalRoute | null
  delegatedProfile: string | null
}

type RegistryDialOptions = {
  globalRemote: boolean
  profileRemoteOverride: boolean
  primaryProfile: string
}

export function resolveRegistryDialOptions(
  profile: null | string | undefined,
  primaryProfile: string,
  globalRemote: boolean,
  profileHasRemoteOverride: (profile: string) => unknown
): RegistryDialOptions {
  const key = String(profile ?? '').trim() || primaryProfile

  return { globalRemote, profileRemoteOverride: Boolean(profileHasRemoteOverride(key)), primaryProfile }
}

// A registry factory can await primary matching before reaching the local
// branch. A formerly delegated route must fail if v1 has become remote;
// otherwise ensureBackend would silently send this local dial elsewhere.
export function assertDelegatedLocalDialCurrent(route: RegistryBackendDial, options: RegistryDialOptions): void {
  if (route.localRoute?.delegate && !resolveRegistryLocalRoute(route.profile, options).delegate) {
    throw new Error('Local backend dial was superseded by a remote route. Retry the connection.')
  }
}

export class BackendDialClaims {
  readonly #inflightByKey = new Map<string, Promise<unknown>>()

  // Resolve the actual local pool before admitting a claim. Carry the same
  // routing snapshot into the factory: an async wait must not re-resolve it
  // against a later registry/configuration and silently retarget the dial.
  runRegistry<T>(
    registry: ConnectionRegistry,
    connectionId: null | string | undefined,
    profile: null | string | undefined,
    options: RegistryDialOptions,
    dial: (route: RegistryBackendDial) => Promise<T> | T
  ): Promise<T> {
    const id = String(connectionId || '').trim() || registry.primary
    const source = registry.connections.find(connection => connection.id === id)

    if (!source) {
      return Promise.reject(new Error(`No connection with id "${id}".`))
    }

    const profileKey = String(profile ?? '').trim() || 'default'
    const localRoute = source.kind === 'local' ? resolveRegistryLocalRoute(profileKey, options) : null
    const delegatedProfile = localRoute?.delegate ? String(profile ?? '').trim() || options.primaryProfile : null
    const route = { registry, source, connectionId: id, profile: profileKey, localRoute, delegatedProfile }

    const key = localRoute
      ? localRoute.delegate
        ? backendScopeKey(null, delegatedProfile)
        : localRoute.poolKey
      : backendScopeKey(id, profile)

    return this.run(key, () => dial(route))
  }

  /** Whether a dial for this key is currently in flight (test/diagnostic seam). */
  inFlight(key: string): boolean {
    return this.#inflightByKey.has(key)
  }

  run<T>(key: string, dial: () => Promise<T> | T): Promise<T> {
    const existing = this.#inflightByKey.get(key) as Promise<T> | undefined

    if (existing) {
      return existing
    }

    // Start the dial eagerly so the first caller's spawn is already in flight
    // when a concurrent caller arrives; a synchronously-throwing dial is
    // converted into a rejection of THIS claim so it cannot bypass the seam.
    let pending: Promise<T>

    try {
      pending = Promise.resolve(dial())
    } catch (error) {
      pending = Promise.reject(error)
    }

    const release = () => {
      if (this.#inflightByKey.get(key) === pending) {
        this.#inflightByKey.delete(key)
      }
    }

    this.#inflightByKey.set(key, pending)
    // Release on both outcomes without creating an unhandled rejected branch.
    void pending.then(release, release)

    return pending
  }
}
