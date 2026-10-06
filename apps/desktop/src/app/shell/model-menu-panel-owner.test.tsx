import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from '@/api/client'
import type * as Gateway from '@/store/gateway'
import { $activeGatewayProfile, $newChatConnectionId, $newChatProfile, $newChatRoute } from '@/store/profile'
import {
  $activeSessionId, $currentModel, $currentProvider, $sessions, _resetComposerModelSelectionsForTests,
  _resetSessionOwnerHintsForTests, captureComposerModelSelection, recordComposerModelSelection,
  setComposerModelSelectionOwner, setSessionOwnerHint
} from '@/store/session'
import { knownOwnerForSession } from '@/store/session-states'
import type { ModelOptionsResponse } from '@/types/hermes'

import { deferred } from '../../test/deferred'

import { ModelMenuPanel } from './model-menu-panel'

const calls = vi.hoisted(() => ({ agent: vi.fn(), rest: vi.fn() }))
vi.mock('@/store/gateway', async original => ({
  ...await original<typeof Gateway>(),
  requestGatewayForAgent: (...args: unknown[]) => calls.agent(...args)
}))
vi.mock('@/hermes', () => ({ getGlobalModelOptions: (...args: unknown[]) => calls.rest(...args), setApiRequestProfile: vi.fn() }))
// Inspect the real producer's current pair without involving dropdown portals.
vi.mock('./model-catalog-menu', () => ({
  ModelMenuCloseContext: {},
  ModelCatalogMenu: ({ controller }: { controller: { current: { model: string; provider: string } } }) =>
    <output data-testid="current">{controller.current.model}|{controller.current.provider}</output>
}))

const ownerB = { connectionId: 'source-b', profile: 'same-name', targetProfile: 'backend-b' }
const options = (model: string): ModelOptionsResponse => ({ model, provider: 'custom:b', providers: [{ name: 'B', slug: 'custom:b', models: [model] }] })

beforeEach(() => {
  vi.clearAllMocks()
  calls.agent.mockReset().mockResolvedValue(options('catalog-b'))
  calls.rest.mockReset().mockResolvedValue(options('rest-b'))
  _resetComposerModelSelectionsForTests()
  _resetSessionOwnerHintsForTests()
  $sessions.set([])
  $activeSessionId.set(null)
  $currentModel.set('ambient-a')
  $currentProvider.set('provider-a')
  setApiRequestConnection('source-a')
  setApiRequestProfile('default')
  $activeGatewayProfile.set('default')
  $newChatRoute.set(ownerB)
  $newChatProfile.set(ownerB.profile)
  $newChatConnectionId.set(ownerB.connectionId)
  setComposerModelSelectionOwner(ownerB)
})
afterEach(() => { cleanup(); setApiRequestConnection(null); setApiRequestProfile('default'); $newChatRoute.set(null); $newChatProfile.set(null); $newChatConnectionId.set(null); $sessions.set([]); _resetSessionOwnerHintsForTests() })

function mount(rpcUnavailable?: 'empty' | 'rejected') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const ambient = vi.fn(async () => options('ambient-catalog-a'))

  if (rpcUnavailable === 'empty') {
    ambient.mockResolvedValue({ providers: [] })
  } else if (rpcUnavailable === 'rejected') {
    ambient.mockRejectedValue(new Error('Offline catalog RPC'))
  }

  const view = render(<QueryClientProvider client={client}><ModelMenuPanel gateway={{ request: ambient } as never} onSelectModel={vi.fn()} profile="default" requestGateway={ambient as never} /></QueryClientProvider>)

  return { client, ambient, view }
}

it('routes a fresh pending/failed B draft catalog to B while the foreground gateway stays on A', async () => {
  mount()
  await waitFor(() => expect(calls.agent).toHaveBeenCalledWith('source-b', 'same-name', 'model.options', { profile: 'backend-b', explicit_only: true }))
  await waitFor(() => expect(screen.getByTestId('current').textContent).toBe('catalog-b|custom:b'))
})

it('retains a valid scalar draft receipt beside a stale complete catalog instead of borrowing ambient A', async () => {
  recordComposerModelSelection(captureComposerModelSelection(ownerB), { model: 'scalar-b', provider: '', source: 'default' })
  mount()
  await waitFor(() => expect(screen.getByTestId('current').textContent).toBe('scalar-b|'))
})

it('keeps late B catalog replies in B cache after a B → A draft rehome', async () => {
  const pending = deferred<ModelOptionsResponse>()
  calls.agent.mockReturnValueOnce(pending.promise).mockResolvedValue(options('catalog-a'))
  const { client } = mount()
  await waitFor(() => expect(calls.agent).toHaveBeenCalledOnce())
  await act(async () => {
    const ownerA = { connectionId: 'source-a', profile: 'same-name', targetProfile: 'backend-a' }
    $newChatRoute.set(ownerA)
    $newChatConnectionId.set(ownerA.connectionId)
    setComposerModelSelectionOwner(ownerA)
  })
  await waitFor(() => expect(screen.getByTestId('current').textContent).toBe('catalog-a|custom:b'))
  await act(async () => pending.resolve(options('late-b')))
  expect(screen.getByTestId('current').textContent).toBe('catalog-a|custom:b')
  expect(client.getQueryData(['model-options', 'source-b::backend-b', 'global'])).toEqual(options('late-b'))
})

it('recovers a failed B RPC through the same captured B REST target', async () => {
  calls.agent.mockRejectedValueOnce(new Error('B route unavailable'))
  mount()
  await waitFor(() => expect(calls.rest).toHaveBeenCalledWith({ explicitOnly: true }, { connectionId: 'source-b', profile: 'backend-b' }))
  await waitFor(() => expect(screen.getByTestId('current').textContent).toBe('rest-b|custom:b'))
})

it.each([
  ['legacy', 'rejected'], ['legacy', 'empty'], ['local', 'rejected'], ['local', 'empty']
] as const)('preserves a live %s owner through %s RPC recovery', async (source, rpcUnavailable) => {
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
  calls.rest.mockImplementation(async (_opts, scope) => options(scope.connectionId === 'local' ? 'local-b' : 'legacy-a'))
  const { client, ambient } = mount(rpcUnavailable)
  await waitFor(() => expect(calls.rest).toHaveBeenCalledWith({ explicitOnly: true }, { connectionId, profile: 'default' }))
  const model = source === 'legacy' ? 'legacy-a' : 'local-b'
  await waitFor(() => expect(screen.getByTestId('current').textContent).toBe(`${model}|custom:b`))
  expect(ambient).toHaveBeenCalledWith('model.options', { profile: 'default', session_id: runtimeId, explicit_only: true })
  expect(client.getQueryData(['model-options', source === 'legacy' ? 'default' : 'local::default', runtimeId])).toEqual(options(model))
})
