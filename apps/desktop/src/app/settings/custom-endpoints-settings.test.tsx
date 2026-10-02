import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from '@/api/client'
import { $activeGatewayProfile, $newChatProfile, $newChatRoute } from '@/store/profile'
import { _resetComposerModelSelectionsForTests } from '@/store/session'
import type { CustomEndpoint, CustomEndpointsResponse } from '@/types/hermes'

import { deferred } from '../../test/deferred'

const getCustomEndpoints = vi.fn()
const saveCustomEndpoint = vi.fn()
const activateCustomEndpoint = vi.fn()
const validateCustomEndpoint = vi.fn()
const notifyError = vi.fn()

vi.mock('@/hermes', () => ({
  getCustomEndpoints: () => getCustomEndpoints(),
  saveCustomEndpoint: (body: unknown) => saveCustomEndpoint(body),
  activateCustomEndpoint: (id: string) => activateCustomEndpoint(id),
  validateCustomEndpoint: (body: unknown) => validateCustomEndpoint(body),
  deleteCustomEndpoint: vi.fn(),
  setApiRequestProfile: vi.fn()
}))
vi.mock('@/store/notifications', () => ({ notify: vi.fn(), notifyError: (...args: unknown[]) => notifyError(...args) }))
vi.mock('@/lib/haptics', () => ({ triggerHaptic: vi.fn() }))

const owner = { connectionId: 'source-a', profile: 'specialist', targetProfile: 'backend-a' }

const endpoint: CustomEndpoint = {
  id: 'custom:fixture',
  name: 'Fixture endpoint',
  base_url: 'https://fixture.invalid/v1',
  model: 'fixture-model',
  models: ['fixture-model'],
  discover_models: false,
  has_api_key: false,
  is_current: false
}

function savedResponse(): CustomEndpointsResponse {
  return {
    id: endpoint.id,
    current: { base_url: endpoint.base_url, model: endpoint.model, provider: endpoint.id },
    endpoints: [{ ...endpoint, is_current: true }]
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  _resetComposerModelSelectionsForTests()
  $newChatRoute.set(owner)
  $newChatProfile.set(owner.profile)
  $activeGatewayProfile.set(owner.profile)
  setApiRequestConnection(owner.connectionId)
  setApiRequestProfile(owner.profile)
  getCustomEndpoints.mockResolvedValue({ endpoints: [endpoint] })
})
afterEach(() => {
  cleanup()
  setApiRequestConnection(null)
  setApiRequestProfile('default')
  $newChatRoute.set(null)
  $newChatProfile.set(null)
  $activeGatewayProfile.set('default')
})

async function renderSettings(changed = vi.fn()) {
  const { CustomEndpointsSettings } = await import('./custom-endpoints-settings')
  render(<CustomEndpointsSettings onMainModelChanged={changed} />)
  await screen.findByRole('button', { name: 'Save' })

  return changed
}

describe('custom endpoint save ownership', () => {
  it('carries the original owner through a delayed save callback', async () => {
    const pending = deferred<CustomEndpointsResponse>()
    saveCustomEndpoint.mockReturnValueOnce(pending.promise)
    const changed = await renderSettings()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(saveCustomEndpoint).toHaveBeenCalledOnce())
    setApiRequestConnection('source-b')
    pending.resolve(savedResponse())
    await waitFor(() => expect(changed).toHaveBeenCalledOnce())
    expect(changed).toHaveBeenCalledWith(
      expect.objectContaining({
        owner,
        model: endpoint.model,
        provider: endpoint.id,
        saveIntent: expect.objectContaining({ target: JSON.stringify(['source-a', 'specialist', 'backend-a']) })
      })
    )
    expect(validateCustomEndpoint).not.toHaveBeenCalled()
  })

  it('carries the original owner through activation and its delayed refresh', async () => {
    const pending = deferred<{ provider: string; model: string }>()
    const refresh = deferred<CustomEndpointsResponse>()
    activateCustomEndpoint.mockReturnValueOnce(pending.promise)
    const changed = await renderSettings()
    getCustomEndpoints.mockReturnValueOnce(refresh.promise)
    fireEvent.click(screen.getByRole('button', { name: 'Use' }))
    await waitFor(() => expect(activateCustomEndpoint).toHaveBeenCalledWith(endpoint.id))
    pending.resolve({ provider: endpoint.id, model: endpoint.model })
    await waitFor(() => expect(getCustomEndpoints).toHaveBeenCalledTimes(2))
    setApiRequestConnection('source-b')
    refresh.resolve(savedResponse())
    await waitFor(() => expect(changed).toHaveBeenCalledOnce())
    expect(changed).toHaveBeenCalledWith(
      expect.objectContaining({ owner, model: endpoint.model, provider: endpoint.id })
    )
    expect(validateCustomEndpoint).not.toHaveBeenCalled()
  })

  it('publishes no model callback after a failed save', async () => {
    saveCustomEndpoint.mockRejectedValueOnce(new Error('fixture-save-failed'))
    const changed = await renderSettings()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(notifyError).toHaveBeenCalled())
    expect(changed).not.toHaveBeenCalled()
    expect(validateCustomEndpoint).not.toHaveBeenCalled()
  })
})
