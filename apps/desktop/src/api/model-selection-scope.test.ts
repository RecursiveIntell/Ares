import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from './client'
import { getGlobalModelOptions } from './models'

const originalBridge = window.hermesDesktop
const api = vi.fn(async () => ({ providers: [] }))

beforeEach(() => {
  window.hermesDesktop = { api } as never
  setApiRequestConnection('ambient-remote')
  setApiRequestProfile('ambient-profile')
  api.mockClear()
})

afterEach(() => {
  window.hermesDesktop = originalBridge
  setApiRequestConnection(null)
  setApiRequestProfile(null)
})

it.each(['local', 'owner-remote'])(
  'passes an explicit connection and target profile to the preload bridge (%s)',
  async connectionId => {
    await getGlobalModelOptions(undefined, { connectionId, profile: 'target-specialist' })
    expect(api).toHaveBeenCalledWith(expect.objectContaining({ connectionId, profile: 'target-specialist' }))
  }
)

it('preserves ambient routing for legacy unowned catalog callers', async () => {
  await getGlobalModelOptions()
  expect(api).toHaveBeenCalledWith(
    expect.objectContaining({ connectionId: 'ambient-remote', profile: 'ambient-profile' })
  )
})

it.each([null, 'local', 'owner-remote'])(
  'reads an exact model-info owner without borrowing ambient routing (%s)',
  async connectionId => {
    const { getGlobalModelInfo } = await import('./models')
    await getGlobalModelInfo({ connectionId, profile: 'backend-target' })
    expect(api).toHaveBeenCalledWith(
      expect.objectContaining({
        connectionId: connectionId || 'local',
        profile: 'backend-target',
        path: '/api/model/info'
      })
    )
  }
)

it('preserves legacy ambient model-info routing', async () => {
  const { getGlobalModelInfo } = await import('./models')
  await getGlobalModelInfo()
  expect(api).toHaveBeenCalledWith(
    expect.objectContaining({ connectionId: 'ambient-remote', profile: 'ambient-profile', path: '/api/model/info' })
  )
})
