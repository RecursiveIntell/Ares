import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { getApiRequestConnection, setApiRequestConnection, setApiRequestProfile } from '@/api/client'
import { getHermesConfigRecord as readConfigOverBridge, saveHermesConfig as saveConfigOverBridge } from '@/api/config'
import { $activeGatewayProfile, $newChatProfile, $newChatRoute } from '@/store/profile'
import { $connection, _resetComposerModelSelectionsForTests } from '@/store/session'

import { deferred } from '../../test/deferred'

// Radix Select calls scrollIntoView on its items when the content opens; jsdom
// doesn't implement it (nor hasPointerCapture / releasePointerCapture), so stub
// them to let the dropdown open in tests.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

const getGlobalModelInfo = vi.fn()
const getGlobalModelOptions = vi.fn()
const getAuxiliaryModels = vi.fn()
const getMoaModels = vi.fn()
const setModelAssignment = vi.fn()
const getRecommendedDefaultModel = vi.fn()
const saveMoaModels = vi.fn()
const setEnvVar = vi.fn()
const getHermesConfigRecord = vi.fn()
const saveHermesConfig = vi.fn()
const startManualLocalEndpoint = vi.fn()
const startManualOnboarding = vi.fn()
const startManualProviderOAuth = vi.fn()
let profileSwitchHandler: (() => void) | null = null

vi.mock('@/hermes', () => ({
  getGlobalModelInfo: (profile?: null | string) => getGlobalModelInfo(profile),
  getGlobalModelOptions: (opts?: unknown, profile?: null | string) => getGlobalModelOptions(opts, profile),
  getAuxiliaryModels: (profile?: null | string) => getAuxiliaryModels(profile),
  getApiRequestProfile: () => 'default',
  getMoaModels: (profile?: null | string) => getMoaModels(profile),
  profileScopeKey: (scope?: null | string | { connectionId?: string | null; profile?: string | null }) =>
    typeof scope === 'object' && scope
      ? `${scope.connectionId || 'local'}::${scope.profile || 'default'}`
      : (scope ?? '').trim() || 'default',
  setModelAssignment: (body: unknown) => setModelAssignment(body),
  getRecommendedDefaultModel: (slug: string) => getRecommendedDefaultModel(slug),
  saveMoaModels: (body: unknown) => saveMoaModels(body),
  setEnvVar: (key: string, value: string) => setEnvVar(key, value),
  getHermesConfigRecord: (profile?: null | string) => getHermesConfigRecord(profile),
  saveHermesConfig: (config: unknown, profile?: null | string) => saveHermesConfig(config, profile),
  setApiRequestProfile: () => {}
}))

vi.mock('@/store/onboarding', () => ({
  startManualLocalEndpoint: () => startManualLocalEndpoint(),
  startManualOnboarding: () => startManualOnboarding(),
  startManualProviderOAuth: (slug: string) => startManualProviderOAuth(slug)
}))

vi.mock('../hooks/use-on-profile-switch', () => ({
  useOnProfileSwitch: (handler: () => void) => {
    profileSwitchHandler = handler
  }
}))

beforeEach(() => {
  _resetComposerModelSelectionsForTests()
  $connection.set(null)
  $newChatRoute.set(null)
  $newChatProfile.set(null)
  $activeGatewayProfile.set('default')
  setApiRequestConnection(null)
  setApiRequestProfile('default')
  getGlobalModelInfo.mockResolvedValue({ provider: 'nous', model: 'hermes-4' })
  getGlobalModelOptions.mockResolvedValue({
    providers: [
      {
        name: 'Nous',
        slug: 'nous',
        models: ['hermes-4', 'hermes-4-mini'],
        authenticated: true,
        capabilities: { 'hermes-4': { reasoning: true, fast: true } }
      }
    ]
  })
  getAuxiliaryModels.mockResolvedValue({
    main: { provider: 'nous', model: 'hermes-4' },
    tasks: [{ task: 'vision', provider: 'auto', model: '', base_url: '' }]
  })
  getMoaModels.mockResolvedValue(null)
  setModelAssignment.mockResolvedValue({ ok: true, provider: 'nous', model: 'hermes-4', gateway_tools: [] })
  getRecommendedDefaultModel.mockResolvedValue({ provider: 'nous', model: 'hermes-4', free_tier: null })
  setEnvVar.mockResolvedValue({ ok: true })
  getHermesConfigRecord.mockResolvedValue({ agent: { reasoning_effort: 'medium', service_tier: 'normal' } })
  saveHermesConfig.mockResolvedValue({ ok: true })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  profileSwitchHandler = null
})

