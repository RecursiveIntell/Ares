import assert from 'node:assert/strict'
import test from 'node:test'
import { harness, members, deferred, flush, drive } from './stop-custody-harness.mjs'

test('PR127: cancelled proven refusal retains preparation custody, then releases without admission', async () => {
  const gate = deferred()
  const h = await harness(members(1), {
    submitError: s => s.submits === 1 ? Object.assign(new Error('session not found'), { code: 4001 }) : null,
    beforeResume(s, sessions) {
      if (s.submits !== 1) return
      sessions.delete(s.runtime); s.runtime += '-retry'; sessions.set(s.runtime, s)
    },
    resumeProjection(s, method, projection) {
      if (method !== 'session.resume' || s.submits !== 1) return projection
      const lazy = { ...projection }; delete lazy.turn_outcomes; return lazy
    },
    async capabilityPoll() {
      await gate.promise
      throw Object.assign(new Error('session_id and full accepted_turn identity required'), { code: 4006 })
    }
  })
  const pending = drive(h)
  await flush()
  const coordinator = h.gc.groupRoomCoordinators.get('Room')
  const stopped = await h.gc.stopGroupThread('Room', 't1', h.roster)
  assert.equal(stopped.status, 'unconfirmed')
  assert.equal(coordinator.active, 1)
  assert.equal(h.activeLeases(), 1)
  assert.equal(h.gc.groupRuntimeSessionOwners.size, 1)
  assert.equal(h.rpc('prompt.submit').length, 1)
  gate.resolve(); await flush(); await h.advance(); await pending
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.equal(h.gc.groupRuntimeSessionOwners.size, 0)
  assert.equal(coordinator.active, 0)
  assert.equal(h.activeLeases(), 0)
  assert.equal(h.leases[0].releases, 1)
  assert.equal(Object.keys(h.room().stranded).length, 0)
  assert.equal((await h.gc.stopGroupThread('Room', 't1', h.roster)).status, 'stopped')
})

test('PR127: a refused first submit never excuses unknown acceptance of its retry', async () => {
  const h = await harness(members(1), {
    submitError: s => s.submits === 1
      ? Object.assign(new Error('session not found'), { code: 4001 })
      : new Error('lost retry admission acknowledgement')
  })
  await drive(h)
  assert.equal(h.rpc('prompt.submit').length, 2)
  assert.equal(h.activeLeases(), 1)
  assert.equal(h.gc.groupRoomCoordinators.get('Room').active, 1)
  const first = await h.gc.stopGroupThread('Room', 't1', h.roster)
  assert.equal(first.status, 'stopping')
  assert.equal(first.pending, 1)
  assert.equal(h.activeLeases(), 1)
  assert.equal(h.gc.groupRuntimeSessionOwners.size, 1)
  const second = await h.gc.stopGroupThread('Room', 't1', h.roster)
  assert.equal(second.status, 'stopping')
  assert.equal(h.rpc('prompt.submit').length, 2, 'Stop never retries uncertain text')
  assert.equal(h.leases[0].releases, 0)
})

test('PR127: four unknown retry admissions continue to block a six-member room', async () => {
  const h = await harness(members(6), {
    submitError: s => s.submits === 1
      ? Object.assign(new Error('session not found'), { code: 4001 })
      : new Error('lost retry acknowledgement')
  })
  const pending = drive(h)
  await flush()
  assert.equal(h.rpc('prompt.submit').length, 8)
  assert.equal(h.gc.groupRoomCoordinators.get('Room').active, 4)
  assert.equal(h.gc.groupRoomCoordinators.get('Room').queue.length, 2)
  assert.equal(h.activeLeases(), 4)
  const stopped = await h.gc.stopGroupThread('Room', 't1', h.roster)
  await h.advance(); await pending
  assert.equal(stopped.status, 'stopping')
  assert.equal(stopped.pending, 4)
  assert.equal(h.gc.groupRoomCoordinators.get('Room').active, 4)
  assert.equal(h.activeLeases(), 4)
  assert.equal(h.rpc('prompt.submit').length, 8)
})
