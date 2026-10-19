// Loopback credentials stay in this origin's tab, never in ambient cookies.
// Same-origin script/XSS trust still applies; this is not a secure secret vault.
const TOKEN_KEY = 'hermes.dashboard.session'
const REQUIRED = 'Open a new authenticated launch URL from hermes dashboard.'

function clearToken(): void {
  delete window.__HERMES_SESSION_TOKEN__
  try {
    window.sessionStorage.removeItem(TOKEN_KEY)
  } catch {
    /* storage unavailable */
  }
}

export async function initializeDashboardBootstrap(): Promise<void> {
  const fragment = new URLSearchParams(window.location.hash.slice(1))
  const ticket = fragment.get('bootstrap')
  if (ticket !== null) {
    fragment.delete('bootstrap')
    const remainder = fragment.toString()
    window.history.replaceState(
      null,
      '',
      window.location.pathname + window.location.search + (remainder ? '#' + remainder : '')
    )
  }
  if (window.__HERMES_AUTH_REQUIRED__) {
    clearToken()
    return
  }
  const base = window.__HERMES_BASE_PATH__ ?? ''
  if (base && (!base.startsWith('/') || base.startsWith('//') || /[\\?#]/.test(base))) {
    clearToken()
    throw new Error(REQUIRED)
  }
  let saved: string | null = null
  try {
    saved = window.sessionStorage.getItem(TOKEN_KEY)
  } catch {
    /* memory-only launch */
  }
  const token = ticket === null ? saved : null
  if (!ticket && !token) {
    clearToken()
    throw new Error(REQUIRED)
  }
  try {
    const response = await fetch(base.replace(/\/$/, '') + '/auth/browser-bootstrap', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(token ? { 'X-Hermes-Session-Token': token } : {}) },
      body: JSON.stringify(ticket !== null ? { ticket } : {}),
      credentials: 'omit',
      cache: 'no-store',
      redirect: 'error',
      referrerPolicy: 'no-referrer',
      signal: AbortSignal.timeout(5000)
    })
    if (!response.ok) throw new Error(REQUIRED)
    let accepted = token
    if (ticket !== null) {
      const result = await response.json()
      if (
        response.status !== 200 ||
        result.protocol !== 'browser-launch-v1' ||
        typeof result.token !== 'string' ||
        !/^[\x21-\x7e]{1,256}$/.test(result.token)
      ) {
        throw new Error(REQUIRED)
      }
      accepted = result.token
    } else if (response.status !== 204) {
      throw new Error(REQUIRED)
    }
    if (!accepted) throw new Error(REQUIRED)
    window.__HERMES_SESSION_TOKEN__ = accepted
    try {
      window.sessionStorage.setItem(TOKEN_KEY, accepted)
    } catch {
      /* usable in this document only */
    }
  } catch {
    clearToken()
    throw new Error(REQUIRED)
  }
}
