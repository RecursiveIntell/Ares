// Every RPC response comes from the real Python dispatcher over the parent
// test's pipes. The whole plugin is imported with inert renderer dependencies.
import assert from 'node:assert/strict'
import { createInterface } from 'node:readline'
import { importGroupTurnPlugin } from './group-turn-test-loader.mjs'

const input = createInterface({ input: process.stdin, terminal: false })[Symbol.asyncIterator]()
const config = JSON.parse((await input.next()).value)
let now = 100000
const calls = [], refs = []
const atom = initial => {
  let value = initial
  const listeners = new Set()
  return { get: () => value, set: next => { value = next; for (const fn of listeners) fn(value) },
    listen: fn => { listeners.add(fn); return () => listeners.delete(fn) } }
}
const handle = async (method, params) => {
  calls.push({ method, params })
  process.stdout.write(JSON.stringify({ method, params }) + '\n')
  const packet = JSON.parse((await input.next()).value)
  if (packet.error) throw Object.assign(new Error(packet.error.message), packet.error)
  if (method === 'prompt.submit' && packet.result.accepted_turn) refs.push(packet.result.accepted_turn)
  return packet.result
}
const gc = await importGroupTurnPlugin({ atom,
  Date: class extends Date { static now() { return now } },
  setTimeout: (fn, delay = 0) => { if (delay === 1200000) return 1; if (delay === 2000) now += delay; fn(); return 0 },
  clearTimeout: () => {},
  host: { request: handle, requestProfile: (_route, method, params) => handle(method, params),
    retainProfile: async () => () => {}, notify: () => {}, notifyError: () => {},
    state: { profile: atom('default'), gateway: atom(null), connectionId: atom('') } }
})
gc.stopGroupChatServerSync()
gc.bindGroupTurnTestStorage({ set: () => {} })
const member = { name: 'alpha', title: '' }
gc.$groupChats.set({ Room: { roomId: 'test-room', members: [member],
  log: [{ id: 'user-1', at: now, from: { kind: 'user', name: 'You' }, text: '@alpha answer', thread: 'thread-1' }],
  watermarks: {}, sessions: { alpha: config.canonical }, sessionOwners: {}, epoch: 1, running: true, holds: {} } })
await gc.runGroupChatRounds('Room', [member], 'thread-1')
const posts = gc.$groupChats.get().Room.log.filter(entry => entry.from.kind === 'member')
if (config.expected === 'complete') {
  assert.equal(posts.length, 1)
  assert.equal(posts[0].text, 'owned answer')
  assert.deepEqual(posts[0].delivery.accepted_turn, refs[0])
} else {
  assert.equal(posts.length, 0)
  const kind = { error: 'failed', interrupted: 'interrupted', unavailable: 'unavailable' }[config.expected]
  assert.equal(gc.currentGroupActivity('Room').filter(entry => entry.kind === kind).length, 1)
}
await gc.runGroupChatRounds('Room', [member], 'thread-1')
await gc.harvestStrandedGroupReply('Room', member)
await gc.harvestStrandedGroupReply('Room', member)
assert.equal(calls.filter(call => call.method === 'prompt.submit').length, 1)
assert.equal(gc.$groupChats.get().Room.log.filter(entry => entry.from.kind === 'member').length, posts.length)
const polls = calls.filter(call => call.method === 'session.turn.poll')
assert.ok(polls.length || config.queued)
for (const call of polls) {
  assert.equal(call.params.session_id, config.runtime)
  assert.deepEqual(call.params.accepted_turn, refs[0])
}
// Preparation may resume the canonical key. Observation never resumes runtime.
assert.ok(calls.filter(call => call.method === 'session.resume').every(call => call.params.session_id === config.canonical))
process.stdout.write(JSON.stringify({ done: true, expected: config.expected, posts: posts.length,
  submits: 1, polls: polls.length, elapsed: now - 100000, accepted_turn: refs[0] || null }) + '\n')
process.exit(0)
