import assert from 'node:assert/strict'
import test from 'node:test'
import { deferred, drive, flush, harness, members } from './stop-custody-harness.mjs'

async function expiredObservation() {
  let available = false
  const h = await harness(members(1), { resumeProjection: (session, method, projection) =>
    method === 'session.turn.poll' && session.ref && !available
      ? { ...projection, turn_outcomes: { version: 1, scope: 'process_local', availability: 'unavailable', turns: [] } }
      : projection })
  const running = drive(h)
  await h.until(() => h.gc.groupBlockedMembers(h.room(), h.roster).length === 1)
  for (let i = 0; i < 65; i++) await h.advance(5000)
  await running
  return { h, restore: () => { available = true } }
}

const nodes = tree => {
  const result = []
  const walk = value => {
    if (Array.isArray(value)) value.forEach(walk)
    else if (value && typeof value === 'object') { result.push(value); walk(value.props?.children) }
  }
  walk(tree)
  return result
}

test('explicit status check recovers an exact completed reply after automatic observation expires', async () => {
  const { h, restore } = await expiredObservation()
  assert.equal(typeof h.gc.checkGroupTurnStatus, 'function')
  const resumes = h.rpc('session.resume').length
  restore(); h.finish('bot1', 'retained exact reply')
  await h.gc.checkGroupTurnStatus('Room', h.roster)
  await flush()
  assert.deepEqual(h.posts().map(entry => entry.text), ['retained exact reply'])
  assert.equal(h.rpc('session.resume').length, resumes)
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.equal(h.activeLeases(), 0)
  await h.gc.checkGroupTurnStatus('Room', h.roster)
  assert.equal(h.posts().length, 1)
})

test('explicit status check preserves a lost runtime receipt without adopting or replaying its session', async () => {
  const { h } = await expiredObservation()
  assert.equal(typeof h.gc.checkGroupTurnStatus, 'function')
  const receipt = structuredClone(h.room().stranded['local::bot1'].delivery)
  h.sessions.clear()
  const resumes = h.rpc('session.resume').length
  await h.gc.checkGroupTurnStatus('Room', h.roster)
  assert.deepEqual(h.room().stranded['local::bot1'].delivery, receipt)
  assert.equal(h.rpc('session.resume').length, resumes)
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.equal(h.posts().length, 0)
})

test('blocked notice offers exact status observation separately from a new-session transition', async () => {
  const { h } = await expiredObservation()
  let checks = 0, creations = 0
  const tree = h.gc.GroupBlockedNotice({ room: h.room(), members: h.roster,
    onCheck: () => { checks++ }, onCreate: () => { creations++ } })
  const buttons = nodes(tree).filter(node => typeof node.props?.onClick === 'function')
  const check = buttons.find(node => node.props.children === 'Check earlier turn status')
  assert.ok(check, 'status observation is a real user action, not a prompt resend')
  check.props.onClick()
  assert.equal(checks, 1); assert.equal(creations, 0)
  assert.match(JSON.stringify(tree), /reconciliation/)
  assert.ok(!JSON.stringify(tree).includes('are thinking'))
})

test('a stop-requested marker ends the collector instead of observing as superseded work', async () => {
  const h = await harness(members(1))
  const running = drive(h)
  await h.until(() => h.rpc('prompt.submit').length === 1)
  await h.advance(2000)
  const marker = h.room().stranded['local::bot1']
  marker.stop_requested = true // cold/reconciliation Stop custody owns this receipt
  const before = h.rpc('session.turn.poll').length
  let settled = false
  void running.then(() => { settled = true }, () => { settled = true })
  for (let i = 0; i < 8 && !settled; i++) await h.advance(2000)
  assert.equal(settled, true,
    'the live collector must hand a stop-requested receipt to reconciliation, not keep observing it')
  assert.ok(h.rpc('session.turn.poll').length - before <= 2)
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.equal(h.posts().length, 0)
})

test('room replacement under the same name cannot be reconciled by the old lifetime timer', async () => {
  const { h } = await expiredObservation()
  const polls = h.rpc('session.turn.poll').length
  const old = h.room()
  h.gc.$groupChats.set({ Room: { ...old, roomId: 'room-replaced', coordinationId: 'replacement-token',
    stranded: {}, log: [...old.log] } })
  await h.advance(60000)
  assert.equal(h.rpc('session.turn.poll').length, polls,
    'a replacement lifetime is never polled with the old lifetime\'s receipt')
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.equal(h.posts().length, 0)
})

test('a superseded still-running turn is observed only up to the hard cap, then handed to bounded reconciliation', async () => {
  const h = await harness(members(1))
  const first = drive(h)
  await h.until(() => h.rpc('prompt.submit').length === 1)
  await h.advance(2000)
  h.gc.sendToGroupChat('Room', h.roster, '@all newer instruction', 't1')
  let settled = false
  void first.then(() => { settled = true }, () => { settled = true })
  const start = h.rpc('session.turn.poll').length
  // 20 minute hard cap at 2s polls is 600 reads; stay well past it.
  for (let i = 0; i < 700 && !settled; i++) await h.advance(2000)
  const collectorReads = h.rpc('session.turn.poll').length - start
  assert.ok(collectorReads <= 620, `collector reads are capped near the hard cap, got ${collectorReads}`)
  const afterCap = h.rpc('session.turn.poll').length
  for (let i = 0; i < 80; i++) await h.advance(5000)
  assert.ok(h.rpc('session.turn.poll').length - afterCap <= 61,
    'automatic reconciliation is bounded to its retry budget')
  assert.equal(h.rpc('prompt.submit').length, 1, 'the newest input is not admitted without terminal evidence')
  assert.equal(h.posts().length, 0)
})

test('observation disposal cancels scheduled retries without releasing uncertain custody', async () => {
  const h = await harness(members(1), { unavailable: true })
  const running = drive(h)
  await h.until(() => h.gc.groupBlockedMembers(h.room(), h.roster).length === 1)
  await running
  assert.equal(typeof h.gc.stopGroupTurnObservation, 'function')
  const receipt = structuredClone(h.room().stranded['local::bot1'].delivery)
  const polls = h.rpc('session.turn.poll').length
  h.gc.stopGroupTurnObservation()
  await h.advance(60000)
  assert.equal(h.rpc('session.turn.poll').length, polls)
  assert.deepEqual(h.room().stranded['local::bot1'].delivery, receipt)
  assert.equal(h.activeLeases(), 1)
  assert.equal(h.rpc('prompt.submit').length, 1)
})

test('manual exact status checks coalesce while the same room observation is pending', async () => {
  const gate = deferred(); let block = false
  const h = await harness(members(1), { unavailable: true, rpcResponse: (_route, method) => {
    if (method === 'session.turn.poll' && block) return gate.promise
  } })
  const running = drive(h); await h.until(() => h.gc.groupBlockedMembers(h.room(), h.roster).length === 1)
  await running
  assert.equal(typeof h.gc.checkGroupTurnStatus, 'function')
  block = true
  const polls = h.rpc('session.turn.poll').length
  const first = h.gc.checkGroupTurnStatus('Room', h.roster)
  const second = h.gc.checkGroupTurnStatus('Room', h.roster)
  await flush()
  assert.equal(h.rpc('session.turn.poll').length, polls + 1)
  const marker = h.room().stranded['local::bot1']
  gate.resolve({ session_id: marker.delivery.accepted_turn.session_id,
    turn_outcomes: { version: 1, scope: 'process_local', availability: 'unavailable', turns: [] } })
  await Promise.all([first, second])
  assert.equal(h.rpc('prompt.submit').length, 1)
})
