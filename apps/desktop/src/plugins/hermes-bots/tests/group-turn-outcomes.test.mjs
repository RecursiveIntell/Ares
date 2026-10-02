import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { importGroupTurnPlugin } from './group-turn-test-loader.mjs'

const ALPHA = { name: 'alpha', title: '' }
const BETA = { name: 'beta', title: '' }
const final = (text, status = 'complete', extra = {}) => ({ text, status, ...extra })
const wire = (ref, state = 'complete', finalized = [final('finished')], reason) => ({
  version: 1, scope: 'process_local', availability: 'available',
  turns: [{ accepted_turn: ref, state, finalized, ...(reason ? { reason } : {}) }]
})
const history = (length, text = 'old copied answer') => Array.from({ length }, (_, i) => ({
  role: i % 2 ? 'assistant' : 'user', content: text, row_id: i + 1, timestamp: 10
}))

async function harness(scripts = {}, { baseline = 0, onPoll, onSubmit, ack, connectionId = '',
  outcomeRoute = 'compute_host', members = [ALPHA, BETA], submitError, queuedDrives = false, runtimeId,
  clarifyResult = { status: 'ok' } } = {}) {
  let now = 100000
  let activeConnection = connectionId
  let gc
  let releases = 0
  const collectorTimers = new Map()
  const driveTimers = new Map()
  let timerSequence = 0
  const sessions = new Map()
  const calls = []
  const atom = initial => {
    let value = initial
    const listeners = new Set()
    return { get: () => value, set: next => { value = next; for (const fn of listeners) fn(value) },
      listen: fn => { listeners.add(fn); return () => listeners.delete(fn) } }
  }
  const handle = async (route, method, params) => {
    calls.push({ route: route && { ...route }, method, params: { ...params }, at: now })
    if (method === 'session.create') {
      const session = { profile: params.profile, runtime: runtimeId || `runtime-${params.profile}`,
        stored: `stored-${params.profile}`, title: params.title,
        messages: history(baseline), polls: 0, totalPolls: 0, submits: 0 }
      sessions.set(params.profile, session)
      return { session_id: session.runtime, stored_session_id: session.stored, messages: [...session.messages] }
    }
    const session = [...sessions.values()].find(s => s.runtime === params.session_id ||
      s.stored === params.session_id || (params.profile === s.profile && params.session_id === s.title))
    if (!session) throw Object.assign(new Error('session not found'), { code: 4007 })
    if (method === 'prompt.submit') {
      session.submits++
      session.polls = 0
      if (submitError) throw submitError(session)
      session.ref = { request_id: `owned-${session.profile}-${session.submits}`, session_id: session.runtime,
        route: outcomeRoute, host_boot_id: outcomeRoute === 'compute_host' ? 'boot-A' : null }
      onSubmit?.(session, gc)
      const response = ack ? ack(session, gc) : { accepted_turn: { ...session.ref } }
      if (response.accepted_turn) session.ref = { ...response.accepted_turn }
      return response
    }
    if (method === 'session.interrupt') return {}
    if (method === 'clarify.respond') return clarifyResult
    if (method.endsWith('.attach') || method.endsWith('.attach_bytes')) return {}
    if (method !== 'session.resume' && method !== 'session.turn.poll') throw new Error(`unexpected RPC: ${method}`)
    if (method === 'session.turn.poll') {
      assert.equal(params.session_id, session.runtime, 'poll accepts runtime namespace only')
      assert.deepEqual(params.accepted_turn, session.ref)
    }
    let snapshot = {}
    if (method === 'session.turn.poll') {
      session.polls++
      session.totalPolls++
      const script = scripts[session.profile] || [s => ({ turn_outcomes: wire(s.ref, 'complete', [final('(pass)')]) })]
      const step = script[Math.min(session.polls - 1, script.length - 1)]
      snapshot = typeof step === 'function' ? await step(session, gc) : step
      onPoll?.(session, gc)
    }
    return { session_id: session.runtime, session_key: session.stored, running: false, inflight: null,
      messages: [...session.messages], ...snapshot }
  }
  gc = await importGroupTurnPlugin({
    atom, Date: class extends Date { static now() { return now } },
    setTimeout: (fn, delay = 0) => {
      if (delay === 1200000) { const id = ++timerSequence; collectorTimers.set(id, fn); return id }
      if (delay === 250 && queuedDrives) { const id = ++timerSequence; driveTimers.set(id, fn); return id }
      if (delay === 2000) now += delay
      fn(); return 0
    },
    clearTimeout: id => { collectorTimers.delete(id); driveTimers.delete(id) },
    host: { request: (method, params) => handle(null, method, params),
      requestProfile: (route, method, params) => handle(route, method, params),
      retainProfile: async () => () => { releases++ },
      state: { profile: atom('default'), gateway: atom(null),
        connectionId: { get: () => activeConnection, listen: () => () => undefined } },
      notify: () => undefined, notifyError: () => undefined }
  })
  gc.stopGroupChatServerSync()
  const storageWrites = new Map()
  gc.bindGroupTurnTestStorage({ set: (key, value) => { storageWrites.set(key, clone(value)) } })
  gc.$groupChats.set({ Room: { roomId: 'room-A', log: [{ id: 'user-1', at: now,
    from: { kind: 'user', name: 'You' }, text: '@all answer', thread: 'thread-1' }],
    watermarks: {}, sessions: {}, sessionOwners: {}, epoch: 1, running: true, holds: {}, members } })
  return { gc, sessions, calls, storageWrites, releases: () => releases,
    collectorTimers: () => collectorTimers.size,
    expireCollectorWait: () => { for (const fn of [...collectorTimers.values()]) fn() },
    driveTimers: () => driveTimers.size,
    runQueuedDrives: () => { const batch = [...driveTimers.values()]; driveTimers.clear(); for (const fn of batch) fn() },
    switchConnection: next => { activeConnection = next },
    rpc: method => calls.filter(call => call.method === method),
    elapsed: () => now - (calls.find(c => c.method === 'prompt.submit')?.at ?? 100000) }
}

