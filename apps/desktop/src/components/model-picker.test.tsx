import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeAll, expect, it, vi } from 'vitest'

import { getGlobalModelOptions } from '@/hermes'
import { modelOptionsQueryKey } from '@/lib/model-options'
import { ModelPickerDialog } from './model-picker'
import { ModelVisibilityDialog } from './model-visibility-dialog'

vi.mock('@/hermes', () => ({ getGlobalModelOptions: vi.fn(), setApiRequestProfile: vi.fn() }))
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

const payload = (model: string) => ({ providers: [{ models: [model], name: 'Target provider', slug: 'target' }] })

it('sends the modal target profile and explains a missing session selection without changing it', async () => {
  vi.mocked(getGlobalModelOptions).mockResolvedValue(payload('model-b'))
  const select = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(<QueryClientProvider client={client}>
    <ModelPickerDialog open onOpenChange={vi.fn()} onSelect={select} profile="specialist" connectionId="owner"
      sessionId="session" currentProvider="ollama-launch" currentModel="model-a" />
  </QueryClientProvider>)
  await view.findByText(/selected provider or model is unavailable in this profile/i)
  await view.findByText(/session keeps its own model selection/i)
  expect(getGlobalModelOptions).toHaveBeenCalledWith({ explicitOnly: true }, { connectionId: 'owner', profile: 'specialist' })
  expect(select).not.toHaveBeenCalled()
})

it('keeps a late catalog reply in its original source and profile cache', async () => {
  let resolveA!: (value: ReturnType<typeof payload>) => void
  vi.mocked(getGlobalModelOptions)
    .mockImplementationOnce(() => new Promise(resolve => { resolveA = resolve }))
    .mockResolvedValueOnce(payload('model-b'))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const picker = (connectionId: string, profile: string) => <QueryClientProvider client={client}>
    <ModelPickerDialog open onOpenChange={vi.fn()} onSelect={vi.fn()} profile={profile} connectionId={connectionId}
      currentProvider="target" currentModel="model-b" />
  </QueryClientProvider>
  const view = render(picker('source-a', 'target-a'))
  await vi.waitFor(() => expect(getGlobalModelOptions).toHaveBeenCalledTimes(1))
  view.rerender(picker('source-b', 'target-b'))
  await vi.waitFor(() => expect(client.getQueryData(modelOptionsQueryKey('target-b', null, 'source-b'))).toEqual(payload('model-b')))
  await act(async () => { resolveA(payload('model-a')) })
  await vi.waitFor(() => expect(client.getQueryData(modelOptionsQueryKey('target-a', null, 'source-a'))).toEqual(payload('model-a')))
  expect(client.getQueryData(modelOptionsQueryKey('target-b', null, 'source-b'))).toEqual(payload('model-b'))
})

it('visibility and picker subscribers share the same target catalog instead of a nonempty launch catalog', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const request = vi.fn(async (_method: string, params?: Record<string, unknown>) => payload(params?.profile === 'target' ? 'target-model' : 'source-model'))
  const gateway = { request } as never
  const view = render(<QueryClientProvider client={client}>
    <ModelVisibilityDialog gw={gateway} open profile="target" onOpenChange={vi.fn()} onOpenProviders={vi.fn()} />
  </QueryClientProvider>)
  await vi.waitFor(() => expect(client.getQueryData(modelOptionsQueryKey('target'))).toEqual(payload('target-model')))
  view.rerender(<QueryClientProvider client={client}>
    <ModelPickerDialog gw={gateway} open profile="target" onOpenChange={vi.fn()} onSelect={vi.fn()}
      currentProvider="target" currentModel="target-model" />
  </QueryClientProvider>)
  await vi.waitFor(() => expect(request).toHaveBeenCalledWith('model.options', { profile: 'target', explicit_only: true }))
  expect(client.getQueryData(modelOptionsQueryKey('target'))).toEqual(payload('target-model'))
})
