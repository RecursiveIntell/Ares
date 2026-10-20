import assert from 'node:assert/strict'
import test from 'node:test'
import { harness, members, deferred, flush, drive } from './stop-custody-harness.mjs'

const coordinator = h => h.gc.groupRoomCoordinators.get('Room')
const occurrences = h => [...coordinator(h).occurrences]
const receipts = h => Object.values(h.room().stranded)
const clone = value => JSON.parse(JSON.stringify(value))
const settleStop = async (h, pending) => {
  const result = await h.gc.stopGroupThread('Room', 't1', h.roster)
  await h.advance(); await pending; await flush()
  return result
}
const assertCustody = (h, count, workers) => {
  assert.equal(receipts(h).length, count)
  assert.equal(occurrences(h).length, count)
  assert.equal(coordinator(h).members.size, count)
  assert.equal(h.gc.groupRuntimeSessionOwners.size, count)
  assert.equal(coordinator(h).active, workers)
  assert.equal(h.activeLeases(), count)
  assert.ok(receipts(h).every(m => m.stop_requested && m.delivery.accepted_turn))
  assert.ok(h.leases.every(l => l.releases === 0))
}
const assertReleased = h => {
  assert.equal(receipts(h).length, 0)
  assert.equal(occurrences(h).length, 0)
  assert.equal(coordinator(h).members.size, 0)
  assert.equal(h.gc.groupRuntimeSessionOwners.size, 0)
  assert.equal(coordinator(h).active, 0)
  assert.equal(h.activeLeases(), 0)
  assert.ok(h.leases.every(l => l.releases === 1))
}

for (const mode of ['running', 'waiting', 'expired']) {
  test(`failed Stop retains ${mode} custody; explicit retry alone repeats interruption`, async () => {
    const options = { interruptError: true }
    const h = await harness(members(6), options), pending = drive(h); await flush()
    if (mode === 'waiting') {
      for (const s of h.sessions.values()) { s.state = 'waiting'; s.pending = { request_id: `ask-${s.profile}`, question: 'Choose?' } }
      await h.advance()
    }
    if (mode === 'expired') await h.advance(21 * 60 * 1000)
    const count = mode === 'waiting' ? 6 : 4, workers = mode === 'waiting' ? 2 : 4
    const result = await settleStop(h, pending)
    assert.deepEqual(result, { status: 'unconfirmed', unconfirmed: count, pending: count })
    assertCustody(h, count, workers)
    assert.equal(h.rpc('session.interrupt').length, count)
    assert.equal(Object.keys(h.gc.$groupClarify.get()).length, 0)
    assert.ok(h.room().turns.every(t => t.phase === 'stop-unconfirmed'))
    assert.match(h.gc.groupActivityLabel(h.gc.currentGroupActivity('Room').at(-1)), /unconfirmed/)
    await h.advance(21 * 60 * 1000)
    assert.equal(h.rpc('session.interrupt').length, count, 'no automatic interrupt spin')
    const firstTargets = h.rpc('session.interrupt').map(c => [c.route, c.params.session_id])
    await h.gc.stopGroupThread('Room', 't1', h.roster)
    assert.equal(h.rpc('session.interrupt').length, count * 2)
    assert.deepEqual(h.rpc('session.interrupt').slice(count).map(c => [c.route, c.params.session_id]), firstTargets)
    assertCustody(h, count, workers)
    options.interruptError = false
    const stopped = await h.gc.stopGroupThread('Room', 't1', h.roster)
    await flush()
    assert.equal(stopped.unconfirmed, 0)
    assert.ok(['stopping', 'stopped'].includes(stopped.status))
    assertReleased(h)
    await h.gc.stopGroupThread('Room', 't1', h.roster)
    assert.equal(h.rpc('session.interrupt').length, count * 3)
    assert.ok(h.leases.every(l => l.releases === 1))
  })
}

for (const reply of [{}, { interrupted: false }, { interrupted: true }, { status: 'ok' }, null]) {
  test(`malformed/negative interrupt ACK ${JSON.stringify(reply)} never proves stopped`, async () => {
    const options = { interruptReply: reply }
    const h = await harness(members(1), options), pending = drive(h); await flush()
    assert.equal((await settleStop(h, pending)).status, 'unconfirmed')
    assertCustody(h, 1, 1)
    assert.equal([...h.sessions.values()][0].state, 'running')
    options.interruptReply = undefined
    await h.gc.stopGroupThread('Room', 't1', h.roster)
    await flush()
    assertReleased(h)
  })
}