const run = (h, member = ALPHA, deliveryResult) => h.gc.runGroupChatMemberTurn(
  'Room', member, 'prompt', 'thread-1', undefined, deliveryResult)
const room = h => h.gc.$groupChats.get().Room
const posts = h => room(h).log.filter(entry => entry.from.kind === 'member')
const clone = value => JSON.parse(JSON.stringify(value))
const setRoom = (h, changes) => h.gc.$groupChats.set({ Room: { ...room(h), ...changes } })

test('whole-plugin clarification answers carry the runtime owner and an unavailable snapshot retains its card', async () => {
  const h = await harness()
  await run(h)
  h.gc.syncGroupClarify('Room', ALPHA, { session_id: 'runtime-alpha', pending_clarify: {
    request_id: 'child-clarify', question: 'Confirm?' } })
  const entry = h.gc.$groupClarify.get()['Room::alpha']
  assert.ok(entry)
  assert.equal(h.gc.syncGroupClarify('Room', ALPHA, { pending_clarify_unavailable: true }), true)
  assert.equal(h.gc.$groupClarify.get()['Room::alpha'], entry)
  await h.gc.answerGroupClarify(entry, ALPHA, 'yes')
  assert.deepEqual(h.rpc('clarify.respond').map(c => c.params), [{
    session_id: 'runtime-alpha', request_id: 'child-clarify', answer: 'yes' }])
  assert.equal(h.gc.$groupClarify.get()['Room::alpha'], undefined)
})

for (const status of ['expired', 'conflict', undefined]) {
  for (const batch of [false, true]) {
    test(`whole-plugin ${batch ? 'batch' : 'single'} clarification ${status || 'unknown'} acknowledgement retains its card and stops sending`, async () => {
      const h = await harness({}, { clarifyResult: { status } })
      await run(h)
      h.gc.syncGroupClarify('Room', ALPHA, { session_id: 'runtime-alpha', pending_clarify: {
        request_id: 'child-clarify', question: 'Confirm?',
        ...(batch ? { questions: [{ qid: 'q0', question: 'First?' }, { qid: 'q1', question: 'Second?' }] } : {}) } })
      const entry = h.gc.$groupClarify.get()['Room::alpha']
      await assert.rejects(h.gc.answerGroupClarify(entry, ALPHA, batch ? { q0: 'one', q1: 'two' } : 'yes'))
      assert.equal(h.gc.$groupClarify.get()['Room::alpha'], entry)
      assert.equal(h.rpc('clarify.respond').length, 1)
      assert.equal(posts(h).length, 0)
    })
  }
}

test('292 → 11 → 15 compacted display history delivers only its matching owned final', async t => {
  const h = await harness({ alpha: [
    s => ({ messages: history(11), turn_outcomes: wire(s.ref, 'running', [final('not yet terminal')]) }),
    s => ({ messages: history(15), turn_outcomes: wire(s.ref, 'complete', [final('fresh result'), final('(pass)')]) })
  ] }, { baseline: 292 })
  const receipt = {}
  assert.equal(await run(h, ALPHA, receipt), 'fresh result')
  assert.equal(h.sessions.get('alpha').polls, 2)
  assert.equal(h.elapsed(), 4000)
  assert.equal(receipt.value.accepted_turn.request_id, 'owned-alpha-1')
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.equal(h.rpc('session.turn.poll').at(-1).params.session_id, 'runtime-alpha')
  assert.equal(room(h).stranded.alpha, undefined)
  t.diagnostic(JSON.stringify({ preHistory: 292, compactedHistory: 11, finalHistory: 15,
    virtualElapsedMs: h.elapsed(), providerCalls: 0 }))
})

for (const [name, messages] of [
  ['unchanged old history', history(292)],
  ['protected old rows copied with larger IDs and fresh timestamps', history(15).map((row, i) => ({
    ...row, row_id: 10000 + i, timestamp: 999999, content: 'old copied answer' }))]
]) {
  test(`${name} never becomes a fresh reply`, async () => {
    const h = await harness({ alpha: [s => ({ messages, turn_outcomes: wire(s.ref, 'complete', [final('(pass)')]) })] },
      { baseline: 292 })
    assert.equal(await run(h), '(pass)')
    assert.equal(posts(h).length, 0)
  })
}

test('a finalized bubble in a running continuation never settles the outer turn', async () => {
  const h = await harness({ alpha: [
    s => ({ turn_outcomes: wire(s.ref, 'running', [final('answer before continuation')]) }),
    s => ({ turn_outcomes: wire(s.ref, 'running', [final('answer before continuation'), final('(pass)')]) }),
    s => ({ turn_outcomes: wire(s.ref, 'complete', [final('answer before continuation'), final('(pass)')]) })
  ] })
  assert.equal(await run(h), 'answer before continuation')
  assert.equal(h.sessions.get('alpha').polls, 3)
  assert.equal(h.rpc('prompt.submit').length, 1)
})

test('complete selection uses only complete entries, newest substantive before later pass', async () => {
  const h = await harness({ alpha: [s => ({ turn_outcomes: wire(s.ref, 'complete', [
    final('first answer'), final('new answer'), final('(pass)'),
    final('failed partial', 'error', { error: 'failed' }), final('stopped partial', 'interrupted')
  ]) })] })
  assert.equal(await run(h), 'new answer')
})

