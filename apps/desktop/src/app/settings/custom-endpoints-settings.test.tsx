import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getApiRequestConnection, setApiRequestConnection, setApiRequestProfile } from '@/api/client'
import { confirm } from '@/store/confirm'
import { $activeGatewayProfile, $newChatProfile, $newChatRoute } from '@/store/profile'
import { $connection, _resetComposerModelSelectionsForTests } from '@/store/session'
import type { CustomEndpoint, CustomEndpointsResponse } from '@/types/hermes'

import { deferred } from '../../test/deferred'

const getCustomEndpoints = vi.fn()
const saveCustomEndpoint = vi.fn()
const activateCustomEndpoint = vi.fn()
const validateCustomEndpoint = vi.fn()
const deleteCustomEndpoint = vi.fn()
const notifyError = vi.fn()

vi.mock('@/hermes', () => ({
  getCustomEndpoints: () => getCustomEndpoints(),
  saveCustomEndpoint: (body: unknown) => saveCustomEndpoint(body),
  activateCustomEndpoint: (id: string) => activateCustomEndpoint(id),
  validateCustomEndpoint: (body: unknown) => validateCustomEndpoint(body),
  deleteCustomEndpoint: (id: string) => deleteCustomEndpoint(id),
  setApiRequestProfile: vi.fn()
}))
vi.mock('@/store/notifications', () => ({ notify: vi.fn(), notifyError: (...args: unknown[]) => notifyError(...args) }))
vi.mock('@/store/confirm', () => ({ confirm: vi.fn(async () => true) }))
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
  getCustomEndpoints.mockReset()
  saveCustomEndpoint.mockReset()
  activateCustomEndpoint.mockReset()
  deleteCustomEndpoint.mockReset()
  vi.mocked(confirm).mockReset().mockResolvedValue(true)
  _resetComposerModelSelectionsForTests()
  $connection.set(null)
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
  it('clears source A form authority and key draft when the same profile rehomes to B and back', async () => {
    const second = { ...endpoint, name: 'Source B endpoint', model: 'model-b', models: ['model-b'], base_url: 'https://b.invalid/v1' }

    const rehome = (connectionId: string) => {
      setApiRequestConnection(connectionId)
      $connection.set({ connectionId } as never)
      $newChatRoute.set({ connectionId, profile: owner.profile, targetProfile: connectionId === 'source-a' ? 'backend-a' : 'backend-b' })
    }

    getCustomEndpoints.mockImplementation(async () => ({ endpoints: [getApiRequestConnection() === 'source-b' ? second : endpoint] }))
    saveCustomEndpoint.mockResolvedValue({ id: second.id, endpoints: [second] })
    await renderSettings()
    fireEvent.change(screen.getByPlaceholderText('Leave blank to keep current key'), { target: { value: 'fake-source-a-key' } })
    await act(async () => rehome('source-b'))
    await screen.findByDisplayValue(second.base_url)
    expect((screen.getByPlaceholderText('Leave blank to keep current key') as HTMLInputElement).value).toBe('')
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(saveCustomEndpoint).toHaveBeenCalledWith(expect.objectContaining({
      base_url: second.base_url, model: second.model, api_key: undefined
    })))
    await act(async () => rehome('source-a'))
    await screen.findByDisplayValue(endpoint.base_url)
  })

  it('rejects a delayed Delete confirmation from an unmounted owner even after A → B → A', async () => {
    const confirmation = deferred<boolean>()
    vi.mocked(confirm).mockReturnValueOnce(confirmation.promise)
    deleteCustomEndpoint.mockResolvedValue({ endpoints: [] })
    await renderSettings()
    fireEvent.click(screen.getByRole('button', { name: 'Delete endpoint' }))
    await waitFor(() => expect(confirm).toHaveBeenCalledOnce())

    const rehome = (connectionId: string) => {
      setApiRequestConnection(connectionId)
      $connection.set({ connectionId } as never)
      $newChatRoute.set({ connectionId, profile: owner.profile, targetProfile: connectionId === 'source-a' ? 'backend-a' : 'backend-b' })
    }

    await act(async () => rehome('source-b'))
    await screen.findByRole('button', { name: 'Save' })
    await act(async () => rehome('source-a'))
    await screen.findByRole('button', { name: 'Save' })
    await act(async () => confirmation.resolve(true))
    expect(deleteCustomEndpoint).not.toHaveBeenCalled()
  })

  it('rejects a stale Delete confirmation when React batches the source A → B → A round trip', async () => {
    const confirmation = deferred<boolean>()
    vi.mocked(confirm).mockReturnValueOnce(confirmation.promise)
    deleteCustomEndpoint.mockResolvedValue({ endpoints: [] })
    saveCustomEndpoint.mockResolvedValue(savedResponse())
    await renderSettings()
    fireEvent.click(screen.getByRole('button', { name: 'Delete endpoint' }))
    await waitFor(() => expect(confirm).toHaveBeenCalledOnce())
    await act(async () => {
      setApiRequestConnection('source-b')
      $connection.set({ connectionId: 'source-b' } as never)
      setApiRequestConnection(owner.connectionId)
      $connection.set({ connectionId: owner.connectionId } as never)
      confirmation.resolve(true)
    })
    expect(deleteCustomEndpoint).not.toHaveBeenCalled()
    expect(notifyError).toHaveBeenCalledWith(expect.objectContaining({ message: expect.stringMatching(/target changed/i) }), 'Delete failed')
    await waitFor(() => expect(getCustomEndpoints).toHaveBeenCalledTimes(2))
    await screen.findByDisplayValue(endpoint.base_url)
    fireEvent.click(await screen.findByRole('button', { name: 'Save' }))
    await waitFor(() => expect(saveCustomEndpoint).toHaveBeenCalledOnce())
  })

  it('discards a late source A inventory after the settings owner has rehomed to B', async () => {
    const pending = deferred<CustomEndpointsResponse>()
    const second = { ...endpoint, name: 'Source B endpoint', base_url: 'https://b.invalid/v1' }
    getCustomEndpoints.mockReturnValueOnce(pending.promise).mockResolvedValue({ endpoints: [second] })
    const { CustomEndpointsSettings } = await import('./custom-endpoints-settings')
    render(<CustomEndpointsSettings />)
    await waitFor(() => expect(getCustomEndpoints).toHaveBeenCalledOnce())
    await act(async () => {
      setApiRequestConnection('source-b')
      $connection.set({ connectionId: 'source-b' } as never)
      $newChatRoute.set({ connectionId: 'source-b', profile: owner.profile })
    })
    await act(async () => pending.resolve({ endpoints: [endpoint], current: savedResponse().current }))
    await screen.findByDisplayValue(second.base_url)
    expect(screen.queryByDisplayValue(endpoint.base_url)).toBeNull()
  })

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

