import { QueryClient } from '@tanstack/react-query'
import { cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from '@/api/client'
import { getGlobalModelInfo } from '@/hermes'
import { createClientSessionState } from '@/lib/chat-runtime'
import { modelOptionsQueryKey } from '@/lib/model-options'
import { $activeGatewayProfile, $newChatConnectionId, $newChatProfile, $newChatRoute } from '@/store/profile'
import {
  $activeSessionId,
  $currentModel,
  $selectedStoredSessionId,
  _resetComposerModelSelectionsForTests,
  captureComposerModelSelection,
  type ComposerModelOwner,
  getComposerModelSelection,
  recordComposerModelSelection,
  setComposerModelSelectionOwner,
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

import { deferred } from '../../../test/deferred'

import { beginMainModelSave, captureModelRequestOwner } from './composer-model-selection-owner'
import { useModelControls } from './use-model-controls'

const scope = vi.hoisted(() => ({
  liveOwner: null as null | { connectionId: string; profile: string; targetProfile?: string }
}))

vi.mock('@/hermes', () => ({ getGlobalModelInfo: vi.fn(), setApiRequestProfile: vi.fn() }))
vi.mock('@/i18n', () => ({
  useI18n: () => ({ t: { common: { confirm: 'Confirm' }, desktop: { modelSwitchFailed: 'failed' } } })
}))
vi.mock('@/store/notifications', () => ({ notify: vi.fn(), notifyError: vi.fn() }))
vi.mock('@/store/session-states', async importOriginal => ({
  ...(await importOriginal<object>()),
  knownOwnerForSession: (id: string | null) => (id ? scope.liveOwner : undefined)
}))

const local = { connectionId: null, profile: 'default' } satisfies ComposerModelOwner
const a = { connectionId: 'source-a', profile: 'specialist', targetProfile: 'backend-a' }
const b = { connectionId: 'source-b', profile: 'specialist', targetProfile: 'backend-b' }

function activate(owner: ComposerModelOwner) {
  $newChatRoute.set(owner.connectionId ? { ...owner, connectionId: owner.connectionId } : null)
  $newChatProfile.set(owner.profile)
  $newChatConnectionId.set(owner.connectionId)
  $activeGatewayProfile.set(owner.profile)
  setApiRequestProfile(owner.profile)
  setApiRequestConnection(owner.connectionId)
}

function seed(owner: ComposerModelOwner, model = 'model-a', source: 'manual' | 'default' = 'default') {
  setComposerModelSelectionOwner(owner)

  return recordComposerModelSelection(captureComposerModelSelection(owner), { model, provider: 'provider-a', source })!
}

function controls(requestGateway = vi.fn(async () => ({}) as never), queryClient = new QueryClient()) {
  return renderHook(() => useModelControls({ queryClient, requestGateway })).result
}

function runtime(owner = a) {
  activate(owner)
  scope.liveOwner = owner
  $activeSessionId.set('runtime-owned')
  $selectedStoredSessionId.set('stored-owned')
  setCurrentModel('model-a')
  setCurrentProvider('provider-a')
  setCurrentModelSource('default')
  publishSessionState('runtime-owned', {
    ...createClientSessionState('stored-owned'),
    model: 'model-a',
    provider: 'provider-a'
  })
  setSessionTileDelegate({
    updateSession: (id, updater) => {
      const next = updater($sessionStates.get()[id])
      publishSessionState(id, next)

      return next
    }
  } as SessionTileDelegate)
}

beforeEach(() => {
  _resetComposerModelSelectionsForTests()
  scope.liveOwner = null
  $sessionStates.set({})
  $activeSessionId.set(null)
  $selectedStoredSessionId.set(null)
  setCurrentModel('')
  setCurrentProvider('')
  setCurrentModelSource('')
  activate(local)
  setComposerModelSelectionOwner(local)
  vi.mocked(getGlobalModelInfo).mockReset()
})
afterEach(() => {
  cleanup()
  setSessionTileDelegate({} as SessionTileDelegate)
  $activeSessionId.set(null)
})

describe('real core owner receipts produced by controls', () => {
  it.each(['openrouter', 'anthropic', 'openai-codex', 'moa', 'ollama-cloud', 'ollama-launch', 'custom:target', 'auto'])(
    'records A→B→A deliberate %s selections without global writes',
    async provider => {
      const request = vi.fn(async () => ({}) as never)
      const c = controls(request)

      for (const model of ['model-a', 'model-b', 'model-a']) {
        await c.current.selectModel({ model, provider })
        expect(getComposerModelSelection(local)).toMatchObject({ model, provider, source: 'manual' })
      }

      expect(request).not.toHaveBeenCalled()
    }
  )

  it('records a pending B draft under B rather than relabeling ambient A', async () => {
    activate(a)
    seed(a, 'a-pin', 'manual')
    setCurrentModel('a-pin')
    setCurrentProvider('provider-a')
    $newChatRoute.set(b)
    $newChatProfile.set(b.profile)
    $newChatConnectionId.set(b.connectionId)
    const client = new QueryClient()
    client.setQueryData(modelOptionsQueryKey(a.targetProfile, null, a.connectionId), {
      model: 'a-pin',
      provider: 'provider-a',
      providers: []
    })
    await controls(undefined, client).current.selectModel({ model: 'b-pin', provider: 'ollama-launch' })
    expect(client.getQueryData(modelOptionsQueryKey(a.targetProfile, null, a.connectionId))).toMatchObject({
      model: 'a-pin'
    })
    expect(client.getQueryData(modelOptionsQueryKey(b.targetProfile, null, b.connectionId))).toMatchObject({
      model: 'b-pin'
    })
    expect(getComposerModelSelection(a)?.model).toBe('a-pin')
    expect(getComposerModelSelection(b)).toMatchObject({ owner: b, model: 'b-pin', source: 'manual' })
  })

  it('restores owner-qualified manual pins across source A→B→A including force', async () => {
    const c = controls()
    activate(a)
    await c.current.selectModel({ model: 'a-pin', provider: 'ollama-launch' })
    activate(b)
    await c.current.selectModel({ model: 'b-pin', provider: 'custom:target' })
    activate(a)
    await c.current.refreshCurrentModel(true)
    expect($currentModel.get()).toBe('a-pin')
    activate(b)
    await c.current.refreshCurrentModel(true)
    expect($currentModel.get()).toBe('b-pin')
    expect(getGlobalModelInfo).not.toHaveBeenCalled()
  })

  it('does not fetch or relabel A while an unknown B draft waits for activation', async () => {
    activate(a)
    setCurrentModel('ambient-a')
    setCurrentProvider('provider-a')
    $newChatRoute.set(b)
    $newChatProfile.set(b.profile)
    $newChatConnectionId.set(b.connectionId)
    await controls().current.refreshCurrentModel(true)
    expect(getGlobalModelInfo).not.toHaveBeenCalled()
    expect($currentModel.get()).toBe('')
    expect(getComposerModelSelection(b)).toBeNull()
  })

  it('captures a logical profile and backend target for the actual default request', async () => {
    activate(a)
    vi.mocked(getGlobalModelInfo).mockResolvedValue({ model: 'target-a-default', provider: 'ollama-launch' })
    expect(captureModelRequestOwner()).toEqual(a)
    await controls().current.refreshCurrentModel(true)
    expect(getGlobalModelInfo).toHaveBeenCalledWith('backend-a')
    expect(getComposerModelSelection(a)).toMatchObject({ owner: a, model: 'target-a-default', source: 'default' })
    expect(getComposerModelSelection({ ...a, targetProfile: 'backend-b' })).toBeNull()
  })

  it('rejects a late default after a deliberate equal-value manual pick', async () => {
    const old = deferred<{ model: string; provider: string }>()
    vi.mocked(getGlobalModelInfo).mockReturnValue(old.promise)
    const c = controls()
    const pending = c.current.refreshCurrentModel()
    await c.current.selectModel({ model: 'same-model', provider: 'ollama-launch' })
    const manual = getComposerModelSelection(local)
    old.resolve({ model: 'same-model', provider: 'ollama-launch' })
    await pending
    expect(getComposerModelSelection(local)).toBe(manual)
    expect(manual?.source).toBe('manual')
  })

  it('latest confirmed Settings save wins a weaker default read and rejects the old reply', async () => {
    const old = deferred<{ model: string; provider: string }>()
    vi.mocked(getGlobalModelInfo).mockReturnValue(old.promise)
    const c = controls()
    const pending = c.current.refreshCurrentModel()
    const origin = beginMainModelSave()
    recordComposerModelSelection(captureComposerModelSelection(local), {
      model: 'weaker-read',
      provider: 'openrouter',
      source: 'default'
    })
    c.current.applySavedMainModel({ ...origin, model: 'saved-b', provider: 'ollama-launch' })
    old.resolve({ model: 'stale-a', provider: 'openrouter' })
    await pending
    expect(getComposerModelSelection(local)).toMatchObject({ model: 'saved-b', source: 'default' })
    expect($currentModel.get()).toBe('saved-b')
  })

  it('preserves a later manual pin against an equal-value Settings save', async () => {
    const c = controls()
    const origin = beginMainModelSave()
    await c.current.selectModel({ model: 'same-model', provider: 'ollama-launch' })
    const pin = getComposerModelSelection(local)
    c.current.applySavedMainModel({ ...origin, model: 'same-model', provider: 'ollama-launch' })
    expect(getComposerModelSelection(local)).toBe(pin)
    expect(pin?.source).toBe('manual')
  })

  it('ignores older Settings callbacks that lost the shared save token', () => {
    const c = controls()
    const first = beginMainModelSave()
    const latest = beginMainModelSave()
    c.current.applySavedMainModel({ ...latest, model: 'saved-latest', provider: 'ollama-launch' })
    const accepted = getComposerModelSelection(local)
    c.current.applySavedMainModel({ ...first, model: 'saved-old', provider: 'openrouter' })
    expect(getComposerModelSelection(local)).toBe(accepted)
    expect($currentModel.get()).toBe('saved-latest')
  })

  it('a background save updates only its captured source/target cache', async () => {
    activate(a)
    const origin = beginMainModelSave()
    const client = new QueryClient()
    const c = controls(undefined, client)
    activate(b)
    await c.current.selectModel({ model: 'b-pin', provider: 'custom:target' })
    c.current.applySavedMainModel({ ...origin, model: 'a-saved', provider: 'ollama-launch' })
    expect($currentModel.get()).toBe('b-pin')
    expect(getComposerModelSelection(a)).toBeNull()
    expect(client.getQueryData(modelOptionsQueryKey(a.targetProfile, null, a.connectionId))).toMatchObject({
      model: 'a-saved'
    })
    expect(getComposerModelSelection(b)?.source).toBe('manual')
  })

  it('old A default remains rejected after source A→B→A', async () => {
    activate(a)
    const old = deferred<{ model: string; provider: string }>()
    vi.mocked(getGlobalModelInfo).mockReturnValueOnce(old.promise)
    const c = controls()
    const pending = c.current.refreshCurrentModel(true)
    activate(b)
    vi.mocked(getGlobalModelInfo).mockResolvedValueOnce({ model: 'b-default', provider: 'ollama-launch' })
    await c.current.refreshCurrentModel(true)
    activate(a)
    vi.mocked(getGlobalModelInfo).mockResolvedValueOnce({ model: 'new-a', provider: 'openrouter' })
    await c.current.refreshCurrentModel(true)
    old.resolve({ model: 'old-a', provider: 'openrouter' })
    await pending
    expect(getComposerModelSelection(a)?.model).toBe('new-a')
  })

  it.each([true, false])(
    'restores the exact prior receipt or absence after rejection (has baseline=%s)',
    async exists => {
      activate(a)
      const previous = exists ? seed(a) : null
      runtime()

      const c = controls(
        vi.fn(async () => {
          throw new Error('rejected')
        })
      )

      await expect(c.current.selectModel({ model: 'rejected-model', provider: 'ollama-launch' })).resolves.toBe(false)
      expect(getComposerModelSelection(a)?.model ?? null).toBe(previous?.model ?? null)
      expect($sessionStates.get()['runtime-owned'].model).toBe('model-a')
    }
  )

  it('earlier acknowledgement advances the exact rollback receipt before the newer rejection', async () => {
    activate(a)
    seed(a)
    runtime()
    const first = deferred<object>()
    const second = deferred<object>()
    const request = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const c = controls(request)
    const oldPick = c.current.selectModel({ model: 'model-b', provider: 'provider-b' })
    const newerPick = c.current.selectModel({ model: 'model-c', provider: 'provider-c' })
    first.resolve({})
    await oldPick
    second.reject(new Error('newer rejected'))
    await newerPick
    expect($sessionStates.get()['runtime-owned'].model).toBe('model-b')
    expect(getComposerModelSelection(a)).toMatchObject({ model: 'model-b', provider: 'provider-b', source: 'manual' })
  })

  it('late older success preserves runtime handling without republishing an unproved draft stamp', async () => {
    activate(a)
    seed(a)
    runtime()
    const first = deferred<object>()
    const second = deferred<object>()
    const request = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const c = controls(request)
    const oldPick = c.current.selectModel({ model: 'model-b', provider: 'provider-b' })
    const newerPick = c.current.selectModel({ model: 'model-c', provider: 'provider-c' })
    second.reject(new Error('newer rejected'))
    await newerPick
    const restored = getComposerModelSelection(a)
    first.resolve({})
    await oldPick
    expect($sessionStates.get()['runtime-owned'].model).toBe('model-b')
    expect(getComposerModelSelection(a)).toBe(restored)
    expect(restored?.model).toBe('model-a')
  })
})

it('keeps B’s healthy default read when a background A save completes', async () => {
  activate(a)
  const origin = beginMainModelSave()
  const c = controls()
  activate(b)
  const pending = deferred<{ model: string; provider: string }>()
  vi.mocked(getGlobalModelInfo).mockReturnValueOnce(pending.promise)
  const waiting = c.current.refreshCurrentModel(true)
  c.current.applySavedMainModel({ ...origin, model: 'saved-a', provider: 'ollama-launch' })
  pending.resolve({ model: 'healthy-b', provider: 'custom:b' })
  await waiting
  expect(getComposerModelSelection(b)).toMatchObject({ model: 'healthy-b', provider: 'custom:b' })
  expect($currentModel.get()).toBe('healthy-b')
})