for (const [name, snapshot] of [
  ['missing projection', () => ({})],
  ['unavailable process-local projection', () => ({ turn_outcomes: { version: 1, scope: 'process_local', availability: 'unavailable', turns: [] } })],
  ['missing accepted outcome', () => ({ turn_outcomes: { version: 1, scope: 'process_local', availability: 'available', turns: [] } })],
  ['backend restart', s => ({ turn_outcomes: wire({ ...s.ref, host_boot_id: 'boot-B' }) })],
  ['wrong request', s => ({ turn_outcomes: wire({ ...s.ref, request_id: 'different' }) })],
  ['wrong runtime', s => ({ turn_outcomes: wire({ ...s.ref, session_id: 'other-runtime' }) })],
  ['wrong route', s => ({ turn_outcomes: wire({ ...s.ref, route: 'inline', host_boot_id: null }) })],
  ['runtime rebound', s => ({ session_id: 'replacement-runtime', turn_outcomes: wire(s.ref) })],
  ['future version', s => ({ turn_outcomes: { ...wire(s.ref), version: 2 } })],
  ['wrong scope', s => ({ turn_outcomes: { ...wire(s.ref), scope: 'durable' } })],
  ['ambiguous duplicate identities', s => { const value = wire(s.ref); return { turn_outcomes: { ...value, turns: [...value.turns, ...value.turns] } } }],
  ['empty final window', s => ({ turn_outcomes: wire(s.ref, 'complete', []) })],
  ['empty final text', s => ({ turn_outcomes: wire(s.ref, 'complete', [final('  ')]) })],
  ['non-contract final status', s => ({ turn_outcomes: wire(s.ref, 'complete', [final('wrong alias', 'success')]) })],
  ['explicit unavailable turn', s => ({ turn_outcomes: wire(s.ref, 'unavailable', [], 'evicted') })]
]) {
  test(`${name} is honest, retains its marker, and never uses attractive old history`, async () => {
    const h = await harness({ alpha: [s => ({ messages: [{ role: 'assistant', content: 'tempting stale text', row_id: 99999 }],
      ...snapshot(s) })] })
    await h.gc.runGroupChatRounds('Room', [ALPHA], 'thread-1')
    assert.equal(posts(h).length, 0)
    const ref = clone(room(h).stranded.alpha.delivery.accepted_turn)
    assert.equal(ref.request_id, 'owned-alpha-1')
    assert.equal(h.gc.currentGroupActivity('Room').filter(e => e.kind === 'unavailable').length, 1)
    assert.equal(room(h).watermarks['thread-1::alpha'], undefined)
    await h.gc.harvestStrandedGroupReply('Room', ALPHA)
    await h.gc.runGroupChatRounds('Room', [ALPHA], 'thread-1')
    assert.deepEqual(room(h).stranded.alpha.delivery.accepted_turn, ref)
    assert.equal(h.rpc('prompt.submit').length, 1)
    assert.equal(posts(h).length, 0)
    assert.equal(h.gc.currentGroupActivity('Room').filter(e => e.kind === 'unavailable').length, 1)
  })
}

for (const [name, ack] of [
  ['legacy ACK without identity', () => ({ status: 'streaming' })],
  ['queued ACK without admission', () => ({ status: 'queued' })],
  ['wrong admitted runtime', s => ({ accepted_turn: { ...s.ref, session_id: 'wrong-runtime' } })],
  ['invalid inline boot identity', s => ({ accepted_turn: { ...s.ref, route: 'inline', host_boot_id: 'boot-A' } })],
  ['missing host boot identity', s => ({ accepted_turn: { ...s.ref, host_boot_id: null } })]
]) {
  test(`${name} remains unresolved and is never resubmitted`, async () => {
    const h = await harness({}, { ack })
    await h.gc.runGroupChatRounds('Room', [ALPHA], 'thread-1')
    assert.equal(room(h).stranded.alpha.delivery.accepted_turn, null)
    await h.gc.harvestStrandedGroupReply('Room', ALPHA)
    await h.gc.runGroupChatRounds('Room', [ALPHA], 'thread-1')
    assert.equal(h.rpc('prompt.submit').length, 1)
    assert.equal(posts(h).length, 0)
    assert.equal(h.gc.currentGroupActivity('Room').filter(e => e.kind === 'unavailable').length, 1)
  })
}

test('inline admission has an exact null boot identity', async () => {
  const h = await harness({ alpha: [s => ({ turn_outcomes: wire(s.ref) })] }, { outcomeRoute: 'inline' })
  const delivery = {}
  assert.equal(await run(h, ALPHA, delivery), 'finished')
  assert.equal(delivery.value.accepted_turn.host_boot_id, null)
})

for (const terminal of ['error', 'interrupted']) {
  test(`${terminal} never publishes completed or partial bubbles and advances the next member once`, async () => {
    const h = await harness({ alpha: [s => ({ running: false,
      inflight: { status: 'error', streaming: false },
      turn_outcomes: wire(s.ref, terminal, [final('partial result'), final('failure', terminal, { error: 'test reason' })]) })]
    })
    await h.gc.runGroupChatRounds('Room', [ALPHA, BETA], 'thread-1')
    assert.equal(posts(h).length, 0)
    const events = h.gc.currentGroupActivity('Room').filter(e => e.kind === (terminal === 'error' ? 'failed' : 'interrupted'))
    assert.equal(events.length, 1)
    assert.equal(events[0].reason, 'test reason')
    assert.deepEqual(h.rpc('prompt.submit').map(c => c.params.session_id), ['runtime-alpha', 'runtime-beta'])
    assert.equal(room(h).stranded.alpha, undefined)
    await h.gc.runGroupChatRounds('Room', [ALPHA, BETA], 'thread-1')
    assert.equal(h.rpc('prompt.submit').length, 2)
  })
}