test('lost ACK after backend applied interrupt can retire only from exact interrupted outcome', async () => {
  const h = await harness(members(1), { interruptBehavior: session => {
    session.state = 'interrupted'; throw new Error('ACK was lost after apply')
  } }), pending = drive(h); await flush()
  await settleStop(h, pending)
  assert.equal(h.rpc('session.turn.poll').at(-1).params.accepted_turn.request_id,
    [...h.sessions.values()][0].ref.request_id, 'retirement used the accepted request despite a lost ACK')
  await h.gc.harvestStrandedGroupReply('Room', h.roster[0])
  assertReleased(h)
  assert.equal(h.posts().length, 0)
  assert.deepEqual(h.room().watermarks, {})
  assert.equal(h.rpc('prompt.submit').length, 1)
})

for (const state of ['complete', 'error', 'interrupted']) {
  test(`exact ${state} outcome releases stopped custody without delivery or consumed input`, async () => {
    const h = await harness(members(1), { interruptError: true }), pending = drive(h); await flush()
    await settleStop(h, pending)
    h.finish('bot1', 'MUST_NOT_PUBLISH', state)
    await h.gc.harvestStrandedGroupReply('Room', h.roster[0])
    await h.gc.harvestStrandedGroupReply('Room', h.roster[0])
    assertReleased(h)
    assert.equal(h.posts().length, 0)
    assert.deepEqual(h.room().watermarks, {})
    assert.deepEqual(h.room().consumedInputs, {})
    assert.equal(h.gc.$botAttention.get()['local::bot1'], undefined)
    assert.equal(h.rpc('prompt.submit').length, 1)
  })
}

test('stopped harvest checks complete accepted identity; unrelated terminal cannot release worker', async () => {
  const h = await harness(members(1), { interruptError: true }), pending = drive(h); await flush()
  await settleStop(h, pending)
  const session = [...h.sessions.values()][0], original = session.ref
  session.state = 'complete'; session.ref = { ...original, host_boot_id: 'different-host' }
  await h.gc.harvestStrandedGroupReply('Room', h.roster[0])
  assertCustody(h, 1, 1)
  session.ref = original
  await h.gc.harvestStrandedGroupReply('Room', h.roster[0])
  assertReleased(h)
})

test('exact waiting reconciliation releases only worker, preserves stopped receipt and lease', async () => {
  const options = { interruptError: true }
  const h = await harness(members(1), options), pending = drive(h); await flush()
  await settleStop(h, pending)
  const session = [...h.sessions.values()][0]
  session.state = 'waiting'; session.pending = { request_id: 'late-question', question: 'Choose?' }
  await h.gc.harvestStrandedGroupReply('Room', h.roster[0])
  assertCustody(h, 1, 0)
  assert.equal(Object.keys(h.gc.$groupClarify.get()).length, 0)
  options.interruptError = false
  await h.gc.stopGroupThread('Room', 't1', h.roster)
  await flush()
  assertReleased(h)
})

test('unconfirmed four workers hold unrelated ready work behind scheduler ceiling', async () => {
  const options = { interruptError: true }
  const h = await harness(members(6), options), pending = drive(h); await flush()
  await settleStop(h, pending)
  h.gc.sendToGroupChat('Room', h.roster.slice(4), '@all resume new work', 't2')
  await h.advance(250)
  assert.equal(h.rpc('prompt.submit').length, 4)
  assert.equal(coordinator(h).active, 4)
  assert.equal(h.activeLeases(), 4)
  options.interruptError = false
  await h.gc.stopGroupThread('Room', 't2', h.roster)
  await h.advance()
  assertReleased(h)
})

