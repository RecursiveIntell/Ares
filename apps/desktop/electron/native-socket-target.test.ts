import { expect, test, vi } from 'vitest'

import { prepareNativeSocketTarget } from './native-socket-target'

const connection = {
  baseUrl: 'https://inert.invalid/prefix',
  authMode: 'oauth',
  headers: { 'X-Synthetic-Proxy': 'fixture' },
  profile: 'research',
  sharedRemote: true
}

test('every dial mints fresh OAuth ticket against canonical endpoint with its own headers', async () => {
  const mint = vi.fn().mockResolvedValueOnce('first').mockResolvedValueOnce('second')
  const current = vi.fn()
  const first = await prepareNativeSocketTarget(connection, { kind: 'gateway' }, { mint, current })
  const second = await prepareNativeSocketTarget(connection, { kind: 'tts' }, { mint, current })
  expect(first.url).toBe('wss://inert.invalid/prefix/api/ws?ticket=first&profile=research')
  expect(second.url).toBe('wss://inert.invalid/prefix/api/audio/speak-stream?ticket=second&profile=research')
  expect(first.headers).toEqual({ 'X-Synthetic-Proxy': 'fixture', Origin: 'https://inert.invalid' })
  expect(mint).toHaveBeenCalledTimes(2)
})
test('late ticket revalidation denies retired generation', async () => {
  const current = vi
    .fn()
    .mockImplementationOnce(() => {})
    .mockImplementation(() => {
      throw new Error('retired')
    })

  await expect(
    prepareNativeSocketTarget(connection, { kind: 'gateway' }, { mint: async () => 'synthetic', current })
  ).rejects.toThrow('retired')
})
test('plugin OAuth retains polling; no invented ticket path', async () => {
  const mint = vi.fn()
  await expect(
    prepareNativeSocketTarget(
      connection,
      { kind: 'plugin', pluginId: 'kanban', suffix: '/events' },
      { mint, current: () => {} }
    )
  ).rejects.toMatchObject({ code: 'PLUGIN_OAUTH_POLLING' })
  expect(mint).not.toHaveBeenCalled()
})
test('SSH TTS uses backend profile alias and canonical token; configured Origin cannot override it', async () => {
  const result = await prepareNativeSocketTarget(
    {
      baseUrl: 'http://127.0.0.1:9119',
      token: 'synthetic',
      profile: 'desktop-alias',
      remoteProfile: 'backend-profile',
      headers: { origin: 'https://hostile.invalid' }
    },
    { kind: 'tts' },
    { mint: vi.fn(), current: () => {} }
  )

  expect(result.url).toBe('ws://127.0.0.1:9119/api/audio/speak-stream?token=synthetic&profile=backend-profile')
  expect(result.headers).toEqual({ Origin: 'http://127.0.0.1:9119' })
})
