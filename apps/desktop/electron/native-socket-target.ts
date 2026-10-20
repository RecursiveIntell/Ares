import type { NativeSocketChannel } from './native-socket-contract'

/** Derives only fixed endpoints from a main-resolved canonical descriptor. */
export async function prepareNativeSocketTarget(
  connection: {
    baseUrl: string
    token?: string
    authMode?: string
    headers?: Record<string, string>
    profile?: string
    remoteProfile?: string
    sharedPrimary?: boolean
    sharedRemote?: boolean
  },
  channel: NativeSocketChannel,
  deps: {
    mint: (baseUrl: string, headers: Record<string, string>) => Promise<string>
    current: () => void
  }
) {
  deps.current()
  const base = new URL(connection.baseUrl)

  if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password || base.search || base.hash) {
    throw new Error('Invalid canonical backend')
  }

  const headers = { ...(connection.headers ?? {}) }

  // Always override any case variant of Origin in canonical configured headers.
  for (const key of Object.keys(headers)) {
    if (key.toLowerCase() === 'origin') {
      delete headers[key]
    }
  }

  headers.Origin = base.origin

  const target = new URL(
    base.toString().replace(/\/+$/, '') +
      (channel.kind === 'gateway'
        ? '/api/ws'
        : channel.kind === 'tts'
          ? '/api/audio/speak-stream'
          : `/api/plugins/${channel.pluginId}${channel.suffix}`)
  )

  target.protocol = base.protocol === 'https:' ? 'wss:' : 'ws:'

  if (connection.authMode === 'oauth') {
    // Existing plugin API has no verified ticket path: polling remains its fallback.
    if (channel.kind === 'plugin') {
      throw Object.assign(new Error('Plugin OAuth stream unavailable; use polling'), { code: 'PLUGIN_OAUTH_POLLING' })
    }

    const ticket = await deps.mint(connection.baseUrl, connection.headers ?? {})
    deps.current() // Fresh ticket may complete after source or document retirement.
    target.searchParams.set('ticket', ticket)
  } else {
    if (!connection.token) {
      throw new Error('Canonical backend has no transport credential')
    }

    target.searchParams.set('token', connection.token)
  }

  if (connection.sharedRemote || connection.sharedPrimary) {
    target.searchParams.set('profile', connection.profile || 'default')
  }

  if (channel.kind === 'tts' && !target.searchParams.has('profile')) {
    const profile = connection.remoteProfile ?? connection.profile

    if (profile) {
      target.searchParams.set('profile', profile)
    }
  }

  deps.current()

  return { url: target.toString(), headers }
}
