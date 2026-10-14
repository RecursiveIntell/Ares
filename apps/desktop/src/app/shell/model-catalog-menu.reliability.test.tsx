import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { ModelVisibilityDialog } from '@/components/model-visibility-dialog'
import { DropdownMenu, DropdownMenuContent } from '@/components/ui/dropdown-menu'
import { $modelVisibilityOpen, setModelVisibilityOpen, setVisibleModels } from '@/store/model-visibility'
import type { ModelOptionsResponse } from '@/types/hermes'

import { ModelCatalogMenu, type ModelMenuController } from './model-catalog-menu'

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

afterEach(() => {
  cleanup()
  setModelVisibilityOpen(false)
  vi.clearAllMocks()
})

const fable = 'anthropic/claude-fable-5.1'
const opus = 'anthropic/claude-opus-5.5'
const scope = { connectionId: 'stable-route-test', profile: 'test-profile' }
const rows = ['openrouter', 'tokenrouter'].map(slug => ({
  slug, name: slug, models: [fable, opus], featured_models: [opus], api_url: `https://${slug}.invalid/v1`
}))

function menu(payload: ModelOptionsResponse = { providers: rows }, owner = scope) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const controller: ModelMenuController = {
    current: { model: opus, provider: 'openrouter', fast: false, effort: '' },
    select: vi.fn(), applyPreset: vi.fn(), presetFor: () => ({}), setOptions: vi.fn()
  }
  const request = vi.fn().mockResolvedValue(payload)
  const tree = (currentOwner = owner) => <QueryClientProvider client={client}>
    <DropdownMenu open><DropdownMenuContent><ModelCatalogMenu
      connectionId={currentOwner.connectionId} controller={controller} profile={currentOwner.profile} request={request}
    /></DropdownMenuContent></DropdownMenu>
  </QueryClientProvider>
  const view = render(tree())
  return { client, controller, request, view, tree }
}

describe('rendered model routes and scoped curation without network', () => {
  it('searches both routes for Fable and selects the exact provider', async () => {
    setVisibleModels(new Set(['openrouter::' + opus, 'tokenrouter::']), scope)
    const { controller } = menu()
    await screen.findByText('openrouter')
    fireEvent.change(screen.getByRole('textbox', { name: 'Search models' }), { target: { value: 'fable' } })
    const fables = await screen.findAllByText(/Fable/i)
    expect(fables).toHaveLength(2)
    fireEvent.click(fables[1])
    expect(controller.select).toHaveBeenCalledWith(fable, 'tokenrouter')
  })

  it('keyboard selection does not equate same-named fast IDs on another route', async () => {
    const mounted = menu({ providers: rows.map(p => ({ ...p, models: [opus, `${opus}-fast`] })) })
    mounted.controller.current.model = `${opus}-fast`
    setVisibleModels(new Set(['openrouter::' + opus, 'tokenrouter::' + opus]), scope)
    await screen.findByText('tokenrouter')
    const input = screen.getByRole('textbox', { name: 'Search models' })

    fireEvent.change(input, { target: { value: 'opus' } })
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(mounted.controller.select).toHaveBeenCalledWith(opus, 'tokenrouter')
  })

  it('shows all families on request and sends Edit Models to the catalog owner', async () => {
    setVisibleModels(new Set(['openrouter::' + opus, 'tokenrouter::']), scope)
    menu()
    await screen.findByText('openrouter')
    expect(screen.queryByText(/Fable/i)).toBeNull()
    fireEvent.click(screen.getByText('Show all models'))
    expect(await screen.findAllByText(/Fable/i)).toHaveLength(2)
    fireEvent.click(screen.getByText('Edit Models…'))
    expect($modelVisibilityOpen.get()).toMatchObject(scope)
  })

  it('refreshes reordered catalogs without changing selection, and isolates a profile switch', async () => {
    setVisibleModels(new Set(['openrouter::' + fable, 'tokenrouter::']), scope)
    setVisibleModels(new Set(), { ...scope, profile: 'other' })
    const mounted = menu()
    await screen.findByText(/Fable/i)
    mounted.client.setQueryData(['model-options', `${scope.connectionId}::${scope.profile}`, 'global'], {
      providers: rows.map(p => ({ ...p, models: [...p.models].reverse() }))
    })
    expect(mounted.controller.current).toMatchObject({ model: opus, provider: 'openrouter' })
    expect(mounted.controller.select).not.toHaveBeenCalled()
    mounted.view.rerender(mounted.tree({ ...scope, profile: 'other' }))
    await vi.waitFor(() => expect(screen.queryByText(/Fable/i)).toBeNull())
    mounted.view.unmount()
    mounted.client.clear()
    menu()
    expect(await screen.findByText(/Fable/i)).toBeTruthy()
  })

  it('projects explicit stale metadata and keeps old backend rows working', async () => {
    setVisibleModels(new Set(['openrouter::' + opus]), scope)
    menu({ providers: [{ ...rows[0], catalog_status: { source: 'cache', freshness: 'stale', degraded: true } }] } as unknown as ModelOptionsResponse)
    expect(await screen.findByText('Showing a saved catalog. Refresh to check availability.')).toBeTruthy()
  })

  it('Edit Models lists every family and writes only to its scope', async () => {
    const gw = { request: vi.fn().mockResolvedValue({ providers: rows }) }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><ModelVisibilityDialog
      connectionId={scope.connectionId} gw={gw as never}
      onOpenChange={vi.fn()} onOpenProviders={vi.fn()} open profile={scope.profile}
    /></QueryClientProvider>)
    expect(await screen.findAllByText(/Fable/i)).toHaveLength(2)
  })
})
