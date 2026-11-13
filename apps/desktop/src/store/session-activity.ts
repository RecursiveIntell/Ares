import { computed } from 'nanostores'

import { $sessionStates } from './session-states'

/** What the running turn is doing, from the backend's `session.info`.
 *
 * Empty when unknown: an idle session, or a gateway that predates the field.
 * Callers must not infer activity from `busy` — that conflation is exactly what
 * hid a background turn behind a finished-looking transcript.
 */
export function sessionActivity(sessionId: null | string) {
  return computed($sessionStates, states => (sessionId ? (states[sessionId]?.activity ?? '') : ''))
}
