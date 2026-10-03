// Explicit v1 service fixtures for the existing group/lease/stop tests.
// These values model accepted execution, never client-side history novelty.
export function fixtureAdmission(session, requestId) {
  session.accepted_turn = { request_id: requestId, session_id: session.runtime,
    route: 'inline', host_boot_id: null }
  return session.accepted_turn
}

export function fixtureProjection(session, { state = 'complete', finalized = session.finalized || [], reason } = {}) {
  return { version: 1, scope: 'process_local', availability: 'available',
    turns: session.accepted_turn ? [{ accepted_turn: session.accepted_turn, state,
      finalized, ...(reason ? { reason } : {}) }] : [] }
}

export function assertFixturePoll(session, params) {
  assert.equal(params.session_id, session.runtime, 'turn polling stays in the accepted runtime namespace')
  assert.deepEqual({ ...params.accepted_turn }, { ...session.accepted_turn },
    'turn polling observes the exact backend admission')
}
import assert from 'node:assert/strict'
