import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { notify } from '@/store/notifications'
import {
  $sessionStates,
  publishSessionState,
  type SessionTileDelegate,
  setSessionTileDelegate
} from '@/store/session-states'

import { clearRuntimeOptionUncertainty, reconcileRuntimeOptionFailure } from './runtime-option-recovery'

vi.mock('@/store/notifications', () => ({ notify: vi.fn(), dismissNotification: vi.fn() }))
vi.mock('@/i18n', () => ({ translateNow: (key: string) => key }))

beforeEach(() => {
  vi.clearAllMocks()
  $sessionStates.set({})
  publishSessionState('r', createClientSessionState('stored'))
  setSessionTileDelegate({
    updateSession: (id, fn) => {
      const state = fn($sessionStates.get()[id])
      publishSessionState(id, state)

      return state
    }
  } as SessionTileDelegate)
})
afterEach(() => {
  $sessionStates.set({})
  setSessionTileDelegate({} as SessionTileDelegate)
})

function context(response: unknown, dimension: 'effort' | 'fast' = 'effort') {
  return {
    sessionId: 'r',
    dimension,
    owns: () => true,
    request: vi.fn().mockResolvedValue(response),
    applyObserved: vi.fn()
  }
}

it.each([undefined, null, '', '   ', 0, false])(
  'does not accept readback with unproven host boot %#',
  async host_boot_id => {
    const ctx = context({ owner: 'compute_host', session_id: 'r', host_boot_id, value: 'high' })
    await reconcileRuntimeOptionFailure(Object.assign(new Error('unknown'), { code: 5019 }), ctx)
    expect(ctx.applyObserved).not.toHaveBeenCalled()
    expect($sessionStates.get().r.unconfirmedRuntimeOptions).toEqual(['effort'])
  }
)

it.each([
  ['effort', 'none', 'none'],
  ['effort', '', ''],
  ['effort', 'xhigh', 'xhigh'],
  ['fast', 'normal', false],
  ['fast', 'fast', true]
] as const)('reads %s=%s without retrying a write', async (dimension, value, expected) => {
  const ctx = context({ owner: 'compute_host', session_id: 'r', host_boot_id: 'owner-boot', value }, dimension)
  expect(await reconcileRuntimeOptionFailure(Object.assign(new Error('unknown'), { code: 5019 }), ctx)).toBe(true)
  expect(ctx.request).toHaveBeenCalledExactlyOnceWith('config.get', {
    key: dimension === 'effort' ? 'reasoning' : 'fast',
    session_id: 'r'
  })
  expect(ctx.applyObserved).toHaveBeenCalledExactlyOnceWith(expected)
  expect($sessionStates.get().r.unconfirmedRuntimeOptions).toBeUndefined()
})

it.each([
  { value: 'high' },
  { owner: 'compute_host', session_id: 'other', value: 'high' },
  { owner: 'compute_host', session_id: 'r', value: 'bogus' },
  null
])('does not trust incomplete/foreign readback %#', async response => {
  const ctx = context(response)
  await reconcileRuntimeOptionFailure(new Error('request timed out after 30s: config.set'), ctx)
  expect(ctx.applyObserved).not.toHaveBeenCalled()
  expect($sessionStates.get().r.unconfirmedRuntimeOptions).toEqual(['effort'])
  expect(notify).toHaveBeenCalledWith(expect.objectContaining({ kind: 'warning', durationMs: 0 }))
})

it('does not read or notify for an obsolete intent', async () => {
  const ctx = { ...context({}), owns: () => false }
  await reconcileRuntimeOptionFailure(new Error('Hermes gateway connection closed'), ctx)
  expect(ctx.request).not.toHaveBeenCalled()
  expect(notify).not.toHaveBeenCalled()
})

it('late readback cannot overwrite a newer intent', async () => {
  let current = true
  let resolve!: (value: unknown) => void

  const ctx = {
    ...context({}),
    owns: () => current,
    request: vi.fn(
      () =>
        new Promise(r => {
          resolve = r
        })
    ) as any
  }

  const pending = reconcileRuntimeOptionFailure(new Error('request timed out'), ctx)
  current = false
  clearRuntimeOptionUncertainty('r', 'effort')
  resolve({ owner: 'compute_host', session_id: 'r', value: 'low' })
  await pending
  expect(ctx.applyObserved).not.toHaveBeenCalled()
  expect(notify).not.toHaveBeenCalled()
  expect($sessionStates.get().r.unconfirmedRuntimeOptions).toBeUndefined()
})

it('leaves confirmed rejection on the existing rollback path without reading', async () => {
  const ctx = context({})
  expect(
    await reconcileRuntimeOptionFailure(Object.assign(new Error('request not applied'), { code: 4009 }), ctx)
  ).toBe(false)
  expect(ctx.request).not.toHaveBeenCalled()
})

it('clears only the acknowledged dimension', () => {
  publishSessionState('r', { ...$sessionStates.get().r, unconfirmedRuntimeOptions: ['effort', 'fast'] })
  clearRuntimeOptionUncertainty('r', 'effort')
  expect($sessionStates.get().r.unconfirmedRuntimeOptions).toEqual(['fast'])
})

it('does not recreate a removed runtime even if an intent guard remains true', async () => {
  const ctx = context({ owner: 'compute_host', session_id: 'r', value: 'high' })
  $sessionStates.set({})
  await expect(reconcileRuntimeOptionFailure(new Error('request timed out'), ctx)).resolves.toBe(true)
  expect(ctx.request).not.toHaveBeenCalled()
  expect($sessionStates.get()).toEqual({})
  expect(notify).not.toHaveBeenCalled()
  expect(() => clearRuntimeOptionUncertainty('r', 'effort')).not.toThrow()
})

it('ignores readback after the runtime cache is removed', async () => {
  let resolve!: (value: unknown) => void

  const ctx = {
    ...context({}),
    request: vi.fn(
      () =>
        new Promise(r => {
          resolve = r
        })
    ) as any
  }

  const pending = reconcileRuntimeOptionFailure(new Error('request timed out'), ctx)
  $sessionStates.set({})
  resolve({ owner: 'compute_host', session_id: 'r', value: 'high' })
  await pending
  expect(ctx.applyObserved).not.toHaveBeenCalled()
  expect(notify).not.toHaveBeenCalled()
  expect($sessionStates.get()).toEqual({})
})