for (const [field, kind] of [['pending_clarify', 'clarify'], ['pending_approval', 'approval']]) {
  test(`${kind} retains request/session ownership beyond the base timeout without auto-answer`, async () => {
    let mirrored
    const pending = { request_id: 'human-request', question: 'Choose?', description: 'Approve?', command: 'sensitive' }
    const h = await harness({ alpha: [s => ({ [field]: pending, turn_outcomes: wire(s.ref, 'waiting', []) }),
      ...Array.from({ length: 99 }, () => s => ({ [field]: pending, turn_outcomes: wire(s.ref, 'waiting', []) })),
      s => ({ turn_outcomes: wire(s.ref) })] }, { onPoll: (s, gc) => {
      if (s.polls === 2) mirrored = gc.$groupClarify.get()['Room::alpha']
      if (s.polls === 100) assert.equal(gc.$groupClarify.get()['Room::alpha'], mirrored)
    } })
    assert.equal(await run(h), 'finished')
    assert.equal(h.elapsed(), 202000)
    assert.equal(mirrored.kind, kind)
    assert.equal(mirrored.requestId, 'human-request')
    assert.equal(mirrored.sessionId, 'runtime-alpha')
    assert.equal(h.rpc('clarify.respond').length, 0)
    assert.equal(h.rpc('approval.respond').length, 0)
    assert.equal(h.rpc('prompt.submit').length, 1)
  })
}

test('distinct accepted turns may publish identical text; duplicate outcome polls publish once', async () => {
  const h = await harness({ alpha: [s => ({ turn_outcomes: wire(s.ref, 'complete', [final('identical answer')]) })] })
  await h.gc.runGroupChatRounds('Room', [ALPHA], 'thread-1')
  const first = posts(h)[0]
  setRoom(h, { epoch: 2, running: true, log: [...room(h).log, { id: 'user-2', at: 101000,
    from: { kind: 'user', name: 'You' }, text: '@alpha answer again', thread: 'thread-1' }] })
  await h.gc.runGroupChatRounds('Room', [ALPHA], 'thread-1')
  assert.equal(posts(h).length, 2)
  assert.notDeepEqual(posts(h)[1].delivery.accepted_turn, first.delivery.accepted_turn)
  const duplicate = h.gc.appendGroupChatEntry('Room', first.from, first.text, 'thread-1', undefined, clone(first.delivery))
  assert.equal(duplicate, first)
  assert.equal(posts(h).length, 2)
  await h.gc.runGroupChatRounds('Room', [ALPHA], 'thread-1')
  assert.equal(h.rpc('prompt.submit').length, 2)
  const snapshot = h.gc.groupChatSyncSnapshot()
  const mirrored = Object.values(snapshot.rooms)[0].log.filter(e => e.from.kind === 'member')
  assert.equal(mirrored.length, 2)
  assert.deepEqual(mirrored[0].delivery, first.delivery)
  assert.equal(h.gc.groupChatSyncEntryKey(mirrored[0]), h.gc.groupChatSyncEntryKey({ ...first, id: 'different-copy-id' }))
})

async function strandedHarness({ onPoll, member = ALPHA } = {}) {
  let settle = false
  const h = await harness({ [member.route?.targetProfile || member.name]: [s => ({ messages: history(11),
    turn_outcomes: settle ? wire(s.ref, 'complete', [final('late owned result'), final('(pass)')]) : wire(s.ref, 'running', []) })]
  }, { baseline: 292, onPoll, members: [member] })
  assert.equal(await run(h, member), null)
  assert.equal(h.elapsed(), 1200000)
  settle = true
  return h
}

test('concurrent stranded harvests and a serialized marker deliver the matching outcome once', async () => {
  const h = await strandedHarness()
  // JSON roundtrip models a window reload: process-local proof stays backend-owned.
  setRoom(h, { stranded: clone(room(h).stranded) })
  await Promise.all([1, 2, 3].map(() => h.gc.harvestStrandedGroupReply('Room', ALPHA)))
  await h.gc.harvestStrandedGroupReply('Room', ALPHA)
  assert.equal(posts(h).length, 1)
  assert.equal(posts(h)[0].text, 'late owned result')
  assert.equal(posts(h)[0].delivery.accepted_turn.request_id, 'owned-alpha-1')
  assert.equal(room(h).stranded.alpha, undefined)
  assert.equal(room(h).watermarks['thread-1::alpha'], 2)
  assert.equal(h.rpc('prompt.submit').length, 1)
})

test('marker replacement while a harvest is awaiting RPC cannot consume or publish either result', async () => {
  let replacement
  const h = await strandedHarness({ onPoll: (s, gc) => {
    if (!replacement) return
    const current = gc.$groupChats.get().Room
    gc.$groupChats.set({ Room: { ...current, stranded: { alpha: replacement } } })
  } })
  replacement = { ...clone(room(h).stranded.alpha), thread: 'new-thread',
    delivery: { ...clone(room(h).stranded.alpha.delivery), accepted_turn: {
      ...room(h).stranded.alpha.delivery.accepted_turn, request_id: 'newer-owned-turn' } } }
  await h.gc.harvestStrandedGroupReply('Room', ALPHA)
  assert.equal(room(h).stranded.alpha, replacement)
  assert.equal(posts(h).length, 0)
})

test('legacy number and length-only markers remain unresolved without history fallback', async () => {
  for (const marker of [0, { before: 292, thread: 'thread-1' }]) {
    const h = await harness()
    setRoom(h, { stranded: { alpha: marker } })
    await h.gc.harvestStrandedGroupReply('Room', ALPHA)
    assert.ok(Object.hasOwn(room(h).stranded, 'alpha'))
    assert.equal(posts(h).length, 0)
    assert.equal(h.rpc('prompt.submit').length, 0)
    assert.equal(h.rpc('session.resume').length, 0)
    assert.equal(h.gc.currentGroupActivity('Room').filter(e => e.kind === 'unavailable').length, 1)
  }
})

