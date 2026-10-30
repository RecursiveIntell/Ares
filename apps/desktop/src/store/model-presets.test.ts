import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'

import {
  $modelPresets,
  applyModelPreset,
  getModelPreset,
  modelPresetKey,
  modelPresetScopeKey,
  runtimeOptionsTargetForSession,
  setModelPreset
} from './model-presets'
import {
  $activeSessionId,
  $currentFastMode,
  $currentReasoningEffort,
  getComposerRuntimeOptions,
  setCurrentFastMode,
  setCurrentReasoningEffort
} from './session'
import { $sessionStates } from './session-states'

type GatewayRequest = <T>(method: string, params?: Record<string, unknown>) => Promise<T>

const sessionScope = (sessionId: string) =>
  modelPresetScopeKey({ connectionId: 'source-a', draftId: 'draft', profile: 'worker', sessionId })

describe('session-scoped model presets', () => {
  beforeEach(() => {
    $modelPresets.set({})
    $sessionStates.set({})
    $activeSessionId.set(null)
    setCurrentFastMode(false)
    setCurrentReasoningEffort('')
  })

  it('keys a remembered target choice by connection, profile, and session', () => {
    const sessionA = sessionScope('stored-a')
    const sessionB = sessionScope('stored-b')

    setModelPreset(sessionA, 'anthropic', 'claude-opus-4-8', { effort: 'high', fast: true })
    setModelPreset(sessionB, 'anthropic', 'claude-opus-4-8', { effort: 'low', fast: false })

    expect(modelPresetKey(sessionA, 'anthropic', 'claude-opus-4-8')).not.toBe(
      modelPresetKey(sessionB, 'anthropic', 'claude-opus-4-8')
    )
    expect(getModelPreset(sessionA, 'anthropic', 'claude-opus-4-8')).toEqual({ effort: 'high', fast: true })
    expect(getModelPreset(sessionB, 'anthropic', 'claude-opus-4-8')).toEqual({ effort: 'low', fast: false })
    expect(modelPresetKey(sessionA, 'provider::model', 'item')).not.toBe(
      modelPresetKey(sessionA, 'provider', 'model::item')
    )
    expect(
      modelPresetScopeKey({ connectionId: 'source-b', draftId: 'draft', profile: 'worker', sessionId: 'stored-a' })
    ).not.toBe(sessionA)
    expect(
      modelPresetScopeKey({ connectionId: 'source-a', draftId: 'draft', profile: 'other', sessionId: 'stored-a' })
    ).not.toBe(sessionA)
  })

  it('keeps an option intent bound to the durable stored session across runtime rebinds', () => {
    const state = createClientSessionState('stored-session')
    $sessionStates.set({ 'runtime-old': state, 'runtime-new': { ...state } })

    expect(runtimeOptionsTargetForSession('runtime-old')).toBe(runtimeOptionsTargetForSession('runtime-new'))
  })

  it('merges option changes without dropping the other dimension for one session/model', () => {
    const sessionA = sessionScope('stored-a')
    setModelPreset(sessionA, 'anthropic', 'claude-opus-4-8', { effort: 'high' })
    setModelPreset(sessionA, 'anthropic', 'claude-opus-4-8', { fast: true })

    expect(getModelPreset(sessionA, 'anthropic', 'claude-opus-4-8')).toEqual({ effort: 'high', fast: true })
  })

  it('does not erase an existing target dimension when applying a partial preset', () => {
    const sessionA = sessionScope('stored-a')
    setModelPreset(sessionA, 'anthropic', 'claude-opus-4-8', { effort: 'high', fast: true })
    setModelPreset(sessionA, 'anthropic', 'claude-opus-4-8', { effort: undefined })

    expect(getModelPreset(sessionA, 'anthropic', 'claude-opus-4-8')).toEqual({ effort: 'high', fast: true })
  })

  it('pushes only the supplied options through the combined session RPC', async () => {
    const calls: { method: string; params?: Record<string, unknown> }[] = []

    const request = async <T>(method: string, params?: Record<string, unknown>) => {
      calls.push({ method, params })

      return {} as T
    }

    await applyModelPreset({ effort: 'high' }, { failMessage: 'x', request, sessionId: 's1' })
    await applyModelPreset({}, { failMessage: 'x', request, sessionId: 's1' })

    expect(calls).toEqual([
      { method: 'config.set', params: { key: 'runtime_options', session_id: 's1', value: { effort: 'high' } } }
    ])
  })

  it('does not let a live primary session overwrite the saved new-chat option preferences', async () => {
    $activeSessionId.set('runtime-a')
    setCurrentReasoningEffort('low')
    setCurrentFastMode(false)

    const request = vi.fn(async <T>(_method: string, _params?: Record<string, unknown>) => ({}) as T)
    await applyModelPreset(
      { effort: 'high', fast: true },
      { failMessage: 'x', primary: true, request: request as unknown as GatewayRequest, sessionId: 'runtime-a' }
    )

    // The live SessionView may paint the optimistic values, but draft
    // preferences are separate and must remain unchanged for other sessions.
    expect(getComposerRuntimeOptions()).toEqual({ effort: 'low', fast: false })
  })

  it('applies a fresh-draft preset locally without mutating backend config', async () => {
    const calls: { method: string; params?: Record<string, unknown> }[] = []

    const request = async <T>(method: string, params?: Record<string, unknown>) => {
      calls.push({ method, params })

      return {} as T
    }

    await applyModelPreset({ effort: 'high', fast: true }, { failMessage: 'x', request, sessionId: null })

    expect($currentReasoningEffort.get()).toBe('high')
    expect($currentFastMode.get()).toBe(true)
    expect(calls).toEqual([])
  })

  it('a delayed earlier Fast failure cannot roll back a newer session intent', async () => {
    $activeSessionId.set('runtime-a')
    setCurrentFastMode(false)

    let rejectFirst!: (error: Error) => void

    const first = new Promise<unknown>((_, reject) => {
      rejectFirst = reject
    })

    const request = vi
      .fn(async <T>(_method: string, _params?: Record<string, unknown>) => ({}) as T)
      .mockImplementationOnce(<T>() => first as Promise<T>)
      .mockResolvedValueOnce({} as never)

    const stale = applyModelPreset(
      { fast: true },
      { failMessage: 'x', primary: true, request: request as unknown as GatewayRequest, sessionId: 'runtime-a' }
    )

    const latest = applyModelPreset(
      { fast: false },
      { failMessage: 'x', primary: true, request: request as unknown as GatewayRequest, sessionId: 'runtime-a' }
    )

    await Promise.resolve()
    expect(request).toHaveBeenCalledTimes(2)
    expect(request).toHaveBeenNthCalledWith(1, 'config.set', {
      key: 'runtime_options',
      session_id: 'runtime-a',
      value: { fast: true }
    })
    expect(request).toHaveBeenNthCalledWith(2, 'config.set', {
      key: 'runtime_options',
      session_id: 'runtime-a',
      value: { fast: false }
    })

    await latest
    rejectFirst(new Error('delayed failure'))
    await stale

    expect($currentFastMode.get()).toBe(false)
  })
})
