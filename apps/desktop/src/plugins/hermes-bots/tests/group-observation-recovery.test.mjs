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