test('source and target-profile mismatch never query another owner or consume a marker', async () => {
  const member = { ...ALPHA, sourceScoped: true, connectionId: 'mini', connectionKind: 'remote',
    route: { connectionId: 'mini', mode: 'remote', profile: 'alpha', targetProfile: 'default' } }
  const h = await strandedHarness({ member })
  const marker = room(h).stranded['mini::alpha']
  const callsBefore = h.calls.length
  const changed = { ...member, route: { ...member.route, targetProfile: 'wrong-profile' } }
  await h.gc.harvestStrandedGroupReply('Room', changed)
  assert.equal(h.calls.length, callsBefore)
  assert.deepEqual(room(h).stranded['mini::alpha'].delivery, marker.delivery)
  assert.equal(posts(h).length, 0)
})

test('local RPC route is captured across a connection switch and lease releases once', async () => {
  let h
  h = await harness({ alpha: [s => ({ turn_outcomes: wire(s.ref) })] }, {
    connectionId: 'pc', onSubmit: () => h.switchConnection('other-machine')
  })
  const delivery = {}
  assert.equal(await run(h, ALPHA, delivery), 'finished')
  assert.equal(delivery.value.owner.connectionId, 'pc')
  assert.equal(h.calls.every(call => call.route?.connectionId === 'pc'), true)
  assert.equal(h.releases(), 1)
  assert.equal(room(h).sessions.alpha, 'stored-alpha')
})

for (const terminal of ['complete', 'error']) {
  test(`a stale epoch discards ${terminal} without watermark advance or next submit`, async () => {
    const h = await harness({ alpha: [s => ({ turn_outcomes: wire(s.ref, terminal, [final('obsolete', terminal)], 'old error') })] },
      { onPoll: (_s, gc) => {
        const current = gc.$groupChats.get().Room
        gc.$groupChats.set({ Room: { ...current, epoch: 2, log: [...current.log,
          { id: 'user-new', from: { kind: 'user', name: 'You' }, text: 'new intent', thread: 'thread-1' }] } })
      } })
    await h.gc.runGroupChatRounds('Room', [ALPHA, BETA], 'thread-1')
    assert.equal(posts(h).length, 0)
    assert.equal(room(h).watermarks['thread-1::alpha'], undefined)
    assert.equal(h.rpc('prompt.submit').length, 1)
  })
}

test('stale stranded result is discarded; cross-thread newer intent still permits owned delivery', async () => {
  for (const [thread, expectedPosts] of [['thread-1', 0], ['other-thread', 1]]) {
    const h = await strandedHarness()
    setRoom(h, { epoch: 2, log: [...room(h).log, { id: 'new-user', from: { kind: 'user', name: 'You' },
      text: 'new intent', thread }] })
    await h.gc.harvestStrandedGroupReply('Room', ALPHA)
    assert.equal(posts(h).length, expectedPosts)
    assert.equal(room(h).stranded.alpha, undefined)
    assert.equal(h.rpc('prompt.submit').length, 1)
  }
})

test('explicit stop exits the collector and never collects its late complete outcome', async () => {
  let stopped = false
  const h = await harness({ alpha: [async (s, gc) => {
    if (!stopped) { stopped = true; await gc.stopGroupThread('Room', 'thread-1', [ALPHA, BETA]) }
    return { turn_outcomes: wire(s.ref, 'running', [final('late candidate')]) }
  }] })
  assert.equal(await run(h), null)
  assert.equal(room(h).stranded.alpha, undefined)
  assert.equal(posts(h).length, 0)
  assert.equal(h.rpc('prompt.submit').length, 1)
})

test('uncertain submit transport failure keeps an admission marker and never retries user text', async () => {
  const h = await harness({}, { submitError: () => new Error('transport lost after possible acceptance') })
  await assert.rejects(run(h), /transport lost/)
  assert.equal(room(h).stranded.alpha.delivery.accepted_turn, null)
  await h.gc.harvestStrandedGroupReply('Room', ALPHA)
  await h.gc.runGroupChatRounds('Room', [ALPHA], 'thread-1')
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.equal(posts(h).length, 0)
})

test('durable storage and an actual mirror roundtrip retain delivery proof and suppress a copied ID', async () => {
  const h = await harness({ alpha: [s => ({ turn_outcomes: wire(s.ref) })] }, { members: [ALPHA],
    onPoll: s => assert.deepEqual(h.storageWrites.get('group-chats').Room.stranded.alpha.delivery.accepted_turn, s.ref) })
  await h.gc.runGroupChatRounds('Room', [ALPHA], 'thread-1')
  const durable = clone(h.storageWrites.get('group-chats'))
  const receipt = durable.Room.log.find(entry => entry.from.kind === 'member').delivery
  assert.equal(receipt.accepted_turn.request_id, 'owned-alpha-1')
  const mirror = clone(h.gc.groupChatSyncSnapshot())
  Object.values(mirror.rooms)[0].log.find(entry => entry.delivery).id = 'new-physical-copy-id'
  const hydrated = h.gc.mergeRemoteGroupChatSnapshotIntoRooms(mirror, durable)
  assert.equal(hydrated.Room.log.filter(entry => entry.delivery).length, 1)
  const cold = await harness()
  cold.gc.$groupChats.set(clone(hydrated))
  cold.gc.appendGroupChatEntry('Room', { kind: 'member', name: 'alpha' }, 'finished', 'thread-1', undefined, clone(receipt))
  assert.equal(posts(cold).length, 1)
  assert.deepEqual(cold.gc.durableGroupChatRooms(cold.gc.$groupChats.get()).Room.log.find(entry => entry.delivery).delivery, receipt)
})

