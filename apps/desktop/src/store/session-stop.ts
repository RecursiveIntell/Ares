import { computed } from 'nanostores'

import { $sessionStates } from './session-states'

/** A Stop was accepted, but the turn has not settled.
 *
 * `interrupted` is the interrupt ACK; `busy` is the backend still owning a
 * turn. Together they are the honest "we asked, it has not stopped" state — the
 * UI must not present it as stopped, and must keep the Stop control available
 * so a second press can retry the interrupt.
 */
export function sessionStopUnsettled(sessionId: null | string) {
  return computed($sessionStates, states => {
    const state = sessionId ? states[sessionId] : undefined

    return Boolean(state?.busy && state?.interrupted && !state?.interruptPending)
  })
}