async function renderModelSettings(scopeProfile?: string, onMainModelChanged = vi.fn()) {
  const { ModelSettings } = await import('./model-settings')
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  return render(
    // The aux-task deep-link highlight reads useSearchParams, so the page
    // needs a router context in tests (the app provides HashRouter at root).
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <ModelSettings onMainModelChanged={onMainModelChanged} scopeProfile={scopeProfile} />
      </QueryClientProvider>
    </MemoryRouter>
  )
}

describe('ModelSettings profile scope', () => {
  it('reloads the form for same-named source owners A → B → A before saving', async () => {
    const rehome = (connectionId: string) => {
      setApiRequestConnection(connectionId)
      $connection.set({ connectionId } as never)
      $newChatRoute.set({ connectionId, profile: 'default' })
    }

    rehome('source-a')
    getGlobalModelInfo.mockImplementation(async () => ({
      provider: getApiRequestConnection() === 'source-b' ? 'custom:b' : 'nous',
      model: getApiRequestConnection() === 'source-b' ? 'model-b' : 'hermes-4'
    }))
    getGlobalModelOptions.mockImplementation(async () => ({
      providers:
        getApiRequestConnection() === 'source-b'
          ? [
              {
                name: 'Source B',
                slug: 'custom:b',
                models: ['model-b'],
                authenticated: true,
                api_url: 'https://b.invalid/v1'
              }
            ]
          : [{ name: 'Nous', slug: 'nous', models: ['hermes-4'], authenticated: true }]
    }))
    await renderModelSettings()
    await screen.findByRole('button', { name: 'Apply' })
    await act(async () => rehome('source-b'))
    await waitFor(() => expect(screen.getAllByRole('combobox')[0].textContent).toContain('Source B'))
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() =>
      expect(setModelAssignment).toHaveBeenCalledWith(
        expect.objectContaining({
          provider: 'custom:b',
          model: 'model-b',
          base_url: 'https://b.invalid/v1'
        })
      )
    )
    await act(async () => rehome('source-a'))
    await waitFor(() => expect(screen.getAllByRole('combobox')[0].textContent).toContain('Nous'))
  })

  it.each(['source-b', 'local'])(
    'keeps config GET/PUT routing and record authority together on %s',
    async destination => {
      const previousBridge = window.hermesDesktop

      const api = vi.fn(async (request: { connectionId?: string; method?: string; body?: unknown }) =>
        request.method === 'PUT'
          ? { ok: true }
          : {
              fixture_source: request.connectionId,
              agent: { reasoning_effort: 'medium', service_tier: 'normal' },
              memory: { enabled: false },
              governor: { semantic_memory_enabled: true }
            }
      )

      window.hermesDesktop = { api } as never
      getHermesConfigRecord.mockImplementation(readConfigOverBridge)
      saveHermesConfig.mockImplementation(saveConfigOverBridge)

      const rehome = (connectionId: string) => {
        setApiRequestConnection(connectionId)
        $connection.set({ connectionId } as never)
        $newChatRoute.set({ connectionId, profile: 'default' })
      }

      try {
        rehome('source-a')
        await renderModelSettings()
        await screen.findByRole('switch')
        await act(async () => rehome(destination))
        await waitFor(() =>
          expect(api).toHaveBeenCalledWith(expect.objectContaining({ connectionId: destination, path: '/api/config' }))
        )
        fireEvent.click(await screen.findByRole('switch'))
        await waitFor(() =>
          expect(api).toHaveBeenCalledWith(
            expect.objectContaining({
              connectionId: destination,
              method: 'PUT',
              body: {
                config: expect.objectContaining({
                  fixture_source: destination,
                  memory: { enabled: false },
                  governor: { semantic_memory_enabled: true }
                })
              }
            })
          )
        )
        expect(getHermesConfigRecord).toHaveBeenCalledWith(undefined)
        expect(saveHermesConfig).toHaveBeenCalledWith(
          expect.objectContaining({ fixture_source: destination }),
          undefined
        )
      } finally {
        window.hermesDesktop = previousBridge
        getHermesConfigRecord.mockReset()
        saveHermesConfig.mockReset()
      }
    }
  )

  it('carries the original source/profile/target through an asynchronous save callback', async () => {
    const owner = { connectionId: 'source-a', profile: 'specialist', targetProfile: 'backend-a' }
    $newChatRoute.set(owner)
    $newChatProfile.set(owner.profile)
    $activeGatewayProfile.set(owner.profile)
    setApiRequestConnection(owner.connectionId)
    setApiRequestProfile(owner.profile)
    const pending = deferred<unknown>()
    setModelAssignment.mockReturnValueOnce(pending.promise)
    const changed = vi.fn()
    await renderModelSettings(undefined, changed)
    fireEvent.click(await screen.findByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(setModelAssignment).toHaveBeenCalledOnce())
    const originalSource = getApiRequestConnection()
    await act(async () => {
      setApiRequestConnection('source-b')
      $connection.set({ connectionId: 'source-b' } as never)
    })
    pending.resolve({ ok: true, provider: 'nous', model: 'hermes-4', gateway_tools: [] })
    await waitFor(() => expect(changed).toHaveBeenCalledOnce())
    expect(changed).toHaveBeenCalledWith(
      expect.objectContaining({
        owner,
        model: 'hermes-4',
        provider: 'nous',
        startedAtGeneration: expect.any(Number),
        saveIntent: expect.objectContaining({ target: JSON.stringify(['source-a', 'specialist', 'backend-a']) })
      })
    )
    expect(originalSource).toBe('source-a')
  })

  it('publishes no callback or receipt when the main-model save fails', async () => {
    setModelAssignment.mockRejectedValueOnce(new Error('fixture-save-failed'))
    const changed = vi.fn()
    await renderModelSettings(undefined, changed)
    fireEvent.click(await screen.findByRole('button', { name: 'Apply' }))
    await screen.findByText('fixture-save-failed')
    expect(changed).not.toHaveBeenCalled()
  })

  // #90549: the API helpers treat `null` as "deliberately target the
  // primary/default profile". A page following the active profile must pass
  // `undefined`, or every read repaints the primary's model and the user's
  // change looks reverted.
  it('follows the active profile (undefined, never null) when unscoped', async () => {
    await renderModelSettings()

    await waitFor(() => expect(getGlobalModelInfo).toHaveBeenCalledWith(undefined))
    expect(getGlobalModelOptions).toHaveBeenCalledWith(undefined, undefined)
    expect(getAuxiliaryModels).toHaveBeenCalledWith(undefined)
    expect(getMoaModels).toHaveBeenCalledWith(undefined)
  })

  it('reads through the explicit scope override when one is set', async () => {
    await renderModelSettings('research')

    await waitFor(() => expect(getGlobalModelInfo).toHaveBeenCalledWith('research'))
    expect(getGlobalModelOptions).toHaveBeenCalledWith(undefined, 'research')
    expect(getAuxiliaryModels).toHaveBeenCalledWith('research')
    expect(getMoaModels).toHaveBeenCalledWith('research')
  })
})

