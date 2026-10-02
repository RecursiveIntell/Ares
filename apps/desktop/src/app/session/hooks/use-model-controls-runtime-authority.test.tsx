import { QueryClient } from '@tanstack/react-query'
import { cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PRIMARY_SESSION_VIEW } from '@/app/chat/session-view'
import { createClientSessionState } from '@/lib/chat-runtime'
import {
  $activeSessionId,
  $currentModel,
  $currentProvider,
  $selectedStoredSessionId,
  getCurrentModelSource,
  setCurrentModel,
  setCurrentModelSource,
  setCurrentProvider
} from '@/store/session'
import {
  $sessionStates,
  publishSessionState,
  type SessionTileDelegate,
  setSessionTileDelegate
} from '@/store/session-states'

import { useModelControls } from './use-model-controls'

vi.mock('@/hermes', () => ({
  getGlobalModelInfo: vi.fn(),
  setApiRequestProfile: vi.fn()
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: { confirm: 'Confirm' },
      desktop: { modelSwitchFailed: 'Model switch failed' }
    }
  })
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

const PRIMARY_RUNTIME_ID = 'runtime-primary'
const PRIMARY_STORED_ID = 'stored-primary'

function installSessionStateDelegate() {
  const updateSession: SessionTileDelegate['updateSession'] = (runtimeId, updater) => {
    const previous = $sessionStates.get()[runtimeId]

    if (!previous) {
      throw new Error(`No state for ${runtimeId}`)
    }

    const next = updater(previous)
    publishSessionState(runtimeId, next)

    return next
  }

  setSessionTileDelegate({ updateSession } as SessionTileDelegate)
}

