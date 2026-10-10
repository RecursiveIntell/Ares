import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from '@/api/client'
import { getGlobalModelOptions as readGlobalModelOptions } from '@/api/models'
import { ModelPickerDialog } from '@/components/model-picker'
import type { HermesApiRequest } from '@/global'
import { getGlobalModelOptions } from '@/hermes'
import type { ModelOptionsResponse } from '@/types/hermes'

import { deferred } from '../test/deferred'

import { modelOptionsQueryKey, requestModelOptions, selectionUnavailable } from './model-options'

const globalOptions = { model: 'hermes-4', provider: 'nous', providers: [] }

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: vi.fn(() => Promise.resolve(globalOptions))
}))

// Electron helpers have a separate compile project. Load their real pure
// exports for this bridge regression without pulling that project into the
// renderer's strict typecheck or changing Electron source/compiler settings.
const { apiRequestRegistryConnectionId, resolveProfileApiRequest } = await vi.importActual<{
  apiRequestRegistryConnectionId: (request: HermesApiRequest) => null | string
  resolveProfileApiRequest: (
    profile: unknown,
    path: string,
    opts: Record<string, unknown>
  ) => { backendProfile: null | string }
}>('../../electron/connection-config')

const { normalizeRegistry, resolvedConnectionId, resolveRegistryLocalRoute } = await vi.importActual<{
  normalizeRegistry: (input: unknown) => unknown
  resolvedConnectionId: (registry: unknown, descriptor: Record<string, unknown>) => null | string
  resolveRegistryLocalRoute: (
    profile: unknown,
    opts: { globalRemote: boolean }
  ) => { delegate: boolean; poolKey: string }
}>('../../electron/connection-registry')

const { resolveDesktopRemoteRoute } = await vi.importActual<{
  resolveDesktopRemoteRoute: (
    input: Record<string, unknown>
  ) => null | { connectionId?: string; kind: string; source: string; url?: string }
}>('../../electron/desktop-remote-route')

