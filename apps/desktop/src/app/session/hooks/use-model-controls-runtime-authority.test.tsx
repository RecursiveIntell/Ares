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

  it('retains every superseded pair through rapid optimistic switches', async () => {
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
    const third = result.current.selectModel({ model: 'model-d', provider: 'provider-d' })
    expect($sessionStates.get()[PRIMARY_RUNTIME_ID].pendingModelSelection).toMatchObject({
      model: 'model-d',
      provider: 'provider-d',
      supersededSelections: [
        { model: 'model-a', provider: 'provider-a' },
        { model: 'model-b', provider: 'provider-b' },
        { model: 'model-c', provider: 'provider-c' }
      ]
    })

    for (const [index, pick] of [first, second, third].entries()) {
      requests[index]({ key: 'model', scope: 'session', value: `model-${['b', 'c', 'd'][index]}` })
      await expect(pick).resolves.toBe(true)
      expect(PRIMARY_SESSION_VIEW.$model.get()).toBe('model-d')
    }
  })

  it('rolls the latest failed request back to the highest confirmed earlier revision', async () => {
    setCurrentModelSource('default')

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
    const third = result.current.selectModel({ model: 'model-d', provider: 'provider-d' })

    expect(PRIMARY_SESSION_VIEW.$model.get()).toBe('model-d')
    expect($sessionStates.get()[PRIMARY_RUNTIME_ID].pendingModelSelection).toMatchObject({
      model: 'model-d',
      provider: 'provider-d',
      rollbackModel: 'model-a',
      rollbackProvider: 'provider-a'
    })

    requests[0].resolve({ key: 'model', scope: 'session', value: 'model-b', model_control_revision: 1 })
    await expect(first).resolves.toBe(true)
    expect(PRIMARY_SESSION_VIEW.$model.get()).toBe('model-d')
    expect($sessionStates.get()[PRIMARY_RUNTIME_ID].pendingModelSelection).toMatchObject({
      model: 'model-d',
      provider: 'provider-d',
      rollbackModel: 'model-b',
      rollbackProvider: 'provider-b'
    })

    requests[1].reject(new Error('C rejected'))
    await expect(second).resolves.toBe(false)
    expect(PRIMARY_SESSION_VIEW.$model.get()).toBe('model-d')

    requests[2].reject(new Error('D rejected'))
    await expect(third).resolves.toBe(false)
    expect(PRIMARY_SESSION_VIEW.$model.get()).toBe('model-b')
    expect(PRIMARY_SESSION_VIEW.$provider.get()).toBe('provider-b')
    expect($sessionStates.get()[PRIMARY_RUNTIME_ID]).toMatchObject({
      model: 'model-b',
      provider: 'provider-b',
      pendingModelSelection: null
    })
    expect($currentModel.get()).toBe('model-b')
    expect($currentProvider.get()).toBe('provider-b')
    expect(getCurrentModelSource()).toBe('manual')
  })

  it('keeps the highest confirmed choice when older acknowledgements arrive last', async () => {
    const requests: Array<{ resolve: (value: unknown) => void; reject: (reason: Error) => void }> = []

    const requestGateway = vi.fn(
      () =>
        new Promise<never>((resolve, reject) => {
          requests.push({ resolve: value => resolve(value as never), reject })
        })
    )

    const { result } = renderHook(() => useModelControls({ queryClient: new QueryClient(), requestGateway }))
    const first = result.current.selectModel({ model: 'model-b', provider: 'provider-b' })
    const second = result.current.selectModel({ model: 'model-c', provider: 'provider-c' })
    const third = result.current.selectModel({ model: 'model-d', provider: 'provider-d' })
    requests[1].resolve({ model_control_revision: 2 })
    await second
    requests[0].resolve({ model_control_revision: 1 })
    await first
    expect(PRIMARY_SESSION_VIEW.$model.get()).toBe('model-d')
    requests[2].reject(new Error('D refused'))
    await expect(third).resolves.toBe(false)
    expect(PRIMARY_SESSION_VIEW.$model.get()).toBe('model-c')
    expect($sessionStates.get()[PRIMARY_RUNTIME_ID]).toMatchObject({
      model: 'model-c',
      provider: 'provider-c',
      pendingModelSelection: null,
      modelSelectionFence: { model: 'model-c', provider: 'provider-c', modelControlRevision: 2 }
    })
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
})