test('a live cross-thread turn settles before the same member is admitted for the newer thread', async () => {
  let releaseFirst, enteredFirst
  const entered = new Promise(resolve => { enteredFirst = resolve })
  const gate = new Promise(resolve => { releaseFirst = resolve })
  const h = await harness({ alpha: [async s => {
    const ref = clone(s.ref)
    if (s.submits === 1) { enteredFirst(); await gate }
    return { turn_outcomes: wire(ref, 'complete', [final(ref.request_id === 'owned-alpha-1' ? 'answer A' : 'answer B')]) }
  }] }, { members: [ALPHA] })
  const first = h.gc.runGroupChatRounds('Room', [ALPHA], 'thread-1')
  await entered
  setRoom(h, { epoch: 2, running: true, log: [...room(h).log, { id: 'user-B', from: { kind: 'user', name: 'You' },
    text: 'question B', thread: 'thread-B' }] })
  const second = h.gc.runGroupChatRounds('Room', [ALPHA], 'thread-B')
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(h.rpc('prompt.submit').length, 1, 'new thread waits on the first collector')
  releaseFirst()
  await Promise.all([first, second])
  assert.deepEqual(posts(h).map(entry => [entry.thread, entry.text]), [['thread-1', 'answer A'], ['thread-B', 'answer B']])
  assert.equal(h.rpc('prompt.submit').length, 2)
  assert.equal(room(h).stranded.alpha, undefined)
})

test('newer same-thread intent supersedes its old collector without stale publication or watermark', async () => {
  let releaseFirst, enteredFirst
  const entered = new Promise(resolve => { enteredFirst = resolve })
  const gate = new Promise(resolve => { releaseFirst = resolve })
  const h = await harness({ alpha: [async s => {
    const ref = clone(s.ref)
    if (s.submits === 1) { enteredFirst(); await gate }
    return { turn_outcomes: wire(ref, 'complete', [final(ref.request_id === 'owned-alpha-1' ? 'obsolete' : 'new answer')]) }
  }] }, { members: [ALPHA] })
  const first = h.gc.runGroupChatRounds('Room', [ALPHA], 'thread-1')
  await entered
  setRoom(h, { epoch: 2, running: true, log: [...room(h).log, { id: 'user-new', from: { kind: 'user', name: 'You' },
    text: 'newer question', thread: 'thread-1' }] })
  await h.gc.runGroupChatRounds('Room', [ALPHA], 'thread-1')
  const mark = room(h).watermarks['thread-1::alpha']
  releaseFirst()
  await first
  assert.deepEqual(posts(h).map(entry => entry.text), ['new answer'])
  assert.equal(room(h).watermarks['thread-1::alpha'], mark)
  assert.equal(h.rpc('prompt.submit').length, 2)
  assert.ok(h.gc.currentGroupActivity('Room').some(entry => entry.kind === 'cancelled'))
})

for (const field of ['pending_clarify', 'pending_approval']) {
  test(`rebound runtime ${field} never installs a successor prompt or hides unavailable ownership`, async () => {
    const h = await harness({ alpha: [s => ({ session_id: 'replacement-runtime',
      [field]: { request_id: 'successor-request', question: 'Wrong owner?', command: 'wrong-command' },
      turn_outcomes: wire(s.ref, 'waiting', []) })] })
    await assert.rejects(run(h), /runtime was replaced/)
    assert.equal(h.gc.$groupClarify.get()['Room::alpha'], undefined)
    await h.gc.harvestStrandedGroupReply('Room', ALPHA)
    assert.equal(h.gc.$groupClarify.get()['Room::alpha'], undefined)
    assert.ok(room(h).stranded.alpha)
    assert.equal(h.rpc('approval.respond').length + h.rpc('clarify.respond').length, 0)
  })
}

test('a stopped member cannot collect a complete result returned by its in-flight resume', async () => {
  const h = await harness({ alpha: [async (s, gc) => {
    await gc.stopGroupThread('Room', 'thread-1', [ALPHA])
    return { turn_outcomes: wire(s.ref) }
  }] })
  assert.equal(await run(h), null)
  assert.equal(posts(h).length, 0)
  assert.equal(room(h).stranded.alpha, undefined)
})

async function waitingPair(firstState = 'complete', firstThrows = false) {
  let releaseFirst, enteredFirst
  const entered = new Promise(resolve => { enteredFirst = resolve })
  const gate = new Promise(resolve => { releaseFirst = resolve })
  const h = await harness({ alpha: [async s => {
    const ref = clone(s.ref)
    if (s.submits === 1) {
      enteredFirst()
      await gate
      if (firstThrows) throw new Error('resume transport failed')
    }
    return { turn_outcomes: wire(ref, s.submits === 1 ? firstState : 'complete',
      [final(s.submits === 1 ? 'A reply' : 'B reply', s.submits === 1 ? firstState : 'complete')],
      firstState === 'error' ? 'A failed' : undefined) }
  }] }, { members: [ALPHA] })
  const first = h.gc.runGroupChatRounds('Room', [ALPHA], 'thread-1')
  await entered
  setRoom(h, { epoch: 2, running: true, log: [...room(h).log, { id: 'user-B', from: { kind: 'user', name: 'You' },
    text: 'question B', thread: 'thread-B' }] })
  const second = h.gc.runGroupChatRounds('Room', [ALPHA], 'thread-B')
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.equal(h.collectorTimers(), 1)
  return { h, first, second, releaseFirst }
}

test('Stop cancels a settlement waiter before dispatch and releases its timer/listener', async () => {
  const { h, first, second, releaseFirst } = await waitingPair()
  await h.gc.stopGroupThread('Room', 'thread-B', [ALPHA])
  await second
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.equal(room(h).watermarks['thread-B::alpha'], undefined)
  assert.equal(h.collectorTimers(), 0)
  releaseFirst()
  await first
  assert.equal(posts(h).length, 0)
})