it('serializes Use/Save writes, publishes A, and retains it when a subsequent B fails', async () => {
  const second = { ...endpoint, id: 'custom:second', name: 'Second endpoint', model: 'second-model' }
  getCustomEndpoints.mockResolvedValue({ endpoints: [endpoint, second] })
  const pending = deferred<{ provider: string; model: string }>()
  activateCustomEndpoint.mockReturnValueOnce(pending.promise).mockRejectedValueOnce(new Error('B failed'))
  const changed = await renderSettings()
  const buttons = screen.getAllByRole('button', { name: 'Use' })
  fireEvent.click(buttons[0])
  fireEvent.click(buttons[1])
  fireEvent.click(screen.getByRole('button', { name: 'Save' }))
  expect(activateCustomEndpoint).toHaveBeenCalledTimes(1)
  expect(saveCustomEndpoint).not.toHaveBeenCalled()
  expect((buttons[1] as HTMLButtonElement).disabled).toBe(true)
  pending.resolve({ provider: endpoint.id, model: endpoint.model })
  await waitFor(() => expect(changed).toHaveBeenCalledOnce())
  await waitFor(() => expect((buttons[1] as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(buttons[1])
  await waitFor(() => expect(notifyError).toHaveBeenCalled())
  expect(activateCustomEndpoint).toHaveBeenCalledTimes(2)
  expect(changed).toHaveBeenCalledOnce()
  expect(changed).toHaveBeenCalledWith(expect.objectContaining({ owner, provider: endpoint.id, model: endpoint.model }))
})

it('keeps Delete out of a pending activation and retains a confirmed model if list refresh fails', async () => {
  const pending = deferred<{ provider: string; model: string }>()
  activateCustomEndpoint.mockReturnValueOnce(pending.promise)
  const changed = await renderSettings()
  getCustomEndpoints.mockRejectedValueOnce(new Error('list read failed'))
  fireEvent.click(screen.getByRole('button', { name: 'Use' }))
  fireEvent.click(screen.getByRole('button', { name: 'Delete endpoint' }))
  expect(deleteCustomEndpoint).not.toHaveBeenCalled()
  pending.resolve({ provider: endpoint.id, model: endpoint.model })
  await waitFor(() => expect(notifyError).toHaveBeenCalled())
  expect(changed).toHaveBeenCalledOnce()
  expect(changed).toHaveBeenCalledWith(expect.objectContaining({ owner, provider: endpoint.id, model: endpoint.model }))
})

it('keeps Use and Save out of a pending Delete and releases the lock on failure', async () => {
  const pending = deferred<CustomEndpointsResponse>()
  deleteCustomEndpoint.mockReturnValueOnce(pending.promise)
  await renderSettings()
  fireEvent.click(screen.getByRole('button', { name: 'Delete endpoint' }))
  await waitFor(() => expect(deleteCustomEndpoint).toHaveBeenCalledOnce())
  fireEvent.click(screen.getByRole('button', { name: 'Use' }))
  fireEvent.click(screen.getByRole('button', { name: 'Save' }))
  expect(activateCustomEndpoint).not.toHaveBeenCalled()
  expect(saveCustomEndpoint).not.toHaveBeenCalled()
  pending.reject(new Error('delete failed'))
  await waitFor(() => expect(notifyError).toHaveBeenCalled())
  await waitFor(() => expect((screen.getByRole('button', { name: 'Use' }) as HTMLButtonElement).disabled).toBe(false))
})

it('rechecks the write lock after a delayed Delete confirmation', async () => {
  const confirmation = deferred<boolean>()
  vi.mocked(confirm).mockReturnValueOnce(confirmation.promise)
  const activation = deferred<{ provider: string; model: string }>()
  activateCustomEndpoint.mockReturnValueOnce(activation.promise)
  const changed = await renderSettings()
  fireEvent.click(screen.getByRole('button', { name: 'Delete endpoint' }))
  await waitFor(() => expect(confirm).toHaveBeenCalledOnce())
  fireEvent.click(screen.getByRole('button', { name: 'Use' }))
  confirmation.resolve(true)
  await waitFor(() => expect(activateCustomEndpoint).toHaveBeenCalledOnce())
  expect(deleteCustomEndpoint).not.toHaveBeenCalled()
  activation.resolve({ provider: endpoint.id, model: endpoint.model })
  await waitFor(() => expect(changed).toHaveBeenCalledOnce())
})
