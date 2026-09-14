import { beforeEach, describe, expect, it } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'

import { $modelPresets, applyModelPreset, getModelPreset, modelPresetKey, setModelPreset } from './model-presets'
import { $currentFastMode, $currentReasoningEffort, setCurrentFastMode, setCurrentReasoningEffort } from './session'
import { type SessionTileDelegate, setSessionTileDelegate } from './session-states'

describe('model presets', () => {
  beforeEach(() => {
    $modelPresets.set({})
    setCurrentFastMode(false)
    setCurrentReasoningEffort('')
    setSessionTileDelegate({} as never)
  })

  it('round-trips a preset and merges patches without dropping prior fields', () => {
    setModelPreset('anthropic', 'claude-opus-4-8', { effort: 'high' })
    setModelPreset('anthropic', 'claude-opus-4-8', { fast: true })

    expect(getModelPreset('anthropic', 'claude-opus-4-8')).toEqual({ effort: 'high', fast: true })
  })

  it('returns an empty preset for unknown models', () => {
    expect(getModelPreset('x', 'y')).toEqual({})
  })

  it('keys by provider::model', () => {
    expect(modelPresetKey('openai', 'gpt-5.5')).toBe('openai::gpt-5.5')
  })

  it('pushes only the provided dimensions to the gateway', async () => {
    const calls: { method: string; params?: Record<string, unknown> }[] = []

    const request = async <T>(method: string, params?: Record<string, unknown>) => {
      calls.push({ method, params })

      return {} as T
    }

    await applyModelPreset({ effort: 'high' }, { failMessage: 'x', request, sessionId: 's1' })
    await applyModelPreset({}, { failMessage: 'x', request, sessionId: 's1' })

    expect(calls).toHaveLength(1)
    expect(calls[0]).toMatchObject({
      method: 'session.runtime.configure',
      params: {
        reasoning: { effort: 'high', mode: 'effort' },
        session_id: 's1'
      }
    })
  })

  it('applies a fresh-draft preset locally without mutating gateway config', async () => {
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

  it('updates the primary runtime slice when applying a live-session preset', async () => {
    let state = createClientSessionState('stored-1')
    setSessionTileDelegate({
      updateSession: (_runtimeId: string, updater: Parameters<SessionTileDelegate['updateSession']>[1]) => {
        state = updater(state)
        return state
      }
    } as SessionTileDelegate)

    await applyModelPreset(
      { effort: 'high', fast: true },
      { failMessage: 'x', primary: true, request: async <T>() => ({} as T), sessionId: 'runtime-1' }
    )

    expect(state.reasoningEffort).toBe('high')
    expect(state.fast).toBe(true)
  })
})