test('a newer drive cancels a waiting thread without replaying its command', async () => {
  const { h, first, second, releaseFirst } = await waitingPair()
  setRoom(h, { epoch: 3 })
  await second
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.equal(room(h).watermarks['thread-B::alpha'], undefined)
  assert.equal(h.collectorTimers(), 0)
  releaseFirst()
  await first
  assert.deepEqual(posts(h).map(e => e.text), ['A reply'])
})

test('bounded settlement expiry is honest and does not advance the unadmitted occurrence', async () => {
  const { h, first, second, releaseFirst } = await waitingPair()
  h.expireCollectorWait()
  await second
  assert.equal(room(h).watermarks['thread-B::alpha'], undefined)
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.equal(h.collectorTimers(), 0)
  assert.ok(h.gc.currentGroupActivity('Room').some(e => e.kind === 'unavailable' && /wait limit/.test(e.reason)))
  releaseFirst()
  await first
  assert.deepEqual(posts(h).map(e => e.text), ['A reply'])
})

test('a terminally failed collector settles its lease and permits the waiting thread exactly once', async () => {
  const { h, first, second, releaseFirst } = await waitingPair('error')
  releaseFirst()
  await Promise.all([first, second])
  assert.equal(h.rpc('prompt.submit').length, 2)
  assert.deepEqual(posts(h).map(e => e.text), ['B reply'])
  assert.equal(h.collectorTimers(), 0)
  assert.equal(room(h).stranded.alpha, undefined)
})

test('repeated collector RPC exceptions settle at the hard cap; waiter retains uncertainty without resubmit', async () => {
  const { h, first, second, releaseFirst } = await waitingPair('complete', true)
  releaseFirst()
  await Promise.all([first, second])
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.ok(room(h).stranded.alpha)
  assert.equal(room(h).watermarks['thread-B::alpha'], undefined)
  assert.equal(h.collectorTimers(), 0)
})

test('two awakened thread waiters reserve admission before async session preparation', async () => {
  let releaseFirst, enteredFirst
  const entered = new Promise(resolve => { enteredFirst = resolve })
  const gate = new Promise(resolve => { releaseFirst = resolve })
  const h = await harness({ alpha: [async s => {
    const ref = clone(s.ref)
    if (s.submits === 1) { enteredFirst(); await gate }
    return { turn_outcomes: wire(ref, 'complete', [final(`answer ${ref.request_id}`)]) }
  }] }, { members: [ALPHA] })
  const first = h.gc.runGroupChatRounds('Room', [ALPHA], 'thread-1')
  await entered
  setRoom(h, { epoch: 2, running: true, log: [...room(h).log,
    { id: 'user-B', from: { kind: 'user', name: 'You' }, text: 'question B', thread: 'thread-B' },
    { id: 'user-C', from: { kind: 'user', name: 'You' }, text: 'question C', thread: 'thread-C' }] })
  const second = h.gc.runGroupChatRounds('Room', [ALPHA], 'thread-B')
  const third = h.gc.runGroupChatRounds('Room', [ALPHA], 'thread-C')
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(h.rpc('prompt.submit').length, 1)
  releaseFirst()
  await Promise.all([first, second, third])
  assert.deepEqual(posts(h).map(entry => entry.thread).sort(), ['thread-1', 'thread-B', 'thread-C'])
  assert.equal(new Set(posts(h).map(entry => entry.delivery.accepted_turn.request_id)).size, 3)
  assert.equal(h.rpc('prompt.submit').length, 3)
  assert.equal(room(h).stranded.alpha, undefined)
  assert.equal(h.collectorTimers(), 0)
})

test('actual send scheduler: rapid B/C callbacks behind A publish all three admitted thread outcomes', async () => {
  let releaseFirst, enteredFirst
  const entered = new Promise(resolve => { enteredFirst = resolve })
  const gate = new Promise(resolve => { releaseFirst = resolve })
  const h = await harness({ alpha: [async s => {
    const ref = clone(s.ref)
    if (s.submits === 1) { enteredFirst(); await gate }
    return { turn_outcomes: wire(ref, 'complete', [final(`answer ${ref.request_id}`)]) }
  }] }, { members: [ALPHA], queuedDrives: true })
  setRoom(h, { log: [], running: false })
  const threadA = h.gc.sendToGroupChat('Room', [ALPHA], 'question A')
  await entered
  const threadB = h.gc.sendToGroupChat('Room', [ALPHA], 'question B')
  const threadC = h.gc.sendToGroupChat('Room', [ALPHA], 'question C')
  assert.equal(h.driveTimers(), 2)
  h.runQueuedDrives()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(h.rpc('prompt.submit').length, 1)
  releaseFirst()
  for (let i = 0; i < 100 && posts(h).length !== 3; i++) await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(posts(h).map(entry => entry.thread), [threadA, threadB, threadC])
  assert.deepEqual(posts(h).map(entry => entry.delivery.accepted_turn.request_id), ['owned-alpha-1', 'owned-alpha-2', 'owned-alpha-3'])
  assert.equal(h.rpc('prompt.submit').length, 3)
  assert.equal(room(h).stranded.alpha, undefined)
  assert.equal(h.collectorTimers(), 0)
})

test('actual backend wire fixture drives the frontend after 194 → 15 compaction with omitted messages', async t => {
  const fixture = JSON.parse(readFileSync(new URL('./fixtures/group-turn-backend-v1.json', import.meta.url), 'utf8'))
  const h = await harness({ alpha: [() => clone(fixture.resume_result)] }, {
    baseline: 194, runtimeId: fixture.submit_ack.result.accepted_turn.session_id,
    ack: () => clone(fixture.submit_ack.result), members: [ALPHA]
  })
  await h.gc.runGroupChatRounds('Room', [ALPHA], 'thread-1')
  assert.deepEqual(posts(h).map(entry => entry.text), ['owned new answer'])
  assert.deepEqual(posts(h)[0].delivery.accepted_turn, fixture.submit_ack.result.accepted_turn)
  assert.equal(room(h).stranded.alpha, undefined)
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.equal(h.elapsed(), 2000)
  assert.deepEqual(h.storageWrites.get('group-chats').Room.log.find(entry => entry.delivery).delivery.accepted_turn,
    fixture.resume_result.turn_outcomes.turns[0].accepted_turn)
  t.diagnostic(JSON.stringify({ producer: 'actual backend test path with fake provider',
    preHistory: 194, resumeCount: 15, messagesOmitted: true, frontendProviderCalls: 0 }))
})

