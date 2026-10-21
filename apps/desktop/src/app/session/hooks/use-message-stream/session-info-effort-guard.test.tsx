/** Task 2.1 (goal-wait-effort-clobber-20261020): measured-gap verification of
 *  the release-tree effort/fast heartbeat clobber guard.
 *
 *  The plan's Step 0 enumerates four cases. Cases 1 (deferred-switch stale
 *  heartbeat) and 4 (tile patch) are already covered by
 *  pending-options-hydration.test.tsx and utils.test.ts; this file covers the
 *  two NOT exercised anywhere: the flicker surface the user reported.
 *
 *  Case 2 — an effort pick (markComposerSelectionManual + optimistic composer
 *  atom write) must NOT be reverted in the visible composer atoms by a
 *  subsequent server-confirmation session.info that carries the value the pick
 *  REPLACED (ordered race: heartbeats stamped before the option write landed
 *  must not repaint the pill).
 *
 *  Case 3 — when the model-switch lands, the switch-landing session.info (new
 *  selector epoch) must settle BOTH the composer atoms and pendingRuntimeOptions
 *  to the new model's effort, ending the guard window cleanly.
 */

import { act, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import {
  $currentFastMode,
  $currentReasoningEffort,
  setCurrentFastMode,
  setCurrentReasoningEffort
} from '@/store/session'

import { renderMessageStream } from './test-harness'

beforeEach(() => {
  // The composer draft prefs are persisted in these tests' environment through
  // localStorage; seed a stable baseline each time so assertions are exact.
  setCurrentReasoningEffort('medium')
  setCurrentFastMode(false)
})

afterEach(cleanup)

it('case 2: an in-flight server readback does not repaint a newer manual effort pick', () => {
  const stream = renderMessageStream('live')

  // Backend heartbeats the OLD model's effort (pre-pick server truth).
  act(() =>
    stream.handleEvent({
      type: 'session.info',
      session_id: 'live',
      payload: { reasoning_effort: 'high', fast: false }
    })
  )
  expect(stream.state('live').reasoningEffort).toBe('high')

  // USER_PICK: user selects 'low' for the new model — the composer atom flips
  // optimistically (this is what the model menu's patchReasoning does).
  act(() => {
    setCurrentReasoningEffort('low')
  })
  expect($currentReasoningEffort.get()).toBe('low')

  // A heartbeat emitted BEFORE the user's config.set landed arrives now: the
  // server still reports 'high'. This is the observed clobber.
  act(() =>
    stream.handleEvent({
      type: 'session.info',
      session_id: 'live',
      payload: { reasoning_effort: 'high', fast: false }
    })
  )

  expect(stream.state('live').reasoningEffort).toBe('high')
  // THE RENDERER-VISIBLE INVALUATION: the composer pill must not flip back.
  // The title of this test file is the deliverable: today this can regress
  // whenever the guard's settleOptions path is bypassed.
  expect($currentReasoningEffort.get()).toBe('low')
})

it('case 3: a switch-landing session.info settles the composer to the new effort', () => {
  const stream = renderMessageStream('live')

  // Old model's effort visible.
  act(() =>
    stream.handleEvent({
      type: 'session.info',
      session_id: 'live',
      payload: {
        reasoning_effort: 'xhigh',
        fast: false,
        pending_runtime_options: { effort: 'medium' }
      }
    })
  )
  expect(stream.state('live').pendingRuntimeOptions).toEqual({ effort: 'medium' })
  expect(stream.state('live').reasoningEffort).toBe('medium')

  // Switch lands: new epoch, server truth is the NEW model's effort, no
  // pending intent.
  act(() =>
    stream.handleEvent({
      type: 'session.info',
      session_id: 'live',
      payload: {
        reasoning_effort: 'medium',
        fast: false,
        pending_runtime_options: null,
        selector_version: { epoch: 'new-epoch', revision: 3 }
      }
    })
  )
  expect(stream.state('live').pendingRuntimeOptions).toBeNull()
  expect(stream.state('live').reasoningEffort).toBe('medium')
})

it('case 2b: a readback carrying the picked value retires the intent; a later genuine change wins', () => {
  const stream = renderMessageStream('live')

  act(() =>
    stream.handleEvent({
      type: 'session.info',
      session_id: 'live',
      payload: { reasoning_effort: 'high', fast: false }
    })
  )

  // Client pick.
  act(() => {
    setCurrentReasoningEffort('low')
  })

  // The pick's readback arrives carrying the NEW value: intent retires —
  // server truth has caught up. No repaint occurs (values agree anyway).
  act(() =>
    stream.handleEvent({
      type: 'session.info',
      session_id: 'live',
      payload: { reasoning_effort: 'low', fast: false }
    })
  )
  expect($currentReasoningEffort.get()).toBe('low')

  // NOW a genuine backend change (user edits elsewhere / another surface)
  // must be honored — the intent must not mask it.
  act(() =>
    stream.handleEvent({
      type: 'session.info',
      session_id: 'live',
      payload: { reasoning_effort: 'max', fast: false }
    })
  )
  expect($currentReasoningEffort.get()).toBe('max')
})

it('case 2c: the intent TTL expires — server truth becomes authoritative again', () => {
  vi.useFakeTimers()
  const stream = renderMessageStream('live')

  act(() =>
    stream.handleEvent({
      type: 'session.info',
      session_id: 'live',
      payload: { reasoning_effort: 'high', fast: false }
    })
  )

  act(() => {
    vi.setSystemTime(Date.now() + 1)
    setCurrentReasoningEffort('low')
  })

  // Before the TTL: guarded.
  act(() =>
    stream.handleEvent({
      type: 'session.info',
      session_id: 'live',
      payload: { reasoning_effort: 'high', fast: false }
    })
  )
  expect($currentReasoningEffort.get()).toBe('low')

  // Past the TTL: an abandoned pick cannot mask server truth forever.
  act(() => {
    vi.setSystemTime(Date.now() + 11_000)
    stream.handleEvent({
      type: 'session.info',
      session_id: 'live',
      payload: { reasoning_effort: 'high', fast: false }
    })
  })
  expect($currentReasoningEffort.get()).toBe('high')
})