describe('requestModelOptions', () => {
  afterEach(() => {
    vi.clearAllMocks()
    setApiRequestConnection(null)
    setApiRequestProfile(null)
  })

  it('uses the connected gateway even before a session exists', async () => {
    const gatewayPayload = {
      model: 'BeastMode',
      provider: 'moa',
      providers: [{ models: ['BeastMode'], name: 'Mixture of Agents', slug: 'moa' }]
    }

    const gateway = {
      request: vi.fn(() => Promise.resolve(gatewayPayload))
    }

    await expect(requestModelOptions({ gateway: gateway as never, sessionId: null })).resolves.toBe(gatewayPayload)

    expect(gateway.request).toHaveBeenCalledWith('model.options', { explicit_only: true, profile: 'default' })
    expect(getGlobalModelOptions).not.toHaveBeenCalled()
  })

  it('recovers an empty gateway catalog through profile-scoped REST without replacing the session selection', async () => {
    const gatewayPayload = { model: 'hermes-local', provider: 'hermes-local' }

    const restPayload = {
      model: 'profile-default',
      provider: 'openai-codex',
      providers: [{ models: ['hermes-local'], name: 'Hermes Local vLLM', slug: 'hermes-local' }]
    }

    const gateway = {
      request: vi.fn(() => Promise.resolve(gatewayPayload))
    }

    vi.mocked(getGlobalModelOptions).mockResolvedValueOnce(restPayload)

    await expect(requestModelOptions({ gateway: gateway as never, sessionId: 'session-1' })).resolves.toEqual({
      ...restPayload,
      model: 'hermes-local',
      provider: 'hermes-local'
    })

    expect(getGlobalModelOptions).toHaveBeenCalledWith(
      { explicitOnly: true },
      { connectionId: null, profile: 'default' }
    )
  })

  it('recovers through profile-scoped REST when the gateway catalog request fails', async () => {
    const restPayload = {
      model: 'hermes-local',
      provider: 'hermes-local',
      providers: [{ models: ['hermes-local'], name: 'Hermes Local vLLM', slug: 'hermes-local' }]
    }

    const gateway = {
      request: vi.fn(() => Promise.reject(new Error('gateway request unavailable')))
    }

    vi.mocked(getGlobalModelOptions).mockResolvedValueOnce(restPayload)

    await expect(requestModelOptions({ gateway: gateway as never, sessionId: 'session-1' })).resolves.toEqual(
      restPayload
    )
    expect(getGlobalModelOptions).toHaveBeenCalledWith(
      { explicitOnly: true },
      { connectionId: null, profile: 'default' }
    )
  })

  it('preserves the gateway error when its REST recovery path also fails', async () => {
    const gatewayError = new Error('gateway request unavailable')

    const gateway = {
      request: vi.fn(() => Promise.reject(gatewayError))
    }

    vi.mocked(getGlobalModelOptions).mockRejectedValueOnce(new Error('REST request unavailable'))

    await expect(requestModelOptions({ gateway: gateway as never })).rejects.toBe(gatewayError)
  })

  it('keeps the gateway result when both catalog paths have no selectable models', async () => {
    const gatewayPayload = { model: 'hermes-local', provider: 'hermes-local', providers: [] }

    const gateway = {
      request: vi.fn(() => Promise.resolve(gatewayPayload))
    }

    await expect(requestModelOptions({ gateway: gateway as never })).resolves.toBe(gatewayPayload)
  })

  it('passes the active session id and refresh flag through the gateway', async () => {
    const gateway = {
      request: vi.fn(() => Promise.resolve(globalOptions))
    }

    await requestModelOptions({ gateway: gateway as never, refresh: true, sessionId: 'session-1' })

    expect(gateway.request).toHaveBeenCalledWith('model.options', {
      profile: 'default',
      explicit_only: true,
      refresh: true,
      session_id: 'session-1'
    })
    expect(getGlobalModelOptions).toHaveBeenCalledWith(
      { explicitOnly: true, refresh: true },
      { connectionId: null, profile: 'default' }
    )
  })

  it('falls back to REST when no gateway is connected', async () => {
    await requestModelOptions({ refresh: true })

    expect(getGlobalModelOptions).toHaveBeenCalledWith(
      { explicitOnly: true, refresh: true },
      { connectionId: null, profile: 'default' }
    )
  })

  it('prefers an owner-routed request over the ambient gateway socket', async () => {
    const gatewayPayload = {
      model: 'chrome-model',
      provider: 'nous',
      providers: [{ models: ['chrome-model'], name: 'Nous', slug: 'nous' }]
    }

    const routedPayload = {
      model: 'berry-model',
      provider: 'openai',
      providers: [{ models: ['berry-model'], name: 'OpenAI', slug: 'openai' }]
    }

    const gateway = {
      request: vi.fn(() => Promise.resolve(gatewayPayload))
    }

    const request = vi.fn(() => Promise.resolve(routedPayload)) as unknown as <T>(
      method: string,
      params?: Record<string, unknown>
    ) => Promise<T>

    await expect(requestModelOptions({ gateway: gateway as never, request, sessionId: 'tile-1' })).resolves.toBe(
      routedPayload
    )

    expect(request).toHaveBeenCalledWith('model.options', {
      explicit_only: true,
      profile: 'default',
      session_id: 'tile-1'
    })
    expect(gateway.request).not.toHaveBeenCalled()
  })

  it('scopes REST recovery to the catalog owner profile', async () => {
    const restPayload = {
      model: 'berry-local',
      provider: 'hermes-local',
      providers: [{ models: ['berry-local'], name: 'Hermes Local', slug: 'hermes-local' }]
    }

    const request = vi.fn(() => Promise.reject(new Error('gateway request unavailable')))

    vi.mocked(getGlobalModelOptions).mockResolvedValueOnce(restPayload)

    await expect(requestModelOptions({ profile: 'berry', request, sessionId: 'tile-1' })).resolves.toEqual(restPayload)
    expect(getGlobalModelOptions).toHaveBeenCalledWith({ explicitOnly: true }, { connectionId: null, profile: 'berry' })
  })

  it('freezes source and target before a late gateway failure', async () => {
    vi.mocked(getGlobalModelOptions).mockResolvedValueOnce({
      providers: [{ slug: 'target-a', name: 'Target A', models: ['model-a'] }]
    })
    setApiRequestConnection('source-a')
    setApiRequestProfile('target-a')
    let reject!: (err: Error) => void
    const request = vi.fn(
      () =>
        new Promise<never>((_, fail) => {
          reject = fail
        })
    )
    const pending = requestModelOptions({ request, sessionId: 'session-a' })
    setApiRequestConnection('source-b')
    setApiRequestProfile('target-b')
    reject(new Error('late gateway failure'))
    await pending
    expect(getGlobalModelOptions).toHaveBeenCalledWith(
      { explicitOnly: true },
      { connectionId: 'source-a', profile: 'target-a' }
    )
  })

  it('keeps an explicit local owner on local during ambient remote activity', async () => {
    setApiRequestConnection('remote-source')
    await requestModelOptions({ connectionId: 'local', profile: 'local-specialist' })
    expect(getGlobalModelOptions).toHaveBeenCalledWith(
      { explicitOnly: true },
      { connectionId: 'local', profile: 'local-specialist' }
    )
  })
})