describe('ModelSettings', () => {
  it('loads the current main model and lists configured providers only', async () => {
    await renderModelSettings()

    await waitFor(() => expect(getGlobalModelInfo).toHaveBeenCalled())
    await waitFor(() => expect(getGlobalModelOptions).toHaveBeenCalled())

    // Open the provider Select — only configured providers should be listed.
    const triggers = await screen.findAllByRole('combobox')
    fireEvent.click(triggers[0])

    // "Nous" shows in both the trigger and the open list.
    expect((await screen.findAllByText('Nous')).length).toBeGreaterThan(0)
    expect(screen.queryByText(/DeepSeek/)).toBeNull()
  })

  it.each(['custom', 'local', 'custom:lab'])(
    'opens local endpoint setup when %s has no inventory row',
    async provider => {
      getGlobalModelInfo.mockResolvedValueOnce({ provider, model: '' })
      getGlobalModelOptions.mockResolvedValueOnce({ providers: [] })

      await renderModelSettings()

      const providerSelect = (await screen.findAllByRole('combobox'))[0]

      expect(providerSelect.textContent).toContain(provider)
      expect(screen.queryByText(/undefined/)).toBeNull()
      expect(screen.queryByText(/signs in through your browser/)).toBeNull()

      fireEvent.click(await screen.findByRole('button', { name: 'Set up provider' }))

      expect(startManualLocalEndpoint).toHaveBeenCalledOnce()
      expect(startManualOnboarding).not.toHaveBeenCalled()
      expect(startManualProviderOAuth).not.toHaveBeenCalled()
    }
  )

  it('opens the generic provider picker for an unknown provider with no inventory row', async () => {
    getGlobalModelInfo.mockResolvedValueOnce({ provider: 'retired-provider', model: '' })
    getGlobalModelOptions.mockResolvedValueOnce({ providers: [] })

    await renderModelSettings()

    fireEvent.click(await screen.findByRole('button', { name: 'Set up provider' }))

    expect(startManualOnboarding).toHaveBeenCalledOnce()
    expect(startManualLocalEndpoint).not.toHaveBeenCalled()
    expect(startManualProviderOAuth).not.toHaveBeenCalled()
  })

  it('deep-links a known OAuth provider row into its setup flow', async () => {
    getGlobalModelInfo.mockResolvedValueOnce({ provider: 'anthropic', model: '' })
    getGlobalModelOptions.mockResolvedValueOnce({
      providers: [
        {
          name: 'Anthropic',
          slug: 'anthropic',
          models: [],
          authenticated: false,
          auth_type: 'oauth'
        }
      ]
    })

    await renderModelSettings()

    fireEvent.click(await screen.findByRole('button', { name: 'Set up Anthropic' }))

    expect(startManualProviderOAuth).toHaveBeenCalledWith('anthropic')
    expect(startManualLocalEndpoint).not.toHaveBeenCalled()
    expect(startManualOnboarding).not.toHaveBeenCalled()
  })

  it('replaces the selected provider and model when the active profile changes', async () => {
    getGlobalModelInfo
      .mockResolvedValueOnce({ provider: 'custom', model: 'local-a' })
      .mockResolvedValueOnce({ provider: 'nous', model: 'hermes-4' })
    getGlobalModelOptions
      .mockResolvedValueOnce({
        providers: [
          {
            name: 'Custom A',
            slug: 'custom',
            models: ['local-a'],
            authenticated: true
          }
        ]
      })
      .mockResolvedValueOnce({
        providers: [
          {
            name: 'Nous',
            slug: 'nous',
            models: ['hermes-4'],
            authenticated: true,
            capabilities: { 'hermes-4': { reasoning: true, fast: true } }
          }
        ]
      })

    await renderModelSettings()
    expect((await screen.findAllByRole('combobox'))[0].textContent).toContain('Custom A')

    await act(async () => {
      profileSwitchHandler?.()
    })

    await waitFor(() => expect(getGlobalModelInfo).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getAllByRole('combobox')[0].textContent).toContain('Nous'))
    expect(screen.queryByRole('button', { name: 'Set up provider' })).toBeNull()
  })

  it('preserves a user-defined provider endpoint when applying the main model', async () => {
    getGlobalModelOptions.mockResolvedValueOnce({
      providers: [
        {
          name: 'Nous',
          slug: 'nous',
          models: ['hermes-4'],
          authenticated: true
        },
        {
          name: 'Ollama',
          slug: 'local-ollama',
          models: ['qwen3:latest'],
          authenticated: true,
          is_user_defined: true,
          api_url: 'http://localhost:11434/v1'
        }
      ]
    })
    setModelAssignment.mockResolvedValueOnce({
      ok: true,
      provider: 'local-ollama',
      model: 'qwen3:latest',
      gateway_tools: []
    })

    await renderModelSettings()

    const providerSelect = (await screen.findAllByRole('combobox'))[0]
    fireEvent.click(providerSelect)
    fireEvent.click(await screen.findByRole('option', { name: 'Ollama' }))

    const modelSelect = (await screen.findAllByRole('combobox'))[1]
    fireEvent.click(modelSelect)
    fireEvent.click(await screen.findByRole('option', { name: 'qwen3:latest' }))

    fireEvent.click(await screen.findByRole('button', { name: 'Apply' }))

    await waitFor(() =>
      expect(setModelAssignment).toHaveBeenCalledWith({
        model: 'qwen3:latest',
        provider: 'local-ollama',
        scope: 'main',
        base_url: 'http://localhost:11434/v1'
      })
    )
  })

  it('writes the profile default speed (service_tier) when the fast switch is toggled', async () => {
    await renderModelSettings()
    await waitFor(() => expect(getHermesConfigRecord).toHaveBeenCalled())

    const fastSwitch = await screen.findByRole('switch')
    fireEvent.click(fastSwitch)

    await waitFor(() =>
      expect(saveHermesConfig).toHaveBeenCalledWith(
        expect.objectContaining({ agent: expect.objectContaining({ service_tier: 'fast' }) }),
        undefined
      )
    )
  })

  it('hides the reasoning/speed defaults when the main model reports no capabilities', async () => {
    getGlobalModelOptions.mockResolvedValueOnce({
      providers: [
        {
          name: 'Nous',
          slug: 'nous',
          models: ['hermes-4'],
          authenticated: true,
          capabilities: { 'hermes-4': { reasoning: false, fast: false } }
        }
      ]
    })

    await renderModelSettings()
    await waitFor(() => expect(getHermesConfigRecord).toHaveBeenCalled())

    expect(screen.queryByRole('switch')).toBeNull()
  })

  it('renders the auxiliary task rows', async () => {
    await renderModelSettings()

    expect(await screen.findByText('Vision')).toBeTruthy()
    expect(screen.getAllByText('auto · use main model').length).toBeGreaterThan(0)
  })

  it('assigns an auxiliary task to the main model via setModelAssignment', async () => {
    await renderModelSettings()

    // One "Set to main" button per task slot; the first is Vision.
    const setToMainButtons = await screen.findAllByRole('button', { name: 'Set to main' })
    fireEvent.click(setToMainButtons[0])

    await waitFor(() =>
      expect(setModelAssignment).toHaveBeenCalledWith({
        model: 'hermes-4',
        provider: 'nous',
        scope: 'auxiliary',
        task: 'vision'
      })
    )
  })

  it('carries the user-defined endpoint when an aux slot is set to a local main model', async () => {
    getGlobalModelOptions.mockResolvedValueOnce({
      providers: [
        {
          name: 'Ollama',
          slug: 'local-ollama',
          models: ['qwen3:latest'],
          authenticated: true,
          is_user_defined: true,
          api_url: 'http://localhost:11434/v1'
        }
      ]
    })
    getGlobalModelInfo.mockResolvedValueOnce({ provider: 'local-ollama', model: 'qwen3:latest' })
    getAuxiliaryModels.mockResolvedValueOnce({
      main: { provider: 'local-ollama', model: 'qwen3:latest' },
      tasks: [{ task: 'vision', provider: 'auto', model: '', base_url: '' }]
    })

    await renderModelSettings()

    const setToMainButtons = await screen.findAllByRole('button', { name: 'Set to main' })
    fireEvent.click(setToMainButtons[0])

    await waitFor(() =>
      expect(setModelAssignment).toHaveBeenCalledWith({
        model: 'qwen3:latest',
        provider: 'local-ollama',
        scope: 'auxiliary',
        task: 'vision',
        base_url: 'http://localhost:11434/v1'
      })
    )
  })

  it('warns when a main switch leaves auxiliary tasks pinned to another provider', async () => {
    setModelAssignment.mockResolvedValueOnce({
      ok: true,
      provider: 'openrouter',
      model: 'anthropic/claude-opus-4.7',
      gateway_tools: [],
      stale_aux: [{ task: 'compression', provider: 'nous', model: 'hermes-4' }]
    })

    await renderModelSettings()
    await waitFor(() => expect(getGlobalModelInfo).toHaveBeenCalled())

    const applyButton = await screen.findByRole('button', { name: 'Apply' })
    fireEvent.click(applyButton)

    // The switch-time notice names the pinned provider and offers a reset.
    expect(await screen.findByText(/still run on/)).toBeTruthy()
    expect(screen.getByText('nous')).toBeTruthy()
  })

  it('shows a persistent banner when a loaded aux slot mismatches the main provider', async () => {
    getAuxiliaryModels.mockResolvedValueOnce({
      main: { provider: 'nous', model: 'hermes-4' },
      tasks: [{ task: 'curator', provider: 'openrouter', model: 'anthropic/claude-opus-4.7', base_url: '' }]
    })

    await renderModelSettings()

    // Banner present on load, no switch required.
    expect(await screen.findByText(/still run on/)).toBeTruthy()
  })
})

