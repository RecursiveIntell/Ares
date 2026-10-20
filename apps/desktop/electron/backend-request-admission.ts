/** Main-owned backend admission projection; no secrets or independent authority store. */
export interface NativeBackendAdmission {
  identity: string
  alive: () => boolean
}
interface OwnedChild {
  exitCode: number | null
  killed: boolean
  once: (event: 'exit', callback: () => void) => unknown
}

export function registerOwnedBackendAdmission(
  admissions: Map<string, NativeBackendAdmission>,
  baseUrl: string,
  child: OwnedChild,
  identity: string
): NativeBackendAdmission {
  const target = new URL(baseUrl)

  if (
    target.protocol !== 'http:' ||
    !['127.0.0.1', '[::1]'].includes(target.hostname) ||
    target.username ||
    target.password ||
    target.search ||
    target.hash ||
    target.pathname !== '/'
  ) {
    throw new Error('Owned local backend origin is invalid')
  }

  if (child.exitCode !== null || child.killed) {
    throw new Error('Owned backend exited before admission')
  }
  const entry = { identity, alive: () => child.exitCode === null && !child.killed }
  admissions.set(target.origin, entry)
  child.once('exit', () => {
    if (admissions.get(target.origin) === entry) {
      admissions.delete(target.origin)
    }
  })

  return entry
}

export function privilegedRequestHeaders(
  details,
  contents,
  dependencies: {
    admissions: Map<string, NativeBackendAdmission>
    trusted: (contents: any) => boolean
    remembered: (url: string) => boolean
    headers: (url: string) => Record<string, string>
  }
): { requestHeaders?: Record<string, string | string[]> } {
  try {
    if (!contents || !dependencies.trusted(contents) || !details.frame || details.frame !== contents.mainFrame) {
      return {}
    }

    const target = new URL(details.url)

    // Renderer WebSockets (including workers) never inherit native privileges.
    if (details.resourceType === 'webSocket') {
      return {}
    }
    const headers = dependencies.headers(details.url)

    return Object.keys(headers).length ? { requestHeaders: { ...details.requestHeaders, ...headers } } : {}
  } catch {
    // Destroyed/unknown frames, malformed targets and failed header resolution
    // receive no native origin rewriting or main-owned proxy credentials.
    return {}
  }
}