test('failed Stop retry stays source-qualified with duplicate names, changed roster and active source', async () => {
  const roster = ['east', 'west'].map(connectionId => ({ name: 'same', connectionId, sourceScoped: true, remoteSource: true,
    route: { connectionId, mode: 'remote', profile: 'same', targetProfile: 'default' } }))
  const options = { interruptError: true }
  const h = await harness(roster, options), pending = drive(h); await flush()
  const targets = h.rpc('prompt.submit').map(c => [c.route.connectionId, c.params.session_id]).sort()
  await settleStop(h, pending)
  h.switchConnection('elsewhere')
  h.gc.updateGroupChat('Room', room => { room.sessions = { 'east::same': 'wrong', 'west::same': 'wrong' }; return room })
  options.interruptError = false
  await h.gc.stopGroupThread('Room', 't2', roster.slice().reverse())
  assert.deepEqual(h.rpc('session.interrupt').slice(2).map(c => [c.route.connectionId, c.params.session_id]).sort(), targets)
  assertReleased(h)
})

test('pending Stop coalesces while retaining old session until delayed poll also returns', async () => {
  const interruptGate = deferred(), pollGate = deferred()
  const h = await harness(members(1), { interruptGate, pollGate }), pending = drive(h)
  await flush(); await h.advance()
  const first = h.gc.stopGroupThread('Room', 't1', h.roster)
  const second = h.gc.stopGroupThread('Room', 't1', h.roster)
  await flush()
  h.gc.sendToGroupChat('Room', h.roster, '@all resume replacement', 't2'); await h.advance(250)
  assert.equal(h.rpc('session.interrupt').length, 1)
  assert.equal(h.rpc('prompt.submit').length, 1)
  interruptGate.resolve(); await first; await second; await flush()
  assert.ok(h.gc.currentGroupActivity('Room').every(event => event.kind !== 'stopped'),
    'delayed old Stop status never describes the new drive epoch')
  assert.equal(h.leases[0].releases, 0, 'old poll still owns route/session')
  assert.equal(h.rpc('prompt.submit').length, 1)
  pollGate.resolve(); await pending; await flush()
  await h.until(() => h.rpc('prompt.submit').length === 2)
  await h.until(() => !h.room().running)
  assert.equal(h.rpc('session.interrupt').length, 1)
  assert.ok(h.leases.every(l => l.releases === 1))
})

test('late submit rejection after earlier interrupt ACK requires new-generation Stop proof', async () => {
  const submitGate = deferred()
  let interrupts = 0
  const options = { submitGate, missingAck: true, interruptBehavior: session => {
    interrupts++
    if (interrupts === 1) { session.state = 'interrupted'; return { status: 'interrupted' } }
    throw new Error('post-submit interruption unavailable')
  } }
  const h = await harness(members(1), options), pending = drive(h); await flush()
  assert.equal((await h.gc.stopGroupThread('Room', 't1', h.roster)).status, 'unconfirmed')
  submitGate.resolve(); await flush(); await pending
  assert.equal(h.rpc('session.interrupt').length, 2)
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.equal(receipts(h).length, 1)
  assert.equal(coordinator(h).active, 1)
  assert.equal(h.leases[0].releases, 0)
  options.interruptBehavior = undefined
  await h.gc.stopGroupThread('Room', 't1', h.roster)
  assert.equal(receipts(h).length, 1, 'a later ACK cannot reconstruct the lost accepted identity')
  assert.equal(coordinator(h).active, 1)
  assert.equal(h.leases[0].releases, 0)
})

test('Stop during late session acquisition cannot submit; lease closes once after producer settles', async () => {
  const createGate = deferred()
  const h = await harness(members(2), { createGate, interruptError: true }), pending = drive(h); await flush()
  assert.equal((await h.gc.stopGroupThread('Room', 't1', h.roster)).status, 'unconfirmed')
  assert.equal(h.activeLeases(), 2)
  createGate.resolve(); await flush(); await pending
  assert.equal(h.rpc('prompt.submit').length, 0)
  assert.equal(h.rpc('session.interrupt').length, 2)
  assertReleased(h)
})