describe('ModelSettings MoA preset editor', () => {
  const moaConfig = () => ({
    default_preset: 'default',
    active_preset: '',
    presets: {
      default: {
        reference_models: [
          { provider: 'nous', model: 'hermes-4' },
          { provider: 'openrouter', model: 'deepseek/deepseek-v4-pro' }
        ],
        aggregator: { provider: 'openrouter', model: 'anthropic/claude-opus-4.8' },
        reference_temperature: 0,
        aggregator_temperature: 0,
        max_tokens: 4096,
        enabled: true
      }
    },
    reference_models: [
      { provider: 'nous', model: 'hermes-4' },
      { provider: 'openrouter', model: 'deepseek/deepseek-v4-pro' }
    ],
    aggregator: { provider: 'openrouter', model: 'anthropic/claude-opus-4.8' },
    reference_temperature: 0,
    aggregator_temperature: 0,
    max_tokens: 4096,
    enabled: true
  })

  beforeEach(() => {
    getGlobalModelOptions.mockResolvedValue({
      providers: [
        {
          name: 'Nous',
          slug: 'nous',
          models: ['hermes-4', 'hermes-4-mini'],
          authenticated: true,
          capabilities: { 'hermes-4': { reasoning: true, fast: true } }
        },
        {
          name: 'OpenRouter',
          slug: 'openrouter',
          models: ['deepseek/deepseek-v4-pro', 'anthropic/claude-opus-4.8'],
          authenticated: true
        }
      ]
    })
    getMoaModels.mockResolvedValue(moaConfig())
    saveMoaModels.mockImplementation((body: unknown) => Promise.resolve(body))
  })

  async function openReferenceEditor() {
    await renderModelSettings()
    expect(await screen.findByText('Reference 1')).toBeTruthy()
  }

  function slotSelects() {
    // Combobox order in the MoA section (last 7 on the page): preset select,
    // then provider+model per reference (2 refs), then aggregator
    // provider+model. Reference 1's pair is therefore at -6 / -5.
    const all = screen.getAllByRole('combobox')

    return { ref1Provider: all.at(-6)!, ref1Model: all.at(-5)! }
  }

  it('holds the autosave while a slot is half-filled (provider changed, model pending)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })

    try {
      await openReferenceEditor()

      fireEvent.click(slotSelects().ref1Provider)
      fireEvent.click(await screen.findByRole('option', { name: 'OpenRouter' }))

      // Model was cleared by the provider change → config incomplete → the
      // debounced autosave must NOT fire, even well past the 600ms window.
      await vi.advanceTimersByTimeAsync(2000)
      expect(saveMoaModels).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('saves once the model pick completes the slot', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })

    try {
      await openReferenceEditor()

      fireEvent.click(slotSelects().ref1Provider)
      fireEvent.click(await screen.findByRole('option', { name: 'OpenRouter' }))
      await vi.advanceTimersByTimeAsync(700)

      fireEvent.click(slotSelects().ref1Model)
      fireEvent.click(await screen.findByRole('option', { name: 'anthropic/claude-opus-4.8' }))
      await vi.advanceTimersByTimeAsync(700)

      expect(saveMoaModels).toHaveBeenCalledTimes(1)
      const sent = saveMoaModels.mock.calls[0][0] as ReturnType<typeof moaConfig>
      expect(sent.presets.default.reference_models[0]).toEqual({
        provider: 'openrouter',
        model: 'anthropic/claude-opus-4.8'
      })
      // The untouched slots ride along unchanged — nothing reverts to defaults.
      expect(sent.presets.default.reference_models[1]).toEqual({
        provider: 'openrouter',
        model: 'deepseek/deepseek-v4-pro'
      })
      expect(sent.presets.default.aggregator).toEqual({
        provider: 'openrouter',
        model: 'anthropic/claude-opus-4.8'
      })
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not clear the model or save when the same provider is re-selected', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })

    try {
      await openReferenceEditor()

      fireEvent.click(slotSelects().ref1Provider)
      fireEvent.click(await screen.findByRole('option', { name: 'Nous' }))
      await vi.advanceTimersByTimeAsync(700)

      // Radix treats re-picking the current value as a no-op (no
      // onValueChange), so nothing changes: no save, model still shown.
      expect(saveMoaModels).not.toHaveBeenCalled()
      expect(screen.getByText('nous · hermes-4')).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })

  it('autosaves the selected preset when its enabled switch is toggled', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })

    try {
      await openReferenceEditor()

      fireEvent.click(screen.getByRole('switch', { name: 'Enabled' }))
      await vi.advanceTimersByTimeAsync(700)

      expect(saveMoaModels).toHaveBeenCalledWith(
        expect.objectContaining({
          presets: expect.objectContaining({
            default: expect.objectContaining({ enabled: false })
          })
        })
      )
    } finally {
      vi.useRealTimers()
    }
  })

  it('saves a disabled reference model without removing it (per-slot enabled toggle)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })

    try {
      await openReferenceEditor()

      fireEvent.click(screen.getByRole('switch', { name: 'Disable reference 1' }))
      await vi.advanceTimersByTimeAsync(700)

      expect(saveMoaModels).toHaveBeenCalledWith(
        expect.objectContaining({
          presets: expect.objectContaining({
            default: expect.objectContaining({
              reference_models: [
                expect.objectContaining({ provider: 'nous', model: 'hermes-4', enabled: false }),
                expect.objectContaining({ provider: 'openrouter', model: 'deepseek/deepseek-v4-pro' })
              ]
            })
          })
        })
      )
    } finally {
      vi.useRealTimers()
    }
  })
})
