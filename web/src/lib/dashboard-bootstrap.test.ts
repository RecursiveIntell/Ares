import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { initializeDashboardBootstrap } from './dashboard-bootstrap'

const KEY = 'hermes.dashboard.session'
let values: Map<string, string>
type TestWindow = {
  location: { pathname: string; search: string; hash: string }
  history: { replaceState: ReturnType<typeof vi.fn> }
  sessionStorage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>
  __HERMES_BASE_PATH__: string
  __HERMES_AUTH_REQUIRED__: boolean
  __HERMES_SESSION_TOKEN__?: string
}
let win: TestWindow
beforeEach(() => {
  values = new Map()
  win = {
    location: { pathname: '/sessions/one', search: '?profile=test', hash: '#bootstrap=synthetic-grant' },
    history: {
      replaceState: vi.fn(() => {
        win.location.hash = ''
      })
    },
    sessionStorage: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key)
    },
    __HERMES_BASE_PATH__: '',
    __HERMES_AUTH_REQUIRED__: false
  }
  vi.stubGlobal('window', win)
})
afterEach(() => {
  vi.unstubAllGlobals()
})

describe('origin-isolated dashboard bootstrap', () => {
  it('scrubs grant before fetch and installs the bearer only after redemption', async () => {
    const fetcher = vi.fn(async (_url, options) => {
      expect(win.history.replaceState).toHaveBeenCalledWith(null, '', '/sessions/one?profile=test')
      expect(win.__HERMES_SESSION_TOKEN__).toBeUndefined()
      expect(options.credentials).toBe('omit')
      expect(options.redirect).toBe('error')
      return {
        ok: true,
        status: 200,
        json: async () => ({ protocol: 'browser-launch-v1', token: 'synthetic-api-bearer' })
      }
    })
    vi.stubGlobal('fetch', fetcher)
    await initializeDashboardBootstrap()
    expect(win.__HERMES_SESSION_TOKEN__).toBe('synthetic-api-bearer')
    expect(values.get(KEY)).toBe('synthetic-api-bearer')
    expect(fetcher).toHaveBeenCalledTimes(1)
  })
  it('revalidates tab-scoped storage after reload without acquiring an ambient cookie', async () => {
    win.location.hash = ''
    values.set(KEY, 'known-bearer')
    const fetcher = vi.fn(async (_url, options) => {
      expect(options.headers['X-Hermes-Session-Token']).toBe('known-bearer')
      expect(options.body).toBe('{}')
      return { ok: true, status: 204 }
    })
    vi.stubGlobal('fetch', fetcher)
    await initializeDashboardBootstrap()
    expect(win.__HERMES_SESSION_TOKEN__).toBe('known-bearer')
  })
  it('clears stale state on rotation/denial, with no automatic retry loop', async () => {
    win.location.hash = ''
    values.set(KEY, 'stale-bearer')
    const fetcher = vi.fn(async () => ({ ok: false, status: 403 }))
    vi.stubGlobal('fetch', fetcher)
    await expect(initializeDashboardBootstrap()).rejects.toThrow(/authenticated launch/)
    expect(values.has(KEY)).toBe(false)
    expect(win.__HERMES_SESSION_TOKEN__).toBeUndefined()
    expect(fetcher).toHaveBeenCalledTimes(1)
  })
  it('does not run legacy bootstrap or retain its token in gated login mode', async () => {
    win.__HERMES_AUTH_REQUIRED__ = true
    values.set(KEY, 'old-loopback-bearer')
    const fetcher = vi.fn()
    vi.stubGlobal('fetch', fetcher)
    await initializeDashboardBootstrap()
    expect(fetcher).not.toHaveBeenCalled()
    expect(values.has(KEY)).toBe(false)
  })
  it('uses the declared same-origin base path', async () => {
    win.__HERMES_BASE_PATH__ = '/hermes'
    const fetcher = vi.fn(async (url: string) => {
      expect(url).toBe('/hermes/auth/browser-bootstrap')
      return { ok: true, status: 200, json: async () => ({ protocol: 'browser-launch-v1', token: 'bearer' }) }
    })
    vi.stubGlobal('fetch', fetcher)
    await initializeDashboardBootstrap()
    expect(fetcher.mock.calls[0][0]).toBe('/hermes/auth/browser-bootstrap')
  })
  it('fails closed on a missing grant/token before starting application code', async () => {
    win.location.hash = ''
    vi.stubGlobal('fetch', vi.fn())
    await expect(initializeDashboardBootstrap()).rejects.toThrow(/authenticated launch/)
    expect(fetch).not.toHaveBeenCalled()
  })
})
