import { afterEach, describe, expect, it } from 'vitest'

import { $sessionStates } from './session-states'
import { sessionStopUnsettled } from './session-stop'

type States = Record<string, { busy: boolean; interruptPending: boolean; interrupted: boolean }>

const setState = (state: States[string]) => {
  $sessionStates.set({ s1: state } as never)
}

describe('sessionStopUnsettled', () => {
  afterEach(() => {
    $sessionStates.set({})
  })

  it('is true only when Stop was acked and the turn is still busy', () => {
    setState({ busy: true, interruptPending: false, interrupted: true })

    expect(sessionStopUnsettled('s1').get()).toBe(true)
  })

  it('stays false while the interrupt is still in flight', () => {
    setState({ busy: true, interruptPending: true, interrupted: true })

    expect(sessionStopUnsettled('s1').get()).toBe(false)
  })

  it('is false before any Stop was accepted', () => {
    setState({ busy: true, interruptPending: false, interrupted: false })

    expect(sessionStopUnsettled('s1').get()).toBe(false)
  })

  it('is false once the turn settles', () => {
    setState({ busy: false, interruptPending: false, interrupted: true })

    expect(sessionStopUnsettled('s1').get()).toBe(false)
  })

  it('is false without an active session', () => {
    setState({ busy: true, interruptPending: false, interrupted: true })

    expect(sessionStopUnsettled(null).get()).toBe(false)
  })
})
