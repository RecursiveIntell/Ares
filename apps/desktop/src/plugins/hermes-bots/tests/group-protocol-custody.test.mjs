import assert from 'node:assert/strict'
import test from 'node:test'
import { harness, members, deferred, flush, drive } from './stop-custody-harness.mjs'

test('capability resume recreation transfers custody to the admitted runtime', async () => {
  let oldRuntime, submittedRuntime, h
  const options = {
    beforeAdmissionResume(session, sessions) {
      oldRuntime = session.runtime
      sessions.delete(oldRuntime)
      session.runtime = `${oldRuntime}-recreated`
      sessions.set(session.runtime, session)
    },
    onSubmit(session) {
      submittedRuntime = session.runtime
      const owners = [...h.gc.groupRuntimeSessionOwners.values()]
      assert.equal(owners.length, 1)
      assert.equal(owners[0].runtime, submittedRuntime)
      assert.equal(owners[0].sessionLock.includes(oldRuntime + '-recreated'), true)
    }
  }
  h = await harness(members(1), options)
  const pending = drive(h)
  await flush()
  assert.equal(submittedRuntime, `${oldRuntime}-recreated`)
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.equal((await h.gc.stopGroupThread('Room', 't1', h.roster)).status, 'stopped')
  await h.advance()
  await pending
  assert.equal(h.rpc('session.interrupt').at(-1).params.session_id, submittedRuntime)
  assert.equal(h.gc.groupRuntimeSessionOwners.size, 0)
})

test('Stop during capability resume interrupts the recreated runtime without admission', async () => {
  const gate = deferred()
  let oldRuntime, newRuntime
  const h = await harness(members(1), {
    async beforeAdmissionResume(session, sessions) {
      oldRuntime = session.runtime
      await gate.promise
      sessions.delete(oldRuntime)
      newRuntime = `${oldRuntime}-recreated`
      session.runtime = newRuntime
      session.state = 'running'
      sessions.set(newRuntime, session)
    }
  })
  const pending = drive(h)
  await flush()
  assert.ok(oldRuntime)
  const stopping = h.gc.stopGroupThread('Room', 't1', h.roster)
  await flush()
  gate.resolve()
  assert.equal((await stopping).status, 'unconfirmed', 'Stop cannot confirm an unresolved admission resume')
  await h.advance()
  await pending
  assert.equal(h.rpc('prompt.submit').length, 0)
  assert.ok(h.rpc('session.interrupt').some(c => c.params.session_id === newRuntime))
  assert.equal(h.gc.groupRuntimeSessionOwners.size, 0)
  assert.equal((await h.gc.stopGroupThread('Room', 't1', h.roster)).status, 'stopped')
})

test('Stop during retry capability probe prevents the replacement prompt', async () => {
  const gate = deferred()
  let probedRuntime
  const h = await harness(members(1), {
    submitError: session => session.submits === 1 ? Object.assign(new Error('reaped'), { code: 4001 }) : null,
    beforeResume(session, sessions) {
      if (session.submits !== 1) return
      sessions.delete(session.runtime)
      session.runtime += '-retry'
      sessions.set(session.runtime, session)
    },
    resumeProjection(session, method, projection) {
      if (method === 'session.resume' && session.submits === 1) {
        const lazy = { ...projection }
        delete lazy.turn_outcomes
        return lazy
      }
      return projection
    },
    async capabilityPoll(session) {
      probedRuntime = session.runtime
      await gate.promise
      throw Object.assign(new Error('session_id and full accepted_turn identity required'), { code: 4006 })
    }
  })
  const pending = drive(h)
  await flush()
  assert.ok(probedRuntime?.endsWith('-retry'))
  const stopping = h.gc.stopGroupThread('Room', 't1', h.roster)
  await flush()
  gate.resolve()
  await stopping
  await h.advance()
  await pending
  assert.equal(h.rpc('prompt.submit').length, 1, 'the failed original admission is the only prompt attempt')
  const interrupts = h.rpc('session.interrupt').filter(c => c.params.session_id === probedRuntime)
  assert.ok(interrupts.length)
  assert.ok(interrupts.every(c => c.route.connectionId === 'local' && c.route.targetProfile === 'bot1'))
  assert.equal(h.gc.groupRuntimeSessionOwners.size, 0)
})

test('retry remint collision preserves destination custody and never interrupts its owner', async () => {
  let h, destinationKey, occupiedRuntime, occupant, originalOccurrence, originalLock, originalRuntime
  h = await harness(members(1), {
    submitError: () => Object.assign(new Error('reaped'), { code: 4001 }),
    beforeResume(session, sessions) {
      if (session.submits !== 1) return
      const occurrence = [...h.gc.groupRuntimeSessionOwners.values()][0]
      const priorRuntime = session.runtime
      originalOccurrence = occurrence
      originalLock = occurrence.sessionLock
      originalRuntime = occurrence.runtime
      sessions.delete(priorRuntime)
      occupiedRuntime = `${priorRuntime}-occupied`
      session.runtime = occupiedRuntime
      sessions.set(occupiedRuntime, session)
      destinationKey = occurrence.sessionLock.replace(priorRuntime, occupiedRuntime)
      occupant = { otherOccurrence: true }
      h.gc.groupRuntimeSessionOwners.set(destinationKey, occupant)
    }
  })
  await drive(h)
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.equal(h.gc.groupRuntimeSessionOwners.get(destinationKey), occupant)
  assert.equal(h.rpc('session.interrupt').filter(c => c.params.session_id === occupiedRuntime).length, 0)
  h.gc.groupRuntimeSessionOwners.delete(destinationKey)
  const retained = [...h.gc.groupRuntimeSessionOwners.values()]
  assert.equal(retained.length, 1, 'failed original admission retains its unresolved source custody')
  assert.equal(retained[0], originalOccurrence)
  assert.equal(retained[0].runtime, originalRuntime)
  assert.equal(retained[0].sessionLock, originalLock)
  assert.equal(h.gc.groupRuntimeSessionOwners.get(originalLock), originalOccurrence)
})