test('failed Stop after delayed clarification response retains resumed worker until fresh exact terminal', async () => {
  const answerAckGate = deferred(), options = { answerAckGate, interruptError: true, resumeRunning: true }
  const h = await harness(members(1), options), pending = drive(h); await flush()
  const session = [...h.sessions.values()][0]
  session.state = 'waiting'; session.pending = { request_id: 'ask-1', question: 'Choose?' }
  await h.advance()
  const answer = h.gc.answerGroupClarify(Object.values(h.gc.$groupClarify.get())[0], h.roster[0], 'yes')
  await flush()
  const stopping = h.gc.stopGroupThread('Room', 't1', h.roster); await flush(); await h.advance()
  assert.equal(h.leases[0].releases, 0)
  answerAckGate.resolve(); await answer; await stopping; await pending; await flush()
  assertCustody(h, 1, 1)
  assert.equal(h.rpc('session.interrupt').length, 2)
  h.finish('bot1', 'MUST_NOT_PUBLISH')
  await h.gc.harvestStrandedGroupReply('Room', h.roster[0])
  assertReleased(h)
  assert.equal(h.posts().length, 0)
  assert.deepEqual(h.room().watermarks, {})
})

test('reload retains Stop intent: no replay/publication after holds release; exact terminal only reconciles', async () => {
  const hot = await harness(members(1), { interruptError: true }), pending = drive(hot); await flush()
  await settleStop(hot, pending)
  const cold = await harness(members(1), { interruptError: true })
  cold.gc.$groupChats.set(clone(hot.gc.durableGroupChatRooms()))
  for (const [id, session] of hot.sessions) cold.sessions.set(id, clone(session))
  cold.gc.updateGroupChat('Room', room => { room.holds = {}; return room })
  await cold.gc.harvestStrandedGroupReply('Room', cold.roster[0])
  assert.equal(receipts(cold).length, 1)
  assert.equal(cold.rpc('prompt.submit').length, 0)
  cold.finish('bot1', 'MUST_NOT_PUBLISH')
  await cold.gc.harvestStrandedGroupReply('Room', cold.roster[0])
  assert.equal(receipts(cold).length, 0)
  assert.equal(cold.posts().length, 0)
  assert.deepEqual(cold.room().watermarks, {})
  assert.deepEqual(cold.room().consumedInputs, {})
  assert.equal(cold.rpc('prompt.submit').length, 0)
})

test('reload Stop failures retain exact receipt, coalesce concurrent retry and later confirm without replay', async () => {
  const hot = await harness(members(1), { interruptError: true }), pending = drive(hot); await flush()
  await settleStop(hot, pending)
  const options = { interruptError: true }, cold = await harness(members(1), options)
  cold.gc.$groupChats.set(clone(hot.gc.durableGroupChatRooms()))
  for (const [id, session] of hot.sessions) cold.sessions.set(id, clone(session))
  assert.equal((await cold.gc.stopGroupThread('Room', 't1', cold.roster)).status, 'unconfirmed')
  assert.equal(receipts(cold).length, 1)
  const gate = deferred(); options.interruptGate = gate; options.interruptError = false
  const a = cold.gc.stopGroupThread('Room', 't1', cold.roster), b = cold.gc.stopGroupThread('Room', 't1', cold.roster)
  await flush()
  assert.equal(cold.rpc('session.interrupt').length, 2, 'two concurrent retries issue one exact RPC')
  gate.resolve()
  for (const result of [await a, await b]) {
    assert.equal(result.unconfirmed, 0)
    assert.ok(['stopping', 'stopped'].includes(result.status))
  }
  await flush()
  assert.equal(receipts(cold).length, 0)
  assert.equal(cold.rpc('prompt.submit').length, 0)
})

test('unavailable admission identity after reload remains unresolved through Stop; no guessed runtime or replay', async () => {
  const hot = await harness(members(1), { missingAck: true, interruptError: true }), pending = drive(hot); await flush(); await pending
  await hot.gc.stopGroupThread('Room', 't1', hot.roster)
  const cold = await harness(members(1))
  cold.gc.$groupChats.set(clone(hot.gc.durableGroupChatRooms()))
  for (const [id, session] of hot.sessions) cold.sessions.set(id, clone(session))
  assert.equal((await cold.gc.stopGroupThread('Room', 't1', cold.roster)).status, 'unconfirmed')
  cold.gc.updateGroupChat('Room', room => { room.holds = {}; return room })
  await cold.gc.harvestStrandedGroupReply('Room', cold.roster[0])
  assert.equal(receipts(cold).length, 1)
  assert.equal(cold.rpc('prompt.submit').length, 0)
  assert.equal(cold.rpc('session.interrupt').length, 0)
})
