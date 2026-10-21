import { afterEach, describe, expect, it, vi } from 'vitest'

import { $gateway } from './gateway'
import { $goalsBySession, applyGoalStatusText, clearSessionGoal, refreshSessionGoal } from './goals'
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

  // ── Full status_line() grammar (Task 1.3, audit M2) ────────────────────
  // The runtime emits these exactly (hermes_cli/goals.py status_line()); each
  // must parse to the right status — never leave the previous label stale.

  it('parses the plain running line', () => {
    applyGoalStatusText('s1', '⊙ Goal set: ship the feature')
    applyGoalStatusText('s1', '⊙ Goal active · running (6/50 turns): ship the feature')

    expect($goalsBySession.get().s1).toMatchObject({ status: 'active', title: 'ship the feature' })
  })

  it('parses the awaiting-confirmation line as active', () => {
    applyGoalStatusText('s1', '⊙ Goal set: finish phases')
    applyGoalStatusText(
      's1',
      '⊙ Goal active · awaiting your confirmation (verify with /goal complete, 6/50 turns): finish phases'
    )

    expect($goalsBySession.get().s1).toMatchObject({ status: 'active', title: 'finish phases' })
  })

  it('parses the waiting-for-authority line as waiting', () => {
    applyGoalStatusText('s1', '⊙ Goal set: ci merge train')
    applyGoalStatusText('s1', '⊙ Goal active · waiting for authority (7/50 turns): ci merge train')

    expect($goalsBySession.get().s1).toMatchObject({ status: 'waiting', title: 'ci merge train' })
  })

  it('parses the blocked line as active-looking', () => {
    applyGoalStatusText('s1', '⊙ Goal set: x')
    applyGoalStatusText('s1', '⊙ Goal active · blocked (6/50 turns): x')

    expect($goalsBySession.get().s1).toMatchObject({ status: 'active', title: 'x' })
  })

  it('parses checkpointed outcome lines as active-looking', () => {
    applyGoalStatusText('s1', '⊙ Goal set: x')
    applyGoalStatusText('s1', '⊙ Goal active · checkpointed · turn_budget_exhausted (6/50 turns): x')

    expect($goalsBySession.get().s1).toMatchObject({ status: 'active', title: 'x' })
  })

  it('parses the time-barrier waiting line', () => {
    applyGoalStatusText('s1', '⊙ Goal set: rate-limited work')
    applyGoalStatusText('s1', '⏳ Goal active · waiting (12s — backoff, 6/50 turns): rate-limited work')

    expect($goalsBySession.get().s1).toMatchObject({ status: 'waiting', title: 'rate-limited work' })
  })

  it('replaces a stale waiting pill when the goal resumes work', () => {
    applyGoalStatusText('s1', '⊙ Goal set: ci merge train')
    applyGoalStatusText('s1', '⊙ Goal active · waiting for authority (7/50 turns): ci merge train')
    expect($goalsBySession.get().s1).toMatchObject({ status: 'waiting' })

    applyGoalStatusText('s1', '⊙ Goal active · running (8/50 turns): ci merge train')
    expect($goalsBySession.get().s1).toMatchObject({ status: 'active' })
  })

  it('parses continuation-pending checkpointed lines as active', () => {
    applyGoalStatusText('s1', '⊙ Goal set: y')
    applyGoalStatusText('s1', '⊙ Goal active · checkpointed · continuation pending (9/50 turns, contract): y')

    expect($goalsBySession.get().s1).toMatchObject({ status: 'active', title: 'y' })
  })

  it('parses the nudge-parked confirmation line as waiting (second judge-done)', () => {
    applyGoalStatusText('s1', '⊙ Goal set: finish phases')
    applyGoalStatusText('s1', '⊙ Goal active · awaiting your confirmation (verify with /goal complete, 9/50 turns): finish phases')
    expect($goalsBySession.get().s1).toMatchObject({ status: 'active' })

    // The post-nudge authority wait reuses the same line but with the
    // post-nudge semantics the runtime emits after a SECOND judge-done.
    applyGoalStatusText('s1', '⊙ Goal active · awaiting your confirmation (verify with /goal complete, 10/50 turns): finish phases')
    expect($goalsBySession.get().s1).toMatchObject({ status: 'active', title: 'finish phases' })
  })
})