describe('modelOptionsQueryKey', () => {
  it('isolates new-chat catalogs by active gateway profile', () => {
    expect(modelOptionsQueryKey('default')).toEqual(['model-options', 'default', 'global'])
    expect(modelOptionsQueryKey('compass')).toEqual(['model-options', 'compass', 'global'])
    expect(modelOptionsQueryKey('default')).not.toEqual(modelOptionsQueryKey('compass'))
  })

  it('keeps session catalogs inside the owning profile namespace', () => {
    expect(modelOptionsQueryKey(' compass ', 'session-1')).toEqual(['model-options', 'compass', 'session-1'])
  })

  it('isolates identically named profiles and sessions on different sources', () => {
    expect(modelOptionsQueryKey('target', 'session', 'remote-a')).not.toEqual(
      modelOptionsQueryKey('target', 'session', 'remote-b')
    )
    expect(modelOptionsQueryKey('target', 'session', 'local')).toEqual(['model-options', 'local::target', 'session'])
  })
})

describe('selectionUnavailable', () => {
  const row = { slug: 'ollama-launch', name: 'Ollama', aliases: ['custom:ollama-launch'], models: ['model-a'] }

  it('compares provider and model together and accepts canonical aliases', () => {
    expect(selectionUnavailable([row], 'ollama-launch', 'model-a')).toBe(false)
    expect(selectionUnavailable([row], 'custom:ollama-launch', 'model-a')).toBe(false)
    expect(selectionUnavailable([row], 'another-host', 'model-a')).toBe(true)
  })

  it('explains missing, disabled, and removed selections without choosing replacements', () => {
    expect(selectionUnavailable([], 'ollama-launch', 'model-a')).toBe(true)
    expect(selectionUnavailable([{ ...row, models: [] }], 'ollama-launch', 'model-a')).toBe(true)
    expect(selectionUnavailable([row], 'ollama-launch', 'model-b')).toBe(true)
    expect(selectionUnavailable([{ ...row, unavailable_models: ['model-a'] }], 'ollama-launch', 'model-a')).toBe(true)
    expect(selectionUnavailable(undefined, 'ollama-launch', 'model-a')).toBe(false)
  })
})