describe('useModelControls runtime authority', () => {
  beforeEach(() => {
    $sessionStates.set({})
    $activeSessionId.set(PRIMARY_RUNTIME_ID)
    $selectedStoredSessionId.set(PRIMARY_STORED_ID)
    setCurrentModel('model-a')
    setCurrentProvider('provider-a')
    publishSessionState(PRIMARY_RUNTIME_ID, {
      ...createClientSessionState(PRIMARY_STORED_ID),
      model: 'model-a',
      provider: 'provider-a'
    })
    installSessionStateDelegate()
  })

  afterEach(() => {
    cleanup()
    $sessionStates.set({})
    $activeSessionId.set(null)
    $selectedStoredSessionId.set(null)
    setCurrentModel('')
    setCurrentProvider('')
    setSessionTileDelegate({} as SessionTileDelegate)
    vi.restoreAllMocks()
  })

  it('updates the live primary SessionView selection before the gateway confirms it', async () => {
    const requestGateway = vi.fn(async () => ({ key: 'model', scope: 'global', value: 'model-b' }) as never)
    const { result } = renderHook(() => useModelControls({ queryClient: new QueryClient(), requestGateway }))

    expect(PRIMARY_SESSION_VIEW.$model.get()).toBe('model-a')
    expect(PRIMARY_SESSION_VIEW.$provider.get()).toBe('provider-a')

    await expect(result.current.selectModel({ model: 'model-b', provider: 'provider-b' })).resolves.toBe(true)

    expect(PRIMARY_SESSION_VIEW.$model.get()).toBe('model-b')
    expect(PRIMARY_SESSION_VIEW.$provider.get()).toBe('provider-b')
    expect($sessionStates.get()[PRIMARY_RUNTIME_ID]).toMatchObject({ model: 'model-b', provider: 'provider-b' })
    expect($currentModel.get()).toBe('model-b')
    expect($currentProvider.get()).toBe('provider-b')
  })

  it('restores the live primary SessionView selection when the model write fails', async () => {
    const requestGateway = vi.fn(async () => {
      throw new Error('no such model')
    })

    const { result } = renderHook(() => useModelControls({ queryClient: new QueryClient(), requestGateway }))

    await expect(result.current.selectModel({ model: 'model-b', provider: 'provider-b' })).resolves.toBe(false)

    expect(PRIMARY_SESSION_VIEW.$model.get()).toBe('model-a')
    expect(PRIMARY_SESSION_VIEW.$provider.get()).toBe('provider-a')
    expect($sessionStates.get()[PRIMARY_RUNTIME_ID]).toMatchObject({ model: 'model-a', provider: 'provider-a' })
  })

  it.each([
    { label: 'older rejection first', order: [0, 1] },
    { label: 'newer rejection first', order: [1, 0] }
  ])('restores the last confirmed owner after two overlapping model picks both reject ($label)', async ({ order }) => {
    setCurrentModelSource('default')

    const rejectRequests: Array<(reason: Error) => void> = []

    const requestGateway = vi.fn(
      () =>
        new Promise<never>((_resolve, reject) => {
          rejectRequests.push(reject)
        })
    )

    const { result } = renderHook(() => useModelControls({ queryClient: new QueryClient(), requestGateway }))

    const first = result.current.selectModel({ model: 'model-b', provider: 'provider-b' })
    const second = result.current.selectModel({ model: 'model-c', provider: 'provider-c' })
    expect(rejectRequests).toHaveLength(2)
    expect(PRIMARY_SESSION_VIEW.$model.get()).toBe('model-c')

    const picks = [first, second]

    for (const index of order) {
      rejectRequests[index](new Error(`${index === 0 ? 'B' : 'C'} rejected`))
      await expect(picks[index]).resolves.toBe(false)
    }

    expect(PRIMARY_SESSION_VIEW.$model.get()).toBe('model-a')
    expect(PRIMARY_SESSION_VIEW.$provider.get()).toBe('provider-a')
    expect($sessionStates.get()[PRIMARY_RUNTIME_ID]).toMatchObject({
      model: 'model-a',
      provider: 'provider-a',
      pendingModelSelection: null
    })
    expect($currentModel.get()).toBe('model-a')
    expect($currentProvider.get()).toBe('provider-a')
    expect(getCurrentModelSource()).toBe('default')
  })

  it.each([
    { label: 'before', ackFirst: true },
    { label: 'after', ackFirst: false }
  ])('retains an acknowledged earlier pick when it settles $label the later rejection', async ({ ackFirst }) => {
    const requests: Array<{
      reject: (reason: Error) => void
      resolve: (value: unknown) => void
    }> = []

    const requestGateway = vi.fn(
      () =>
        new Promise<never>((resolve, reject) => {
          requests.push({ reject, resolve: value => resolve(value as never) })
        })
    )

    const { result } = renderHook(() => useModelControls({ queryClient: new QueryClient(), requestGateway }))

    const first = result.current.selectModel({ model: 'model-b', provider: 'provider-b' })
    const second = result.current.selectModel({ model: 'model-c', provider: 'provider-c' })

    if (ackFirst) {
      requests[0].resolve({ key: 'model', scope: 'session', value: 'model-b' })
      await expect(first).resolves.toBe(true)
      requests[1].reject(new Error('C rejected'))
      await expect(second).resolves.toBe(false)
    } else {
      requests[1].reject(new Error('C rejected'))
      await expect(second).resolves.toBe(false)
      requests[0].resolve({ key: 'model', scope: 'session', value: 'model-b' })
      await expect(first).resolves.toBe(true)
    }

    expect(PRIMARY_SESSION_VIEW.$model.get()).toBe('model-b')
    expect(PRIMARY_SESSION_VIEW.$provider.get()).toBe('provider-b')
    expect($sessionStates.get()[PRIMARY_RUNTIME_ID]).toMatchObject({
      model: 'model-b',
      provider: 'provider-b',
      pendingModelSelection: null
    })
    expect($currentModel.get()).toBe('model-b')
    expect($currentProvider.get()).toBe('provider-b')
  })

  it('does not let an older acknowledgement repaint a newer successful choice', async () => {
    const requests: Array<(value: unknown) => void> = []

    const requestGateway = vi.fn(
      () =>
        new Promise<never>(resolve => {
          requests.push(value => resolve(value as never))
        })
    )

    const { result } = renderHook(() => useModelControls({ queryClient: new QueryClient(), requestGateway }))

    const first = result.current.selectModel({ model: 'model-b', provider: 'provider-b' })
    const second = result.current.selectModel({ model: 'model-c', provider: 'provider-c' })

    requests[1]({ key: 'model', scope: 'session', value: 'model-c' })
    await expect(second).resolves.toBe(true)
    requests[0]({ key: 'model', scope: 'session', value: 'model-b' })
    await expect(first).resolves.toBe(true)

    expect(PRIMARY_SESSION_VIEW.$model.get()).toBe('model-c')
    expect(PRIMARY_SESSION_VIEW.$provider.get()).toBe('provider-c')
    expect($currentModel.get()).toBe('model-c')
    expect($currentProvider.get()).toBe('provider-c')
  })

  it.each(['openrouter', 'anthropic', 'openai-codex', 'moa', 'ollama-cloud', 'ollama-launch', 'custom:target', 'auto'])
  ('preserves A→B→A intent when older selector replies arrive last (%s)', async provider => {
    const replies: Array<(value: unknown) => void> = []
    const requestGateway = vi.fn((_method: string, _params?: Record<string, unknown>) => new Promise<never>(resolve => {
      replies.push(value => resolve(value as never))
    }))
    const { result } = renderHook(() => useModelControls({ queryClient: new QueryClient(), requestGateway }))
    const choices = [
      result.current.selectModel({ provider, model: 'model-a' }),
      result.current.selectModel({ provider: 'openrouter', model: 'model-b' }),
      result.current.selectModel({ provider, model: 'model-a' })
    ]
    for (const index of [2, 1, 0]) {
      replies[index]({ key: 'model', scope: 'session', value: index === 1 ? 'model-b' : 'model-a' })
      await expect(choices[index]).resolves.toBe(true)
      expect(PRIMARY_SESSION_VIEW.$model.get()).toBe('model-a')
      expect(PRIMARY_SESSION_VIEW.$provider.get()).toBe(provider)
    }
    expect(requestGateway.mock.calls.map(([, params]) => params)).toEqual([
      expect.objectContaining({ value: `model-a --provider ${provider} --session`, session_id: PRIMARY_RUNTIME_ID }),
      expect.objectContaining({ value: 'model-b --provider openrouter --session', session_id: PRIMARY_RUNTIME_ID }),
      expect.objectContaining({ value: `model-a --provider ${provider} --session`, session_id: PRIMARY_RUNTIME_ID })
    ])
  })
})
