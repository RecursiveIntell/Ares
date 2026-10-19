import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { reconcileSelectorInfo } from '@/lib/selector-observation'
import { $currentModel, setActiveSessionId } from '@/store/session'
import { $sessionStates, publishSessionState } from '@/store/session-states'

import { applyRuntimeInfo } from './use-session-actions/utils'
import { useSessionStateCache } from './use-session-state-cache'

afterEach(() => {
  cleanup()
  $sessionStates.set({})
  setActiveSessionId(null)
})
const oldInfo = { session_id: 'r', model: 'A', provider: 'p', selector_version: { epoch: 'owner', revision: 1 } }
const newInfo = { ...oldInfo, model: 'B', selector_version: { epoch: 'owner', revision: 2 } }

it('explicit resume/activation readback cannot regress an admitted same-owner observation', () => {
  const base = createClientSessionState('stored')
  publishSessionState('r', { ...base, ...reconcileSelectorInfo(base, newInfo) })
  expect(applyRuntimeInfo(oldInfo, { foreground: false })?.model).toBeUndefined()
  expect(
    applyRuntimeInfo({ ...oldInfo, selector_version: { epoch: 'owner', revision: 3 } }, { foreground: false })?.model
  ).toBe('A')
})

it('generic runtime readback cannot replace an established owner with a stale epoch', () => {
  const base = createClientSessionState('stored')
  const admitted = { ...newInfo, selector_version: { epoch: 'current-owner', revision: 8 } }
  publishSessionState('r', { ...base, ...reconcileSelectorInfo(base, admitted) })

  for (const revision of [3, 4]) {
    expect(
      applyRuntimeInfo({ ...oldInfo, selector_version: { epoch: 'retired-owner', revision } }, { foreground: false })
        ?.model
    ).toBeUndefined()
  }

  expect($sessionStates.get().r.selectorObservation?.version).toEqual(admitted.selector_version)
})

it('a staged warm-cache view cannot overwrite the newer runtime selector projection', () => {
  setActiveSessionId('r')

  const { result } = renderHook(() =>
    useSessionStateCache({
      activeSessionId: 'r',
      selectedStoredSessionId: 'stored',
      busyRef: { current: false },
      setBusy: () => {},
      setAwaitingResponse: () => {},
      setMessages: () => {}
    })
  )

  const base = createClientSessionState('stored')
  const older = { ...base, ...reconcileSelectorInfo(base, oldInfo) }
  act(() => {
    result.current.updateSessionState('r', state => ({ ...state, ...reconcileSelectorInfo(state, newInfo) }), 'stored')
  })
  act(() => {
    result.current.syncSessionStateToView('r', older)
  })
  expect($currentModel.get()).toBe('B')
})
