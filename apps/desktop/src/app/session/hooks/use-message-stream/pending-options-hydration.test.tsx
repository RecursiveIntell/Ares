import { act, cleanup } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { storedString } from '@/lib/storage'
import {
  $currentFastMode,
  $currentReasoningEffort,
  setCurrentFastMode,
  setCurrentReasoningEffort
} from '@/store/session'

import { renderMessageStream } from './test-harness'

afterEach(cleanup)
it.each(['live', 'background'])(
  'hydrates backend pending intent into a fresh %s cache without persisting draft preferences',
  id => {
    setCurrentReasoningEffort('medium')
    setCurrentFastMode(false)
    const draftEffort = storedString('hermes.desktop.composer.reasoning-effort')
    const draftFast = storedString('hermes.desktop.composer.fast')
    const stream = renderMessageStream('live')
    act(() =>
      stream.handleEvent({
        type: 'session.info',
        session_id: id,
        payload: {
          reasoning_effort: 'low',
          fast: false,
          pending_runtime_options: { effort: 'high', fast: true }
        }
      })
    )
    expect(stream.state(id).pendingRuntimeOptions).toEqual({ effort: 'high', fast: true })
    expect(stream.state(id).reasoningEffort).toBe('high')
    expect(stream.state(id).fast).toBe(true)
    expect(storedString('hermes.desktop.composer.reasoning-effort')).toBe(draftEffort)
    expect(storedString('hermes.desktop.composer.fast')).toBe(draftFast)

    if (id === 'live') {
      expect($currentReasoningEffort.get()).toBe('high')
      expect($currentFastMode.get()).toBe(true)
    } else {
      expect($currentReasoningEffort.get()).toBe('medium')
      expect($currentFastMode.get()).toBe(false)
    }

    act(() =>
      stream.handleEvent({
        type: 'session.info',
        session_id: id,
        payload: {
          reasoning_effort: 'high',
          fast: true,
          pending_runtime_options: null
        }
      })
    )
    expect(stream.state(id).pendingRuntimeOptions).toBeNull()
  }
)

it('does not replace a newer local option intent with an older backend heartbeat', () => {
  const stream = renderMessageStream('live')
  stream.states.set('live', {
    ...stream.state('live'),
    pendingRuntimeOptions: { effort: 'max', fast: false },
    reasoningEffort: 'max',
    fast: false
  })
  act(() =>
    stream.handleEvent({
      type: 'session.info',
      session_id: 'live',
      payload: {
        reasoning_effort: 'low',
        fast: true,
        pending_runtime_options: { effort: 'high', fast: true }
      }
    })
  )
  expect(stream.state('live').pendingRuntimeOptions).toEqual({ effort: 'max', fast: false })
  expect(stream.state('live').reasoningEffort).toBe('max')
  expect(stream.state('live').fast).toBe(false)
})

it('does not hydrate option intent from a retired runtime', () => {
  const stream = renderMessageStream('live')
  stream.states.set('live', { ...stream.state('live'), reconnecting: true })
  act(() =>
    stream.handleEvent({
      type: 'session.info',
      session_id: 'live',
      payload: {
        reasoning_effort: 'low',
        fast: false,
        pending_runtime_options: { effort: 'high', fast: true }
      }
    })
  )
  expect(stream.state('live').pendingRuntimeOptions).toBeFalsy()
})
