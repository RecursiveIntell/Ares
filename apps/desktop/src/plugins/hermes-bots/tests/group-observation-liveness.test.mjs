import assert from 'node:assert/strict'
import test from 'node:test'
import { drive, flush, harness, members } from './stop-custody-harness.mjs'

const submittedProfiles = h => h.rpc('prompt.submit').map(call =>
  [...h.sessions.values()].find(s => s.runtime === call.params.session_id)?.profile)

async function supersede(h) {
  const first = drive(h)
  await h.until(() => h.rpc('prompt.submit').length === Math.min(h.roster.length, 4))
  await h.advance(2000)
  h.gc.sendToGroupChat('Room', h.roster, '@all evaluate NEW_INPUT', 't1')
  await h.advance(300)
  await h.advance(2000)
  return { first }
}

test('same-thread follow-up keeps predecessor observation until latest input can run exactly once', async () => {
  const h = await harness(members(3))
  const { first } = await supersede(h)
  h.roster.forEach(member => h.finish(member.name, 'obsolete answer'))
  await h.until(() => h.rpc('prompt.submit').length === 6)
  await h.until(() => !h.room().running && !Object.keys(h.room().stranded).length)
  await first
  assert.deepEqual(submittedProfiles(h).sort(), ['bot1', 'bot1', 'bot2', 'bot2', 'bot3', 'bot3'])
  assert.equal(h.posts().length, 0, 'obsolete finals and pass sentinels never publish')
  assert.equal(h.activeLeases(), 0)
  assert.equal(h.room().turns.length, 0)
  const latest = h.rpc('prompt.submit').slice(3)
  assert.ok(latest.every(call => call.params.text.includes('NEW_INPUT')))
})

test('four superseded running workers reconcile before an unrelated fifth member is admitted', async () => {
  const h = await harness(members(5))
  const { first } = await supersede(h)
  h.roster.forEach(member => h.finish(member.name, 'obsolete answer'))
  await h.until(() => h.rpc('prompt.submit').length === 9)
  h.finish('bot5', '(pass)')
  await h.until(() => !h.room().running && !Object.keys(h.room().stranded).length)
  await first
  assert.equal(submittedProfiles(h).filter(name => name === 'bot5').length, 1)
  assert.ok(h.maxLeases() <= 4)
  assert.equal(h.posts().length, 0)
  assert.equal(h.activeLeases(), 0)
})

test('a transient exact-turn poll failure retries observation without resubmitting the prompt', async () => {
  const failed = new Set()
  const h = await harness(members(5), { rpcResponse: (_route, method, params) => {
    if (method === 'session.turn.poll' && params.accepted_turn && !failed.has(params.session_id)) {
      failed.add(params.session_id)
      throw new Error('temporary read failure')
    }
  } })
  const running = drive(h)
  await h.until(() => failed.size === 4)
  h.roster.forEach(member => h.finish(member.name, '(pass)'))
  await h.until(() => h.rpc('prompt.submit').length === 5)
  h.finish('bot5', '(pass)')
  await h.until(() => !h.room().running && !Object.keys(h.room().stranded).length)
  await running
  assert.equal(new Set(submittedProfiles(h)).size, 5)
  assert.equal(h.rpc('prompt.submit').length, 5)
  assert.ok(h.rpc('session.turn.poll').length > failed.size)
  assert.equal(h.activeLeases(), 0)
})

test('unavailable observation has a bounded reconciliation path even with four retained workers', async () => {
  let unavailable = true
  const h = await harness(members(5), { resumeProjection: (session, method, projection) => {
    if (method === 'session.turn.poll' && session.ref && unavailable) return {
      ...projection, turn_outcomes: { version: 1, scope: 'process_local', availability: 'unavailable', turns: [] }
    }
    return projection
  } })
  const running = drive(h)
  await h.until(() => h.gc.groupBlockedMembers(h.room(), h.roster).length === 4)
  assert.ok(h.room().turns.filter(turn => turn.phase === 'running').length === 0,
    'retained unknown custody must not be painted as actively thinking')
  const before = h.rpc('session.turn.poll').length
  unavailable = false
  h.roster.forEach(member => h.finish(member.name, '(pass)'))
  await h.until(() => h.rpc('prompt.submit').length === 5)
  h.finish('bot5', '(pass)')
  await h.until(() => !Object.keys(h.room().stranded).length && !h.room().running)
  await running
  assert.ok(h.rpc('session.turn.poll').length > before)
  assert.equal(h.rpc('prompt.submit').length, 5)
  assert.equal(h.activeLeases(), 0)
})

test('permanently unavailable observation preserves exact custody after its bounded retry window', async () => {
  const h = await harness(members(1), { unavailable: true })
  const running = drive(h)
  await h.until(() => h.gc.groupBlockedMembers(h.room(), h.roster).length === 1)
  const receipt = h.room().stranded['local::bot1'].delivery
  for (let i = 0; i < 65; i++) await h.advance(5000)
  await running
  await flush()
  const polls = h.rpc('session.turn.poll').length
  await h.advance(60000)
  assert.equal(h.rpc('session.turn.poll').length, polls, 'automatic recovery is bounded')
  assert.deepEqual(h.room().stranded['local::bot1'].delivery, receipt)
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.equal(h.posts().length, 0)
  assert.equal(h.activeLeases(), 1, 'unavailable is not terminal capacity proof')
})