test('rejected read-only poll is visible immediately and next member advances once without resume or replay', async () => {
  const h = await harness({ alpha: [() => { throw Object.assign(new Error('observation transport failed'), { code: 5032 }) }] })
  await h.gc.runGroupChatRounds('Room', [ALPHA, BETA], 'thread-1')
  assert.equal(h.sessions.get('alpha').submits, 1)
  assert.equal(h.sessions.get('beta').submits, 1)
  assert.equal(h.sessions.get('alpha').totalPolls, 1)
  assert.ok(h.gc.currentGroupActivity('Room').some(entry => entry.kind === 'unavailable' && /observation transport failed/.test(entry.reason)))
  assert.ok(room(h).stranded.alpha)
  const initialResumes = h.rpc('session.resume').length
  await h.gc.harvestStrandedGroupReply('Room', ALPHA)
  assert.equal(h.rpc('session.resume').length, initialResumes)
  assert.equal(h.sessions.get('alpha').submits, 1)
})

test('stopped turn discards an in-flight poll rejection without stale failure publication', async () => {
  const h = await harness({ alpha: [async (_s, gc) => {
    await gc.stopGroupThread('Room', 'thread-1', [ALPHA])
    throw new Error('late lost observer')
  }] }, { members: [ALPHA] })
  assert.equal(await run(h), null)
  assert.equal(room(h).stranded.alpha, undefined)
  assert.equal(posts(h).length, 0)
  assert.equal(h.gc.currentGroupActivity('Room').filter(e => e.kind === 'unavailable').length, 0)
})

test('waiting progress states explain clarification and approval without poll spam', async () => {
  const h = await harness({ alpha: [s => ({ running: true, pending_approval: { request_id: 'approve-1', command: 'controlled' },
    turn_outcomes: wire(s.ref, 'running', []) }), s => ({ running: true,
    turn_outcomes: wire(s.ref, 'running', []), pending_clarify_unavailable: true }),
    s => ({ turn_outcomes: wire(s.ref) })] }, { members: [ALPHA] })
  await run(h)
  const waits = h.gc.currentGroupActivity('Room').filter(e => e.kind === 'waiting')
  assert.deepEqual(waits.map(e => e.reason), ['needs your approval', 'clarification state is temporarily unavailable'])
  assert.match(h.gc.groupActivityLabel(waits[0]), /needs your approval/)
})

test('same-thread newer intent suppresses a late poll rejection and retains uncertain admission without replay', async () => {
  const h = await harness({ alpha: [async (_s, gc) => {
    const current = gc.$groupChats.get().Room
    gc.$groupChats.set({ Room: { ...current, epoch: current.epoch + 1,
      log: [...current.log, { id: 'new-user', from: { kind: 'user', name: 'You' }, text: 'new intent', thread: 'thread-1' }] } })
    throw new Error('obsolete observation failure')
  }] }, { members: [ALPHA] })
  assert.equal(await run(h), null)
  assert.ok(room(h).stranded.alpha, 'uncertain admission is retained')
  assert.equal(posts(h).length, 0)
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.equal(h.gc.currentGroupActivity('Room').filter(e => e.kind === 'unavailable').length, 0)
  assert.equal(h.gc.$botAttention.get().alpha, undefined)
  await h.gc.harvestStrandedGroupReply('Room', ALPHA)
  assert.equal(h.gc.currentGroupActivity('Room').filter(e => e.kind === 'unavailable').length, 0)
  assert.equal(h.rpc('prompt.submit').length, 1)
})

for (const state of ['unavailable', 'waiting']) {
  for (const newer of ['same-thread', 'hold']) {
    test(`resolved ${state} harvest under ${newer} publishes no stale activity, attention or prompt`, async () => {
      const h = await harness({ alpha: [s => ({ turn_outcomes: wire(s.ref, 'unavailable', []) }),
        (s, gc) => {
          const current = gc.$groupChats.get().Room
          gc.$groupChats.set({ Room: { ...current, epoch: current.epoch + 1,
            ...(newer === 'hold' ? { holds: { alpha: { thread: 'thread-1' } } }
              : { log: [...current.log, { id: 'new-user', from: { kind: 'user', name: 'You' }, text: 'new intent', thread: 'thread-1' }] }) } })
          return { turn_outcomes: wire(s.ref, state, []),
            ...(state === 'waiting' ? { pending_clarify: { request_id: 'old-question', question: 'Old prompt?' } } : {}) }
        }] }, { members: [ALPHA] })
      await assert.rejects(run(h))
      const marker = room(h).stranded.alpha
      setRoom(h, { stranded: { alpha: { ...marker, reported: false } } })
      h.gc.$botAttention.set({})
      await h.gc.harvestStrandedGroupReply('Room', ALPHA)
      assert.equal(h.gc.currentGroupActivity('Room').filter(e => e.kind === 'unavailable').length, 0)
      assert.equal(h.gc.$botAttention.get().alpha, undefined)
      assert.equal(h.gc.$groupClarify.get()['Room::alpha'], undefined)
      assert.ok(room(h).stranded.alpha)
      assert.equal(posts(h).length, 0)
      assert.equal(h.rpc('prompt.submit').length, 1)
    })
  }
}
