import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'

import { applyModelPreset } from './model-presets'
import {
  $activeSessionId,
  $currentFastMode,
  $currentReasoningEffort,
  $selectedStoredSessionId,
  setCurrentFastMode,
  setCurrentReasoningEffort
} from './session'
import { $sessionStates, publishSessionState, type SessionTileDelegate, setSessionTileDelegate } from './session-states'

beforeEach(() => {
  $activeSessionId.set('live')
  $selectedStoredSessionId.set('stored')
  setCurrentReasoningEffort('low')
  setCurrentFastMode(false)
  publishSessionState('live', { ...createClientSessionState('stored'), reasoningEffort: 'low', fast: false })
  setSessionTileDelegate({
    updateSession: (id, fn) => {
      const next = fn($sessionStates.get()[id])
      publishSessionState(id, next)

      return next
    }
  } as SessionTileDelegate)
})
afterEach(() => {
  $sessionStates.set({})
  $activeSessionId.set(null)
  $selectedStoredSessionId.set(null)
  setSessionTileDelegate({} as SessionTileDelegate)
})
it('retains a committed legacy effort when the independent Fast write is rejected', async () => {
  const backend = { effort: 'low', fast: false }

  const request = vi.fn(async (_method, params) => {
    if (params.key === 'runtime_options') {
      throw Object.assign(new Error('unknown config key: runtime_options'), { code: 4002 })
    }

    if (params.key === 'reasoning') {
      backend.effort = params.value

      return {}
    }

    throw Object.assign(new Error('fast mode is not available for this model'), { code: 4002 })
  })

  await applyModelPreset(
    { effort: 'high', fast: true },
    { sessionId: 'live', request: request as never, failMessage: 'Failed' }
  )
  expect($sessionStates.get().live.reasoningEffort).toBe(backend.effort)
  expect($currentReasoningEffort.get()).toBe(backend.effort)
  expect($sessionStates.get().live.fast).toBe(backend.fast)
  expect($currentFastMode.get()).toBe(backend.fast)
  expect($sessionStates.get().live.pendingRuntimeOptions).toBeNull()
  expect(request.mock.calls.map(([, p]) => p.key)).toEqual(['runtime_options', 'reasoning', 'fast'])
})

it('keeps an accepted deferred legacy effort pending when Fast is rejected', async () => {
  const request = vi.fn(async (_method, params) => {
    if (params.key === 'runtime_options') {
      throw Object.assign(new Error('unknown config key: runtime_options'), { code: 4002 })
    }

    if (params.key === 'reasoning') {
      return { deferred: true }
    }

    throw Object.assign(new Error('fast mode is not available for this model'), { code: 4002 })
  })

  await applyModelPreset(
    { effort: 'high', fast: true },
    { sessionId: 'live', request: request as never, failMessage: 'Failed' }
  )
  expect($sessionStates.get().live.reasoningEffort).toBe('high')
  expect($sessionStates.get().live.fast).toBe(false)
  expect($sessionStates.get().live.pendingRuntimeOptions).toEqual({ effort: 'high' })
})

it('rolls back both dimensions when the first legacy write is rejected', async () => {
  const request = vi.fn(async (_method, params) => {
    if (params.key === 'runtime_options') {
      throw Object.assign(new Error('unknown config key: runtime_options'), { code: 4002 })
    }

    throw Object.assign(new Error('reasoning not supported'), { code: 4002 })
  })

  await applyModelPreset(
    { effort: 'high', fast: true },
    { sessionId: 'live', request: request as never, failMessage: 'Failed' }
  )
  expect($sessionStates.get().live.reasoningEffort).toBe('low')
  expect($sessionStates.get().live.fast).toBe(false)
  expect($sessionStates.get().live.pendingRuntimeOptions).toBeNull()
  expect(request.mock.calls.map(([, p]) => p.key)).toEqual(['runtime_options', 'reasoning'])
})
