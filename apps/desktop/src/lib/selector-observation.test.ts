import { describe, expect, it } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'

import { reconcileSelectorInfo } from './selector-observation'
const info = (revision: number, model: string, epoch = 'owner') => ({ model, provider: 'p', selector_version: { epoch, revision } })
const observe = (state: ClientSessionState, incoming: ReturnType<typeof info>, options = {}) => ({ ...state, ...reconcileSelectorInfo(state, incoming, options) })

describe('selector owner ordering', () => {
  it('preserves pre-version backend normalization without weakening an ordered owner', () => {
    const pending = { ...createClientSessionState(), model: 'alias', provider: 'p', pendingModelSelection: {
      model: 'alias', provider: 'p', previousModel: 'A', previousProvider: 'p'
    } }
    expect(reconcileSelectorInfo(pending, { model: 'canonical', provider: 'p' })).toMatchObject({ model: 'canonical', pendingModelSelection: null })
    const ordered = observe(createClientSessionState(), info(2, 'B'))
    expect(reconcileSelectorInfo(ordered, { model: 'canonical', provider: 'p' })).toEqual({})
  })
  it('an older Fast ACK cannot clear a newer effort intent', () => {
    const base = observe(createClientSessionState(), info(1, 'A'))
    const pending = { ...base, reasoningEffort: 'high', fast: false, pendingRuntimeOptions: { effort: 'high', fast: false } }
    const patch = reconcileSelectorInfo(pending, { ...info(2, 'A'), reasoning_effort: 'low', fast: false }, { settleOptions: ['fast'] })
    expect(patch.pendingRuntimeOptions).toEqual({ effort: 'high' })
    expect(patch.reasoningEffort).toBeUndefined()
  })
  it('rejects old observations but accepts a legitimate later return to A', () => {
    let state = observe(createClientSessionState(), info(2, 'B'))
    state = observe(state, info(1, 'A'))
    expect(state.model).toBe('B')
    state = observe(state, info(3, 'A'))
    expect(state.model).toBe('A')
  })
  it('rejects equal-revision conflicting data', () => {
    const state = observe(createClientSessionState(), info(2, 'B'))
    expect(observe(state, info(2, 'C')).model).toBe('B')
  })
  it('admits a replacement owner only on a newly bound runtime projection', () => {
    const state = observe(createClientSessionState(), info(2, 'B'))
    expect(observe(state, info(1, 'C', 'new-owner')).model).toBe('B')
    const next = observe(createClientSessionState('rebound-runtime'), info(1, 'C', 'new-owner'))
    expect(next.model).toBe('C')
    expect(observe(next, info(100, 'A')).model).toBe('C')
  })
  it('does not let legacy/malformed metadata downgrade an ordered owner', () => {
    const state = observe(createClientSessionState(), info(2, 'B'))
    expect(reconcileSelectorInfo(state, { model: 'A' })).toEqual({})
    expect(reconcileSelectorInfo(state, { model: 'A', selector_version: { epoch: 'owner', revision: -1 } })).toEqual({})
  })
  it('keeps C intent over B observation and does not lose a newer external observation on ACK', () => {
    let state = observe(createClientSessionState(), info(1, 'A'))
    state = { ...state, model: 'C', pendingModelSelection: { model: 'C', provider: 'p', previousModel: 'B', previousProvider: 'p' } }
    state = observe(state, info(2, 'B'))
    expect(state.model).toBe('C')
    state = observe(state, info(4, 'external'))
    expect(state.model).toBe('C')
    state = observe(state, info(3, 'C'), { settleModel: true })
    expect(state.model).toBe('external')
    expect(state.pendingModelSelection).toBeNull()
  })
  it('accepts backend normalization only on an exact settled write', () => {
    let state = observe(createClientSessionState(), info(1, 'A'))
    state = { ...state, model: 'alias', pendingModelSelection: { model: 'alias', provider: 'p', previousModel: 'A', previousProvider: 'p' } }
    state = observe(state, info(2, 'canonical'), { settleModel: true })
    expect(state.model).toBe('canonical')
  })
  it('keeps effort deferred until applied and rejects post-confirmation old effort', () => {
    let state = { ...createClientSessionState(), reasoningEffort: 'high', pendingRuntimeOptions: { effort: 'high' } }
    state = { ...state, ...reconcileSelectorInfo(state, { reasoning_effort: 'low', pending_runtime_options: { effort: 'high' }, selector_version: { epoch: 'owner', revision: 1 } }) } as typeof state
    expect(state.reasoningEffort).toBe('high')
    const applied = { ...state, ...reconcileSelectorInfo(state, { reasoning_effort: 'high', pending_runtime_options: null, selector_version: { epoch: 'owner', revision: 2 } }) }
    expect(applied.pendingRuntimeOptions).toBeNull()
    expect(reconcileSelectorInfo(applied, { reasoning_effort: 'low', selector_version: { epoch: 'owner', revision: 1 } })).toEqual({})
  })
})
