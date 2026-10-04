import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from '@/api/client'
import type * as Gateway from '@/store/gateway'
import { $newChatConnectionId, $newChatProfile, $newChatRoute } from '@/store/profile'
import {
  $activeSessionId, $currentModel, $currentProvider, $gatewayState, $modelPickerOpen, $sessions,
  _resetComposerModelSelectionsForTests, _resetSessionOwnerHintsForTests, captureComposerModelSelection,
  recordComposerModelSelection, setComposerModelSelectionOwner, setSessionOwnerHint
} from '@/store/session'
import { knownOwnerForSession } from '@/store/session-states'

import { ModelPickerOverlay } from './model-picker-overlay'

const calls = vi.hoisted(() => ({ agent: vi.fn(), profile: vi.fn(), rest: vi.fn() }))
vi.mock('@/store/gateway', async original => ({
  ...await original<typeof Gateway>(),
  requestGatewayForAgent: (...args: unknown[]) => calls.agent(...args),
  requestGatewayForProfile: (...args: unknown[]) => calls.profile(...args)
}))
vi.mock('@/hermes', () => ({ getGlobalModelOptions: (...args: unknown[]) => calls.rest(...args), setApiRequestProfile: vi.fn() }))
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
})
const owner = { connectionId: 'source-b', profile: 'same-name', targetProfile: 'backend-b' }
const options = { model: 'catalog-b', provider: 'custom:b', providers: [{ name: 'B', slug: 'custom:b', models: ['catalog-b', 'pinned-b'] }] }

beforeEach(() => {
  vi.clearAllMocks()
  calls.agent.mockReset().mockResolvedValue(options)
  calls.profile.mockReset().mockResolvedValue(options)
  calls.rest.mockReset().mockResolvedValue(options)
  _resetComposerModelSelectionsForTests()
  _resetSessionOwnerHintsForTests()
  $sessions.set([])
  $activeSessionId.set(null)
  $gatewayState.set('open')
  $modelPickerOpen.set(true)
  $currentModel.set('ambient-a')
  $currentProvider.set('provider-a')
  setApiRequestConnection('source-a')
  setApiRequestProfile('default')
  $newChatRoute.set(owner)
  $newChatProfile.set(owner.profile)
  $newChatConnectionId.set(owner.connectionId)
  setComposerModelSelectionOwner(owner)
})
afterEach(() => { cleanup(); setApiRequestConnection(null); setApiRequestProfile('default'); $newChatRoute.set(null); $newChatProfile.set(null); $newChatConnectionId.set(null); $modelPickerOpen.set(false); $gatewayState.set('idle'); $sessions.set([]); _resetSessionOwnerHintsForTests() })

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const ambient = vi.fn(async () => options)
  const select = vi.fn()
  const view = render(<QueryClientProvider client={client}><ModelPickerOverlay gateway={{ request: ambient } as never} onSelect={select} profile="default" /></QueryClientProvider>)

  return { client, view, ambient, select }
}

it('uses the captured fresh draft source and backend target in the actual overlay catalog', async () => {
  const { ambient } = mount()
  await screen.findByText('catalog-b')
  expect(calls.agent).toHaveBeenCalledWith('source-b', 'same-name', 'model.options', { profile: 'backend-b', explicit_only: true })
  expect(ambient).not.toHaveBeenCalled()
})

it('does not mark a stale catalog default current when a valid scalar receipt owns the draft', async () => {
  recordComposerModelSelection(captureComposerModelSelection(owner), { model: 'scalar-b', provider: '', source: 'default' })
  $currentModel.set('scalar-b')
  $currentProvider.set('')
  const { select } = mount()
  const row = await screen.findByText('catalog-b')
  expect(row.closest('[cmdk-item]')?.className).not.toContain('bg-primary text-primary-foreground')
  expect(select).not.toHaveBeenCalled()
})

it('preserves a deliberate draft pin through a late catalog reply', async () => {
  let resolve!: (value: typeof options) => void
  calls.agent.mockReturnValueOnce(new Promise(r => { resolve = r }))
  mount()
  await waitFor(() => expect(calls.agent).toHaveBeenCalledOnce())
  await act(async () => {
    recordComposerModelSelection(captureComposerModelSelection(owner), { model: 'pinned-b', provider: 'custom:b', source: 'manual' })
    $currentModel.set('pinned-b')
    $currentProvider.set('custom:b')
    resolve(options)
  })
  const row = await screen.findByText('pinned-b')
  expect(row.closest('[cmdk-item]')?.className).toContain('bg-primary text-primary-foreground')
})

it.each([
  ['legacy', 'rejected'], ['legacy', 'empty'], ['local', 'rejected'], ['local', 'empty']
] as const)('preserves a live %s owner through %s RPC recovery in the overlay', async (source, rpcUnavailable) => {
  const runtimeId = 'owned-runtime'
  const connectionId = source === 'legacy' ? null : 'local'
  $sessions.set([{ id: runtimeId, profile: 'default' }] as never)

  if (connectionId) {
    setSessionOwnerHint(runtimeId, { connectionId, profile: 'default', mode: 'local' })
  }

  expect(knownOwnerForSession(runtimeId)).toEqual(connectionId ? { connectionId, profile: 'default', mode: 'local' } : 'default')
  $activeSessionId.set(runtimeId)
  $currentModel.set('')
  $currentProvider.set('')
  const rpc = source === 'legacy' ? calls.profile : calls.agent

  if (rpcUnavailable === 'empty') {
    rpc.mockResolvedValue({ providers: [] })
  } else {
    rpc.mockRejectedValue(new Error('Offline catalog RPC'))
  }

  const model = source === 'legacy' ? 'legacy-a' : 'local-b'
  const expected = { ...options, model, providers: [{ ...options.providers[0], models: [model] }] }
  calls.rest.mockImplementation(async (_opts, scope) => scope.connectionId === connectionId ? expected : options)
  const { client, ambient } = mount()
  await waitFor(() => expect(calls.rest).toHaveBeenCalledWith({ explicitOnly: true }, { connectionId, profile: 'default' }))
  await screen.findByText(model)
  expect(ambient).not.toHaveBeenCalled()

  if (source === 'legacy') {
    expect(calls.profile).toHaveBeenCalledWith('default', 'model.options', { profile: 'default', session_id: runtimeId, explicit_only: true }, undefined, undefined)
  } else {
    expect(calls.agent).toHaveBeenCalledWith('local', 'default', 'model.options', { profile: 'default', session_id: runtimeId, explicit_only: true })
  }

  expect(client.getQueryData(['model-options', source === 'legacy' ? 'default' : 'local::default', runtimeId])).toEqual(expected)
})
