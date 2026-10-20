import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { DropdownMenu, DropdownMenuContent } from '@/components/ui/dropdown-menu'
import {
  $modelVisibilityOpen,
  $visibleModels,
  modelVisibilityKey,
  setModelVisibilityOpen,
  setVisibleModels
} from '@/store/model-visibility'

import { ModelCatalogMenu, type ModelMenuController } from './model-catalog-menu'

// Radix calls these on open; jsdom doesn't implement them.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

const getGlobalModelOptions = vi.fn()

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: (...args: unknown[]) => getGlobalModelOptions(...args),
  setApiRequestProfile: vi.fn()
}))

beforeEach(() => {
  $visibleModels.set(null)
  setModelVisibilityOpen(false)
  getGlobalModelOptions.mockResolvedValue({
    providers: [{ models: ['gemini-3.1-pro', 'gemini-2.5-flash'], name: 'Google', slug: 'google' }]
  })
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

// A minimal controller — these tests are about the CATALOG's own behaviour
// (what it lists, what it offers), not about what any host does with a pick.
function renderMenu(overrides: Partial<ModelMenuController> = {}) {
  const select = vi.fn()

  const controller: ModelMenuController = {
    applyPreset: vi.fn(),
    current: { effort: '', fast: false, model: '', provider: '' },
    presetFor: () => ({}),
    select,
    setOptions: vi.fn(),
    ...overrides
  }

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  render(
    <QueryClientProvider client={client}>
      <DropdownMenu open>
        <DropdownMenuContent>
          <ModelCatalogMenu controller={controller} />
        </DropdownMenuContent>
      </DropdownMenu>
    </QueryClientProvider>
  )

  return select
}

// Curation is ONE global preference, so it belongs to the catalog rather than
// to whichever surface mounted it. If a host had to opt in, the composer and
// the kanban board would end up disagreeing about what "my models" means —
// which is exactly the drift extracting this component was meant to prevent.
describe('the catalog owns model curation', () => {
  it('preserves variant Fast across families without applying a preset', async () => {
    getGlobalModelOptions.mockResolvedValue({
      providers: [
        {
          slug: 'fixture',
          name: 'Fixture',
          models: ['alpha', 'alpha-fast', 'beta', 'beta-fast']
        }
      ]
    })
    const applyPreset = vi.fn()

    const select = renderMenu({
      preserveOptionsOnSelect: true,
      applyPreset,
      current: { model: 'alpha-fast', provider: 'fixture', effort: 'high', fast: false }
    })

    fireEvent.click(await screen.findByText('Beta'))
    await vi.waitFor(() => expect(select).toHaveBeenCalledWith('beta-fast', 'fixture'))
    expect(applyPreset).not.toHaveBeenCalled()
  })

  it('a preserving surface selects only the model, never a remembered effort/Fast preset', async () => {
    const applyPreset = vi.fn()

    const select = renderMenu({
      preserveOptionsOnSelect: true,
      applyPreset,
      current: { model: '', provider: '', effort: 'high', fast: false },
      presetFor: () => ({ effort: 'low', fast: true })
    })

    fireEvent.click(await screen.findByText(/Gemini 3\.1 Pro/i))
    await vi.waitFor(() => expect(select).toHaveBeenCalledWith('gemini-3.1-pro', 'google'))
    expect(applyPreset).not.toHaveBeenCalled()
  })

  it('still applies no preset when an older selection settles after the newer one', async () => {
    const applyPreset = vi.fn()
    const settle: Array<(ok: boolean) => void> = []
    const select = vi.fn(() => new Promise<boolean>(resolve => settle.push(resolve)))

    renderMenu({
      applyPreset,
      current: { model: '', provider: '', effort: 'high', fast: false },
      preserveOptionsOnSelect: true,
      presetFor: () => ({ effort: 'low', fast: true }),
      select
    })

    // The user picks one model, then the newer one they actually want.
    fireEvent.click(await screen.findByText(/Gemini 2\.5 Flash/i))
    fireEvent.click(await screen.findByText(/Gemini 3\.1 Pro/i))

    expect(select).toHaveBeenCalledWith('gemini-2.5-flash', 'google')
    expect(select).toHaveBeenCalledWith('gemini-3.1-pro', 'google')

    // Newer ACK first, older pick settling last — order must not matter.
    await act(async () => {
      settle[1](true)
      settle[0](true)
    })

    expect(applyPreset).not.toHaveBeenCalled()
  })

  it('recovers a transient empty catalog without closing or restarting the picker', async () => {
    getGlobalModelOptions
      .mockResolvedValueOnce({ providers: [] })
      .mockResolvedValueOnce({ providers: [{ models: ['gemini-3.1-pro'], name: 'Google', slug: 'google' }] })

    renderMenu()

    await screen.findByText('No models found')
    await screen.findByText(/Gemini 3\.1 Pro/i, {}, { timeout: 1_000 })

    expect(getGlobalModelOptions).toHaveBeenCalledTimes(2)
  })

  it('honours the stored Edit Models shortlist', async () => {
    setVisibleModels(new Set([modelVisibilityKey('google', 'gemini-2.5-flash')]))

    renderMenu()

    await screen.findByText(/Gemini 2\.5 Flash/i)
    expect(screen.queryByText(/Gemini 3\.1 Pro/i)).toBeNull()
  })

  it('still finds a hidden model by search — curation narrows the default view, not the catalog', async () => {
    setVisibleModels(new Set([modelVisibilityKey('google', 'gemini-2.5-flash')]))

    renderMenu()
    await screen.findByText(/Gemini 2\.5 Flash/i)

    const input = screen.getByRole('textbox', { name: 'Search models' })

    fireEvent.change(input, { target: { value: 'gemini-3.1' } })

    await vi.waitFor(() => {
      expect(screen.queryByText(/Gemini 3\.1 Pro/i)).not.toBeNull()
    })
  })

  it('offers Edit Models without the host wiring it up', async () => {
    renderMenu()
    await screen.findByText(/Gemini 3\.1 Pro/i)

    fireEvent.click(screen.getByText('Edit Models…'))

    expect($modelVisibilityOpen.get()).toEqual({ connectionId: null, profile: 'default', sessionId: null })
  })
})
