import type { NativeSocketRoute } from './native-socket-contract'

export interface NativeBackendLifetimeCandidate {
  promise: Promise<any> | null
  current: () => Promise<any> | null
}

// Main-only, ephemeral cache-owner evidence. Enumerable symbol keys survive
// main's descriptor spreads but are omitted by IPC/JSON structured cloning.
// This is not a second registry: current() reads the canonical cache owner.
const lifetimeBinding = Symbol('nativeBackendLifetime')

export function bindNativeBackendLifetime(connection: any, candidate: NativeBackendLifetimeCandidate) {
  // A revalidation may reconnect through another canonical producer. Preserve
  // that producer's binding instead of attaching the retired outer cache.
  if (connection[lifetimeBinding]) {
    return connection
  }

  if (!candidate.promise) {
    throw new Error('Backend lifetime has no owning promise')
  }

  return { ...connection, [lifetimeBinding]: candidate }
}

export async function bindNativeBackendPromise(promise: Promise<any>, current: () => Promise<any> | null) {
  const candidate = { promise, current }

  return bindNativeBackendLifetime(await promise, candidate)
}

/** Claims remain the existing coalescing owners; never infer owner from values. */
export async function claimNativeBackendLifetime(
  route: NativeSocketRoute,
  deps: {
    primary: (profile: string | null) => Promise<any>
    registry: (connectionId: string, profile: string) => Promise<any>
    currentDocument: () => void
    source: () => string
  }
) {
  deps.currentDocument()
  const source = deps.source()

  const connection = await (route.kind === 'registry'
    ? deps.registry(route.connectionId, route.profile)
    : deps.primary(route.kind === 'profile' ? route.profile : null))

  deps.currentDocument()

  if (deps.source() !== source) {
    throw new Error('Backend source changed during claim')
  }
  const candidate: NativeBackendLifetimeCandidate | undefined = connection[lifetimeBinding]

  if (!candidate?.promise || candidate.current() !== candidate.promise) {
    throw new Error('Resolved backend lifetime unavailable')
  }

  const promise = candidate.promise

  return {
    connection,
    sourceCurrent: () => deps.source() === source,
    lifetimeCurrent: () => candidate.current() === promise
  }
}
