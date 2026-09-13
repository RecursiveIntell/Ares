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
  setCurrentModel,
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
})
