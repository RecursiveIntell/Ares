import { afterEach, describe, expect, it, vi } from 'vitest'

import { $gateway } from './gateway'
import { $goalsBySession, applyGoalState, applyGoalStatusText, clearSessionGoal, refreshSessionGoal } from './goals'
import { resetSessionGone } from './session-gone'

describe('goal store', () => {
  afterEach(() => {
    vi.useRealTimers()
    $goalsBySession.set({})
    $gateway.set(null as never)
    resetSessionGone()
  })

  it('stores active goals from /goal output', () => {
    applyGoalStatusText('s1', '⊙ Goal set (20-turn budget): ship the feature')

    expect($goalsBySession.get().s1).toMatchObject({
      status: 'active',
      title: 'ship the feature'
    })
  })

  it('keeps the current title for continuation and pause messages', () => {
    applyGoalStatusText('s1', '⊙ Goal set (20-turn budget): ship the feature')
    applyGoalStatusText('s1', '↻ Continuing toward goal (1/20): next step is tests')

    expect($goalsBySession.get().s1).toMatchObject({
      detail: 'Continuing toward goal (1/20): next step is tests',
      status: 'active',
      title: 'ship the feature'
    })

    applyGoalStatusText('s1', '⏸ Goal paused — 20/20 turns used. Use /goal resume to keep going.')

    expect($goalsBySession.get().s1).toMatchObject({
      status: 'paused',
      title: 'ship the feature'
    })
  })

  it('lingers done goals before clearing them', () => {
    vi.useFakeTimers()

    applyGoalStatusText('s1', '⊙ Goal set (20-turn budget): ship the feature')
    applyGoalStatusText('s1', '✓ Goal achieved: tests pass')

    expect($goalsBySession.get().s1).toMatchObject({ status: 'done' })

    vi.advanceTimersByTime(7_999)
    expect($goalsBySession.get().s1).toBeTruthy()

    vi.advanceTimersByTime(1)
    expect($goalsBySession.get().s1).toBeUndefined()
  })

  it('clears on no-goal output', () => {
    applyGoalStatusText('s1', '⊙ Goal set (20-turn budget): ship another feature')
    applyGoalStatusText('s1', 'No active goal. Set one with /goal <text>.')

    expect($goalsBySession.get().s1).toBeUndefined()
  })

  it('clears immediately on /goal clear output', () => {
    applyGoalStatusText('s1', '⊙ Goal set (20-turn budget): ship another feature')
    applyGoalStatusText('s1', '⏸ Goal paused — 20/20 turns used. Use /goal resume to keep going.')
    applyGoalStatusText('s1', '✓ Goal cleared.')

    expect($goalsBySession.get().s1).toBeUndefined()
  })

  it('cancels pending done clears when replacing a goal', () => {
    vi.useFakeTimers()

    applyGoalStatusText('s1', '⊙ Goal set: first')
    applyGoalStatusText('s1', '✓ Goal achieved: first done')
    applyGoalStatusText('s1', '⊙ Goal set: second')

    vi.advanceTimersByTime(8_000)

    expect($goalsBySession.get().s1).toMatchObject({ status: 'active', title: 'second' })

    clearSessionGoal('s1')
  })

  it('does not resurrect a done goal on hydration', () => {
    // `/goal status` reports "✓ Goal done" forever after completion (the DB
    // keeps terminal state). Hydrating that on session open must not bring
    // the completed chip back — Bot Mode's single endless session would show
    // it for the rest of time.
    applyGoalStatusText('s1', '✓ Goal done (12/20 turns): ship the feature', { hydrate: true })

    expect($goalsBySession.get().s1).toBeUndefined()
  })

  it('latches a reaped runtime after a terminal goal poll failure', async () => {
    const request = vi.fn(async () => {
      throw new Error('session not found')
    })

    $gateway.set({ request } as never)

    await refreshSessionGoal('dead-runtime')
    await refreshSessionGoal('dead-runtime')

    // The second call is suppressed: a reaped runtime id cannot recover just
    // because the status stack remounted or retried a background poll.
    expect(request).toHaveBeenCalledTimes(1)
  })

  it('hydration drops a lingering done chip already on screen', () => {
    vi.useFakeTimers()

    applyGoalStatusText('s1', '✓ Goal achieved: tests pass')
    applyGoalStatusText('s1', '✓ Goal done (12/20 turns): tests pass', { hydrate: true })

    expect($goalsBySession.get().s1).toBeUndefined()
  })

  it('hydration still surfaces non-terminal goals', () => {
    applyGoalStatusText('s1', '⊙ Goal (active, 3/20 turns): ship the feature', { hydrate: true })

    expect($goalsBySession.get().s1).toMatchObject({ status: 'active', title: 'ship the feature' })

    applyGoalStatusText('s2', '⏸ Goal (paused, 20/20 turns): other work', { hydrate: true })

    expect($goalsBySession.get().s2).toMatchObject({ status: 'paused', title: 'other work' })
  })

  // The backend's `GoalManager.status_line()` emits these shapes for a live
  // goal. The original parser only understood `⊙ Goal (…active…):`, so every
  // `⊙ Goal active · …` line fell through and the chip kept whatever it last
  // understood — typically "Goal waiting" forever.
  it('parses every active/waiting line the backend actually emits', () => {
    const cases: Array<[string, string]> = [
      ['⊙ Goal active · running (4/20 turns): ship the feature', 'active'],
      ['⊙ Goal active · waiting for authority (4/20 turns): ship the feature', 'waiting'],
      ['⊙ Goal active · blocked (4/20 turns): ship the feature', 'active'],
      ['⊙ Goal active · checkpointed · continuation pending (4/20 turns): ship the feature', 'active'],
      ['⊙ Goal active · turn budget exhausted (20/20 turns): ship the feature', 'active'],
      ['⏳ Goal active · waiting (30s — CI, 4/20 turns): ship the feature', 'waiting'],
      ['⏳ Goal (parked on pid 42, 4/20 turns): ship the feature', 'waiting'],
      ['⊙ Goal set (20-turn budget): ship the feature', 'active'],
      ['⏸ Goal (paused, 4/20 turns): ship the feature', 'paused'],
      ['✓ Goal done (4/20 turns): ship the feature', 'done']
    ]

    for (const [line, status] of cases) {
      applyGoalStatusText('s1', line)

      expect($goalsBySession.get().s1, line).toMatchObject({
        status,
        title: 'ship the feature'
      })
      clearSessionGoal('s1')
    }
  })

  it('applies authoritative structured goal state', () => {
    applyGoalState('s1', {
      max_turns: 20,
      outcome: 'CONTINUATION_REQUIRED',
      status: 'active',
      title: 'ship it',
      turns_used: 3
    })

    expect($goalsBySession.get().s1).toMatchObject({ status: 'active', title: 'ship it' })

    applyGoalState('s1', { outcome: 'WAITING_FOR_AUTHORITY', status: 'active', title: 'ship it' })

    expect($goalsBySession.get().s1).toMatchObject({ status: 'waiting', title: 'ship it' })

    applyGoalState('s1', { status: 'paused', title: 'ship it' })

    expect($goalsBySession.get().s1).toMatchObject({ status: 'paused' })

    applyGoalState('s1', { status: 'done', title: 'ship it' })

    expect($goalsBySession.get().s1).toMatchObject({ status: 'done' })

    applyGoalState('s1', { status: 'cleared', title: 'ship it' })

    expect($goalsBySession.get().s1).toBeUndefined()
  })

  it('structured state clears a stale waiting pill', () => {
    applyGoalStatusText('s1', '⏳ Goal (parked on pid 42, 4/20 turns): ship it')

    expect($goalsBySession.get().s1?.status).toBe('waiting')

    applyGoalState('s1', { outcome: 'GOAL_ACTIVE', status: 'active', title: 'ship it', waiting: false })

    expect($goalsBySession.get().s1).toMatchObject({ status: 'active', title: 'ship it' })
  })
})
