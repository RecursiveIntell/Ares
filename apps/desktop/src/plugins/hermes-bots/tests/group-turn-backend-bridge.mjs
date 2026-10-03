// Invoked by the canonical Python test launcher with freshly serialized backend
// ACK/resume results. This exercises the whole plugin through a controlled RPC
// transport; it does not claim a live renderer/WebSocket end-to-end run.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { importGroupTurnPlugin } from './group-turn-test-loader.mjs'

const packet = JSON.parse(readFileSync(process.argv[2], 'utf8'))
const member = { name: 'alpha', title: '' }
let now = 100000
let submitted = false
const calls = []
const atom = initial => {
  let value = initial
  const listeners = new Set()
  return { get: () => value, set: next => { value = next; for (const fn of listeners) fn(value) },
    listen: fn => { listeners.add(fn); return () => listeners.delete(fn) } }
}
const handle = async (method, params) => {
  calls.push({ method, params })
  if (method === 'session.create') return { session_id: 's', stored_session_id: 's', messages: [] }
  if (method === 'prompt.submit') {
    assert.equal(params.session_id, 's')
    submitted = true
    return packet.submit_ack.result
  }
  if (method === 'session.resume' || method === 'session.turn.poll') return submitted ? packet.resume_result : {
    session_id: 's', session_key: 's', running: false, inflight: null, messages: [],
    turn_outcomes: { version: 1, scope: 'process_local', availability: 'available', turns: [] }
  }
  if (method === 'session.interrupt') return {}
  throw new Error(`Unexpected bridge RPC: ${method}`)
}
const gc = await importGroupTurnPlugin({ atom,
  Date: class extends Date { static now() { return now } },
  setTimeout: (fn, delay = 0) => { if (delay === 1200000) return 1; now += delay; fn(); return 0 },
  clearTimeout: () => {},
  host: { request: handle, requestProfile: (_route, method, params) => handle(method, params),
    retainProfile: async () => () => {}, notify: () => {}, notifyError: () => {},
    state: { profile: atom('default'), gateway: atom(null), connectionId: atom('') } }
})
gc.stopGroupChatServerSync()
gc.bindGroupTurnTestStorage({ set: () => {} })
gc.$groupChats.set({ Room: { roomId: 'bridge-room',
  log: [{ id: 'user-1', at: now, from: { kind: 'user', name: 'You' }, text: '@alpha answer', thread: 'thread-1' }],
  watermarks: {}, sessions: {}, sessionOwners: {}, epoch: 1, running: true, holds: {}, members: [member] } })
await gc.runGroupChatRounds('Room', [member], 'thread-1')
const posts = gc.$groupChats.get().Room.log.filter(entry => entry.from.kind === 'member')
if (packet.expected.state === 'complete') {
  assert.equal(posts.length, 1)
  assert.equal(posts[0].text, packet.expected.text)
  assert.deepEqual(posts[0].delivery.accepted_turn, packet.submit_ack.result.accepted_turn)
  // A fresh JSON mirror and a different local row ID must still deduplicate by
  // accepted identity, while the backend terminal remains available on resume.
  const mirror = JSON.parse(JSON.stringify(posts[0]))
  assert.equal(gc.groupChatSyncEntryKey(mirror), gc.groupChatSyncEntryKey({ ...mirror, id: 'other-copy' }))
  assert.equal(gc.appendGroupChatEntry('Room', mirror.from, mirror.text, mirror.thread, undefined, mirror.delivery), posts[0])
} else {
  assert.equal(posts.length, 0)
  const kind = { error: 'failed', interrupted: 'interrupted', unavailable: 'unavailable' }[packet.expected.state]
  assert.equal(gc.currentGroupActivity('Room').filter(entry => entry.kind === kind).length, 1)
}
await gc.runGroupChatRounds('Room', [member], 'thread-1')
await gc.harvestStrandedGroupReply('Room', member)
assert.equal(calls.filter(call => call.method === 'prompt.submit').length, 1)
assert.equal(gc.$groupChats.get().Room.log.filter(entry => entry.from.kind === 'member').length, posts.length)
process.stdout.write(JSON.stringify({ state: packet.expected.state, submits: 1, posts: posts.length,
  accepted_turn: packet.submit_ack.result.accepted_turn }) + '\n')