describe('legacy-null and explicit-local catalog authority', () => {
  const env = { url: 'https://legacy-a.invalid', token: 'inert-routing-fixture' }

  const registry = normalizeRegistry({
    version: 2,
    primary: 'local',
    connections: [{ id: 'local', kind: 'local', label: 'This device' }]
  })

  const catalog = (model: string): ModelOptionsResponse => ({
    model,
    provider: model + '-provider',
    providers: [{ slug: model + '-provider', name: model + ' provider', models: [model] }]
  })

  const routed: { request: HermesApiRequest; target: string }[] = []

  // main's API branch uses this real tag resolver. Its legacy branch uses
  // resolveProfileApiRequest/resolveDesktopRemoteRoute; registry local uses
  // resolveRegistryLocalRoute. Exercise those pure decisions without main,
  // spawning, network, or a live connection/configuration.
  async function bridge<T>(request: HermesApiRequest): Promise<T> {
    expect(request.path).toMatch(/^\/api\/model\/options\?/)
    const connectionId = apiRequestRegistryConnectionId(request)

    if (connectionId === null) {
      const route = resolveProfileApiRequest(request.profile, request.path, {
        primaryProfile: 'default',
        globalRemote: true
      })

      const remote = resolveDesktopRemoteRoute({
        config: { mode: 'local' },
        env,
        profile: route.backendProfile,
        registry
      })

      expect(route.backendProfile).toBeNull()
      expect(remote).toMatchObject({ kind: 'remote', source: 'env', url: env.url })
      expect(remote?.connectionId).toBeUndefined()
      expect(
        resolvedConnectionId(registry, {
          mode: 'remote',
          remoteKind: 'url',
          baseUrl: env.url,
          token: env.token,
          authMode: 'token'
        })
      ).toBeNull()
      routed.push({ request, target: env.url })

      return catalog('model-legacy-a') as T
    }

    expect(connectionId).toBe('local')
    const local = resolveRegistryLocalRoute(request.profile, { globalRemote: Boolean(env.url) })
    expect(local).toEqual({ delegate: false, poolKey: 'conn:local::default' })
    routed.push({ request, target: local.poolKey })

    return catalog('model-local-b') as T
  }

  beforeEach(() => {
    routed.length = 0
    setApiRequestConnection(null)
    setApiRequestProfile('default')
    vi.mocked(getGlobalModelOptions).mockReset().mockImplementation(readGlobalModelOptions)
    vi.stubGlobal('hermesDesktop', { api: vi.fn(bridge) })
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    vi.mocked(getGlobalModelOptions).mockReset().mockResolvedValue(globalOptions)
    setApiRequestConnection(null)
    setApiRequestProfile(null)
  })

  it('distinguishes keys for physically separate legacy environment remote A and forced-local B', async () => {
    const remote = resolveDesktopRemoteRoute({ config: { mode: 'local' }, env, profile: 'default', registry })
    expect(remote).toMatchObject({ kind: 'remote', source: 'env', url: env.url })
    expect(remote?.connectionId).toBeUndefined()
    expect(resolveRegistryLocalRoute('default', { globalRemote: Boolean(env.url) })).toEqual({
      delegate: false,
      poolKey: 'conn:local::default'
    })
    expect(modelOptionsQueryKey('default', null, null)).not.toEqual(modelOptionsQueryKey('default', null, 'local'))
  })

  it.each(['rejected', 'empty'])(
    'recovers a %s legacy RPC through the real API and legacy bridge route',
    async failure => {
      const request = vi.fn(async () => {
        if (failure === 'rejected') {
          throw new Error('inert legacy RPC failure')
        }

        return { providers: [] }
      })

      await expect(
        requestModelOptions({ connectionId: null, profile: 'default', request: request as never })
      ).resolves.toEqual(catalog('model-legacy-a'))
      expect(request).toHaveBeenCalledWith('model.options', { explicit_only: true, profile: 'default' })
      expect(routed).toEqual([{ request: expect.objectContaining({ profile: 'default' }), target: env.url }])
      expect(routed[0].request).not.toHaveProperty('connectionId')
    }
  )

  it('keeps forced-local B recovery registry-pinned while the legacy primary resolves to remote A', async () => {
    const request = vi.fn(async () => {
      throw new Error('inert local RPC failure')
    })
    await expect(requestModelOptions({ connectionId: 'local', profile: 'default', request })).resolves.toEqual(
      catalog('model-local-b')
    )
    expect(routed).toEqual([
      {
        request: expect.objectContaining({ connectionId: 'local', profile: 'default' }),
        target: 'conn:local::default'
      }
    ])
  })

  it('retains captured legacy A when its RPC fails after the foreground moves to named source C', async () => {
    const pending = deferred<ModelOptionsResponse>()
    const request = vi.fn(() => pending.promise)
    const result = requestModelOptions({ request: request as never })
    setApiRequestConnection('source-c')
    setApiRequestProfile('other-profile')
    pending.reject(new Error('inert late legacy RPC failure'))
    await expect(result).resolves.toEqual(catalog('model-legacy-a'))
    expect(routed[0]).toMatchObject({ request: { profile: 'default' }, target: env.url })
    expect(routed[0].request).not.toHaveProperty('connectionId')
  })

  it('keeps a late legacy A catalog in its own cache without painting the foreground local B picker', async () => {
    Element.prototype.scrollIntoView = vi.fn()
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      }
    )
    const pending = deferred<ModelOptionsResponse>()
    const requestA = vi.fn(() => pending.promise)
    const requestB = vi.fn(async () => {
      throw new Error('inert local RPC failure')
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const picker = (connectionId: null | string) =>
      createElement(
        QueryClientProvider,
        { client },
        createElement(ModelPickerDialog, {
          connectionId,
          profile: 'default',
          request: (connectionId === null ? requestA : requestB) as never,
          open: true,
          onOpenChange: vi.fn(),
          onSelect: vi.fn(),
          currentModel: '',
          currentProvider: ''
        })
      )

    const view = render(picker(null))

    try {
      await waitFor(() => expect(requestA).toHaveBeenCalledOnce())
      setApiRequestConnection('local')
      view.rerender(picker('local'))
      await screen.findByText('model-local-b')
      setApiRequestConnection('source-c')
      setApiRequestProfile('other-profile')
      pending.reject(new Error('inert late legacy RPC failure'))
      await waitFor(() =>
        expect(client.getQueryData(modelOptionsQueryKey('default', null, null))).toMatchObject({
          model: 'model-legacy-a'
        })
      )
      expect(client.getQueryData(modelOptionsQueryKey('default', null, 'local'))).toMatchObject({
        model: 'model-local-b'
      })
      expect(screen.queryByText('model-legacy-a')).toBeNull()
      expect(screen.getByText('model-local-b')).toBeTruthy()
    } finally {
      pending.resolve({ providers: [] })
      view.unmount()
      client.clear()
    }
  })
})
