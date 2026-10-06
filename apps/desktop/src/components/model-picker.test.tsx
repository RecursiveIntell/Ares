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

it('retains a confirmed scalar selection instead of marking a stale catalog model current', async () => {
  vi.mocked(getGlobalModelOptions).mockResolvedValue({ ...payload('catalog-model'), model: 'catalog-model', provider: 'target' })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  const view = render(<QueryClientProvider client={client}>
    <ModelPickerDialog currentModel="scalar-model" currentProvider="" onOpenChange={vi.fn()} onSelect={vi.fn()} open selectionIsAuthoritative />
  </QueryClientProvider>)

  const row = await view.findByText('catalog-model')
  expect(row.closest('[cmdk-item]')?.className).not.toContain('bg-primary text-primary-foreground')
})

it('sends the modal target profile and explains a missing session selection without changing it', async () => {
  vi.mocked(getGlobalModelOptions).mockResolvedValue(payload('model-b'))
  const select = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  const view = render(<QueryClientProvider client={client}>
    <ModelPickerDialog connectionId="owner" currentModel="model-a" currentProvider="ollama-launch" onOpenChange={vi.fn()} onSelect={select}
      open profile="specialist" sessionId="session" />
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
    <ModelPickerDialog connectionId={connectionId} currentModel="model-b" currentProvider="target" onOpenChange={vi.fn()} onSelect={vi.fn()}
      open profile={profile} />
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
    <ModelVisibilityDialog gw={gateway} onOpenChange={vi.fn()} onOpenProviders={vi.fn()} open profile="target" />
  </QueryClientProvider>)

  await vi.waitFor(() => expect(client.getQueryData(modelOptionsQueryKey('target'))).toEqual(payload('target-model')))
  view.rerender(<QueryClientProvider client={client}>
    <ModelPickerDialog currentModel="target-model" currentProvider="target" gw={gateway} onOpenChange={vi.fn()} onSelect={vi.fn()}
      open profile="target" />
  </QueryClientProvider>)
  await vi.waitFor(() => expect(request).toHaveBeenCalledWith('model.options', { profile: 'target', explicit_only: true }))
  expect(client.getQueryData(modelOptionsQueryKey('target'))).toEqual(payload('target-model'))
})
