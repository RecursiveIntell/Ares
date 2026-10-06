import assert from 'node:assert/strict'
import test from 'node:test'
import { importGroupTurnPlugin } from './group-turn-test-loader.mjs'

const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
const members = n => Array.from({ length: n }, (_, i) => ({ name: `bot${i + 1}`, title: '', connectionId: 'local',
  sourceScoped: true, route: { connectionId: 'local', mode: 'local', profile: `bot${i + 1}`, targetProfile: `bot${i + 1}` } }))
const clone = value => JSON.parse(JSON.stringify(value))
const flush = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve)) }

async function harness(roster = members(3), options = {}) {
  let now = 100000, sequence = 0, connection = 'local', activeLeases = 0, maxLeases = 0
  const timers = new Map(), sessions = new Map(), calls = [], leases = [], storage = new Map()
  const atom = initial => { let value = initial; const listeners = new Set(); return {
    get: () => value, set: next => { value = next; for (const listener of [...listeners]) listener(value) },
    listen: fn => { listeners.add(fn); return () => listeners.delete(fn) }
  } }
  const sourceKey = (route, profile) => `${route?.connectionId || connection}::${route?.targetProfile || profile}`
  const handle = async (route, method, params) => {
    calls.push({ route: route && { ...route }, method, params: { ...params }, at: now })
    const key = sourceKey(route, params.profile)
    if (method === 'session.create') {
      if (options.createGate) await options.createGate.promise
      const session = { key, route, profile: params.profile, title: params.title,
        runtime: `rt-${++sequence}`, stored: `stored-${sequence}`, submits: 0, state: 'running', text: '(pass)' }
      sessions.set(session.runtime, session)
      return { session_id: session.runtime, stored_session_id: session.stored }
    }
    let session = [...sessions.values()].find(s => (s.runtime === params.session_id || s.stored === params.session_id ||
      (s.key === key && s.title === params.session_id) || (params.request_id && (s.pending?.request_id === params.request_id || s.answeredRequest === params.request_id))) && (route?.connectionId || connection) === (s.route?.connectionId || 'local'))
    if (!session) throw Object.assign(new Error('session not found'), { code: 4007 })
    if (method === 'prompt.submit') {
      session.submits++
      const failure = options.submitError?.(session)
      if (failure) throw failure
      session.ref = { request_id: `accepted-${session.runtime}-${session.submits}`, session_id: session.runtime,
        route: 'compute_host', host_boot_id: `boot-${session.key}` }
      session.prompt = params.text
      session.state = session.submits === 1 || options.chatty ? 'running' : 'complete'
      session.text = options.chatty ? `work ${session.submits} @all` : '(pass)'
      if (options.submitGate) await options.submitGate.promise
      return options.missingAck ? {} : { accepted_turn: { ...session.ref } }
    }
    if (method === 'session.interrupt') {
      if (options.interruptGate) await options.interruptGate.promise
      session.state = 'interrupted'
      return { status: 'interrupted' }
    }
    if (method.includes('attach')) return { ref_text: 'staged-file' }
    if (method === 'clarify.respond' || method === 'approval.respond') {
      if (options.answerError?.()) throw new Error('response transport failed')
      if (options.answerGate) await options.answerGate.promise
      const acknowledgement = method === 'clarify.respond' ? (options.clarifyResult ?? { status: 'ok' }) : {}
      if (method === 'clarify.respond' && acknowledgement?.status !== 'ok') return acknowledgement
      if (method === 'clarify.respond' && session.pending?.questions?.length) {
        session.questionAnswers ||= new Set()
        session.questionAnswers.add(params.question_id)
        if (!session.pending.questions.every(q => session.questionAnswers.has(q.qid ?? q.id))) return acknowledgement
      }
      session.answeredRequest = params.request_id
      session.state = options.resumeRunning ? 'running' : 'complete'
      session.pending = null
      if (options.answerAckGate) await options.answerAckGate.promise
      return acknowledgement
    }
    if (method !== 'session.resume' && method !== 'session.turn.poll') throw new Error(`unexpected RPC ${method}`)
    if (method === 'session.turn.poll') {
      assert.equal(params.accepted_turn?.session_id, params.session_id, 'poll carries captured runtime and accepted identity')
    }
    if (options.pollGate && !params.omit_messages && session.ref) await options.pollGate.promise
    const projection = { session_id: session.runtime, session_key: session.stored, running: session.state === 'running',
      ...(options.pendingUnavailable?.(session) ? { pending_clarify_unavailable: true } : session.pending ? { pending_clarify: session.pending } : {}),
      turn_outcomes: { version: 1, scope: 'process_local', availability: options.unavailable ? 'unavailable' : 'available',
        turns: session.ref && !options.unavailable ? [{ accepted_turn: { ...session.ref }, state: session.state,
          finalized: ['complete', 'error', 'interrupted'].includes(session.state) ? [{ text: session.text, status: session.state,
            ...(session.state === 'error' ? { error: 'member failed' } : {}) }] : [] }] : [] } }
    if (options.staleWaitingGate?.active && session.profile === 'bot1' && session.state === 'waiting') {
      await options.staleWaitingGate.promise
    }
    return projection
  }
  const gc = await importGroupTurnPlugin({ atom,
    Date: class extends Date { static now() { return now } },
    setTimeout: (fn, delay = 0) => { const id = ++sequence; timers.set(id, { fn, at: now + delay }); return id },
    clearTimeout: id => timers.delete(id),
    host: { request: (method, params) => handle(null, method, params), requestProfile: handle,
      retainProfile: async route => {
        if (options.retainGate) await options.retainGate.promise
        if (options.retainError) throw options.retainError
        activeLeases++; maxLeases = Math.max(activeLeases, maxLeases)
        const lease = { route: { ...route }, releases: 0 }; leases.push(lease)
        return () => { lease.releases++; activeLeases-- }
      },
      state: { profile: atom('default'), gateway: atom(null), connectionId: { get: () => connection, listen: () => () => undefined } },
      notify: () => undefined, notifyError: () => undefined }
  })
  gc.stopGroupChatServerSync()
  gc.bindGroupTurnTestStorage({ set: (key, value) => storage.set(key, clone(value)) })
  const input = { id: 'user-1', at: now, from: { kind: 'user', name: 'You' }, text: '@all evaluate FIRST_INPUT', thread: 't1' }
  gc.$groupChats.set({ Room: { roomId: 'room1', epoch: 1, running: true, log: [input],
    watermarks: {}, sessions: {}, stranded: {}, holds: {}, members: roster } })
  const room = () => gc.$groupChats.get().Room
  const posts = () => room().log.filter(e => e.from.kind === 'member')
  const rpc = method => calls.filter(c => c.method === method)
  const advance = async (ms = 2000) => {
    now += ms
    for (const [id, timer] of [...timers]) if (timer.at <= now) { timers.delete(id); timer.fn() }
    await flush()
  }
  const until = async (predicate, max = 30) => { for (let i = 0; i < max && !predicate(); i++) await advance(); assert.ok(predicate(), 'bounded fake-clock condition') }
  const finish = (name, text = '(pass)', state = 'complete') => {
    for (const session of sessions.values()) if (session.profile === name) { session.state = state; session.text = text }
  }
  return { gc, room, posts, rpc, sessions, timers, calls, leases, storage, roster, input, advance, until, finish,
    switchConnection: value => { connection = value }, maxLeases: () => maxLeases, activeLeases: () => activeLeases }
}

const drive = h => h.gc.runGroupChatRounds('Room', h.roster, 't1')

// f911 answer ownership must survive the concurrency responder and its workers.
for (const status of ['expired', 'conflict', undefined]) {
  for (const batch of [false, true]) {
    test(`f911 active ${batch ? 'batch' : 'single'} clarification ${status || 'unknown'} ACK retains exact card and lease`, async () => {
      const h = await harness(members(1), { clarifyResult: { status } })
      const pending = drive(h); await flush()
      const session = [...h.sessions.values()][0]
      session.state = 'waiting'; session.pending = { request_id: 'owned-question', question: 'Choose?',
        ...(batch ? { questions: [{ qid: 'q0' }, { qid: 'q1' }] } : {}) }
      await h.advance()
      const key = 'Room::local::bot1', entry = h.gc.$groupClarify.get()[key]
      await assert.rejects(h.gc.answerGroupClarify(entry, { name: 'bot1', connectionId: 'wrong' }, batch ? { q0: 'one', q1: 'two' } : 'yes'))
      assert.equal(h.gc.$groupClarify.get()[key], entry)
      assert.equal(h.rpc('clarify.respond').length, 1, 'batch stops at first rejected acknowledgement')
      assert.equal(h.rpc('clarify.respond')[0].params.session_id, session.runtime)
      assert.equal(h.rpc('clarify.respond')[0].route.connectionId, 'local')
      assert.equal(h.rpc('prompt.submit').length, 1)
      assert.equal(h.leases[0].releases, 0)
      await h.advance()
      assert.equal(h.gc.$groupClarify.get()[key], entry)
      await h.gc.stopGroupThread('Room', 't1', h.roster); await h.advance(); await pending; await flush()
      assert.equal(h.leases[0].releases, 1)
    })
  }
}

test('f911 active batch answers all questions against captured runtime only after positive acknowledgements', async () => {
  const h = await harness(members(1)); const pending = drive(h); await flush()
  const session = [...h.sessions.values()][0]
  session.state = 'waiting'; session.text = 'BATCH_RESULT'
  session.pending = { request_id: 'owned-batch', question: 'Choose?', questions: [{ qid: 'q0' }, { qid: 'q1' }] }
  await h.advance()
  const key = 'Room::local::bot1', entry = h.gc.$groupClarify.get()[key]
  await h.gc.answerGroupClarify(entry, { name: 'bot1', connectionId: 'wrong' }, { q0: 'one', q1: 'two' })
  assert.deepEqual(h.rpc('clarify.respond').map(c => c.params), [
    { session_id: session.runtime, request_id: 'owned-batch', question_id: 'q0', answer: 'one' },
    { session_id: session.runtime, request_id: 'owned-batch', question_id: 'q1', answer: 'two' }
  ])
  assert.ok(h.rpc('clarify.respond').every(c => c.route.connectionId === 'local'))
  assert.equal(h.gc.$groupClarify.get()[key], undefined)
  await h.until(() => h.posts().some(p => p.text === 'BATCH_RESULT'))
  await h.gc.stopGroupThread('Room', 't1', h.roster); await h.advance(); await pending; await flush()
  assert.ok(h.leases.every(l => l.releases === 1))
})

for (const expired of [false, true]) {
  for (const outcome of ['running', 'unavailable']) {
    test(`f911 ${expired ? 'expired' : 'live'} uncertain answer and unavailable child snapshot keep ${outcome} worker reserved`, async () => {
      let childUnavailable = false
      const answerGate = deferred()
      const options = { answerGate, clarifyResult: {}, pendingUnavailable: s => childUnavailable && s.profile === 'bot1' }
      const h = await harness(members(6), options); const pending = drive(h); await flush()
      const session = [...h.sessions.values()].find(s => s.profile === 'bot1')
      session.state = 'waiting'; session.pending = { request_id: 'uncertain-answer', question: 'Choose?' }
      await h.advance()
      const key = 'Room::local::bot1', entry = h.gc.$groupClarify.get()[key]
      const answering = h.gc.answerGroupClarify(entry, h.roster[0], 'yes')
      const rejected = assert.rejects(answering, /acknowledgement is unconfirmed/)
      if (expired) await h.advance(21 * 60 * 1000)
      h.finish('bot2')
      if (expired) await h.gc.harvestStrandedGroupReply('Room', h.roster[1])
      await h.advance()
      assert.equal(h.rpc('prompt.submit').length, 5)
      answerGate.resolve(); await rejected; await flush()
      // An unconfirmed wire may still have delivered. Only exact outcome proof
      // can surrender its worker; retaining the old card is not waiting proof.
      session.state = 'running'; session.pending = null; childUnavailable = true
      options.unavailable = outcome === 'unavailable'
      if (expired) await h.gc.harvestStrandedGroupReply('Room', h.roster[0])
      await h.advance()
      assert.equal(h.gc.$groupClarify.get()[key], entry, 'unavailable child read preserves card')
      assert.equal(h.rpc('prompt.submit').length, 5, 'sixth stays queued while four may be running')
      assert.equal(h.leases.find(l => l.route.profile === 'bot1').releases, 0)
      await h.gc.stopGroupThread('Room', 't1', h.roster); await h.advance(); await pending; await flush()
      if (options.unavailable) {
        assert.ok(h.leases.some(l => l.releases === 0), 'an interrupt ACK does not retire an unavailable accepted turn')
        options.unavailable = false
        for (const member of h.roster) await h.gc.harvestStrandedGroupReply('Room', member)
      }
      assert.ok(h.leases.every(l => l.releases === 1))
    })
  }
}
const active = h => [...h.gc.groupRoomCoordinators.get('Room').occurrences]

test('actual scheduler overlaps four admissions before any resolves, queues the fifth and freezes every prompt/attachment', async t => {
  const h = await harness(members(6))
  const attachment = { name: 'frozen.txt', data: 'ORIGINAL_BYTES', kind: 'file' }
  h.input.images = [attachment]
  const pending = drive(h)
  await flush()
  assert.equal(h.rpc('prompt.submit').length, 4)
  assert.equal(active(h).filter(o => o.phase === 'running').length, 4)
  assert.equal(active(h).filter(o => o.phase === 'queued').length, 2)
  assert.equal(h.posts().length, 0)
  attachment.data = 'MUTATED_BYTES'
  h.finish('bot4', 'FAST_REPLY')
  await h.advance()
  assert.equal(h.posts()[0].text, 'FAST_REPLY', 'publish without slow siblings')
  assert.equal(h.rpc('prompt.submit').length, 5)
  assert.ok(h.rpc('prompt.submit').every(c => !c.params.text.includes('FAST_REPLY')))
  assert.ok(h.rpc('file.attach').every(c => c.params.data_url === 'ORIGINAL_BYTES'))
  for (const name of ['bot1', 'bot2', 'bot3', 'bot5', 'bot6']) h.finish(name)
  await h.until(() => h.rpc('prompt.submit').length >= 6)
  h.finish('bot6')
  await h.until(() => !h.room().running)
  await pending
  assert.ok(h.maxLeases() <= 4)
  assert.ok(h.leases.every(l => l.releases === 1))
  t.diagnostic(JSON.stringify({ providerCalls: 0, maxConcurrentLeases: h.maxLeases(), fakeClock: true }))
})

test('reverse completion plus trim and remote timestamp insertion never consumes unseen sibling/remote inputs', async () => {
  const h = await harness(members(2))
  h.gc.updateGroupChat('Room', room => { room.log = [...Array.from({ length: 95 }, (_, i) => ({
    id: `prior-${i}`, at: i, from: { kind: 'member', name: 'old' }, text: 'old', thread: 'other' })), ...room.log]; return room })
  const pending = drive(h)
  await flush()
  h.finish('bot2', 'B_REPLY')
  await h.advance()
  h.gc.updateGroupChat('Room', room => {
    for (let i = 0; i < 15; i++) room.log.push({ id: `filler-${i}`, at: 100010 + i, from: { kind: 'member', name: 'filler' }, text: 'other', thread: 'other' })
    return room
  })
  const snapshot = h.gc.groupChatSyncSnapshot()
  Object.values(snapshot.rooms)[0].log.push({ id: 'remote-late-old-time', at: 100040, from: { kind: 'member', name: 'observer' }, text: 'REMOTE_UNSEEN', thread: 't1' })
  h.gc.$groupChats.set(h.gc.mergeRemoteGroupChatSnapshotIntoRooms(snapshot, h.gc.$groupChats.get()))
  h.finish('bot1', 'A_REPLY')
  await h.advance()
  await h.until(() => !h.room().running)
  await pending
  const next = h.rpc('prompt.submit').filter(c => c.params.text.includes('B_REPLY'))
  assert.ok(next.length >= 1, 'frozen bot1 did not consume bot2 completion')
  assert.ok(h.rpc('prompt.submit').some(c => c.params.text.includes('REMOTE_UNSEEN')))
  assert.ok(h.rpc('prompt.submit').some(c => c.params.text.includes('A_REPLY')))
  assert.ok(h.room().log.length <= 96)
  assert.ok(h.storage.get('group-chats').Room.consumedInputs)
})

test('partial failure and clarification park retain lease and exclusion while ready peers progress', async () => {
  const h = await harness(members(3))
  const pending = drive(h)
  await flush()
  const a = [...h.sessions.values()].find(s => s.profile === 'bot1')
  a.state = 'waiting'; a.pending = { request_id: 'ask-A', question: 'Choose?' }
  h.finish('bot2', 'failure', 'error'); h.finish('bot3', 'READY_RESULT')
  await h.advance()
  await h.until(() => !h.room().running)
  await pending
  assert.ok(h.posts().some(p => p.text === 'READY_RESULT'))
  assert.equal([...h.sessions.values()].find(s => s.profile === 'bot1').submits, 1)
  assert.equal(h.leases.find(l => l.route.profile === 'bot1').releases, 0)
  assert.equal(h.gc.$groupClarify.get()['Room::local::bot1'].requestId, 'ask-A')
  assert.equal(active(h).find(o => o.memberKey === 'local::bot1').phase, 'waiting')
  h.finish('bot1', 'ANSWER_AFTER_CLARIFY'); a.pending = null
  await h.advance()
  assert.ok(h.posts().some(p => p.text === 'ANSWER_AFTER_CLARIFY'))
  assert.ok(h.leases.every(l => l.releases === 1))
})

test('dispatch reservations cap chatty six-member drives at ten messages and three dependent rounds', async () => {
  const h = await harness(members(6), { chatty: true })
  const pending = drive(h)
  await flush()
  for (let i = 0; i < 15 && h.room().running; i++) {
    for (const session of h.sessions.values()) session.state = 'complete'
    await h.advance()
  }
  await pending
  assert.equal(h.posts().length, 10)
  assert.equal(h.rpc('prompt.submit').length, 10)
  assert.ok([...h.sessions.values()].every(s => s.submits <= 3))
  assert.ok(h.gc.currentGroupActivity('Room').some(e => e.kind === 'capped'))
})

test('rapid same-thread sends and repeated drive callbacks never duplicate accepted requests', async () => {
  const h = await harness(members(2))
  h.gc.$groupChats.set({ Room: { ...h.room(), log: [], running: false } })
  h.gc.sendToGroupChat('Room', h.roster, 'first', 't1')
  await flush()
  h.gc.sendToGroupChat('Room', h.roster, 'second', 't1')
  h.gc.sendToGroupChat('Room', h.roster, 'newest', 't1')
  await h.advance(250)
  assert.equal(h.rpc('prompt.submit').length, 2, 'old accepted turns retain exclusion')
  h.finish('bot1', 'STALE'); h.finish('bot2', 'STALE')
  await h.advance()
  await h.until(() => !h.room().running)
  assert.ok(!h.posts().some(p => p.text === 'STALE'))
  assert.equal(h.rpc('prompt.submit').length, 4)
  assert.equal(new Set(h.posts().map(p => p.delivery.accepted_turn.request_id)).size, h.posts().length)
})

test('cross-thread epoch retains captured outcomes, same-thread epoch discards every stale sibling', async () => {
  for (const [thread, expected] of [['other', 2], ['t1', 0]]) {
    const h = await harness(members(2)); const pending = drive(h); await flush()
    h.gc.updateGroupChat('Room', room => { room.epoch++; room.log.unshift({ id: 'user-reordered', at: 1,
      from: { kind: 'user', name: 'You' }, text: 'later input', thread }); return room })
    h.finish('bot1', 'OLD1'); h.finish('bot2', 'OLD2')
    await h.advance(); await pending
    assert.equal(h.posts().length, expected)
    assert.equal(h.rpc('prompt.submit').length, 2)
  }
})

for (const stage of ['retain', 'create', 'submit', 'poll']) {
  test(`Stop cancels all queued/starting/running occurrences during ${stage} race and releases once`, async () => {
    const gate = deferred()
    const h = await harness(members(6), { [`${stage === 'create' ? 'create' : stage}Gate`]: gate })
    const pending = drive(h); await flush()
    assert.equal(active(h).length, 6)
    const stopping = h.gc.stopGroupThread('Room', 't1', h.roster)
    await flush()
    gate.resolve()
    await stopping
    await h.until(() => active(h).length === 0); await pending; await flush()
    assert.equal(h.posts().length, 0)
    assert.equal(active(h).length, 0)
    assert.ok(h.leases.every(l => l.releases === 1))
    if (stage === 'retain' || stage === 'create') assert.equal(h.rpc('prompt.submit').length, 0)
    if (stage !== 'retain') assert.equal(new Set(h.rpc('session.interrupt').map(c => c.params.session_id)).size, 4)
    assert.equal(h.room().running, false)
  })
}

test('Stop uses source-qualified duplicate names and exact runtime even if roster/session cache changes', async () => {
  const roster = ['east', 'west'].map(connectionId => ({ name: 'same', connectionId, sourceScoped: true, remoteSource: true,
    route: { connectionId, mode: 'remote', profile: 'same', targetProfile: 'default' } }))
  const h = await harness(roster); const pending = drive(h); await flush()
  const originals = h.rpc('prompt.submit').map(c => [c.route.connectionId, c.params.session_id])
  h.gc.updateGroupChat('Room', room => { room.sessions = { 'east::same': 'replacement-stored', 'west::same': 'wrong' }; return room })
  await h.gc.stopGroupThread('Room', 't1', roster.slice().reverse())
  await h.advance(); await pending
  assert.deepEqual(h.rpc('session.interrupt').map(c => [c.route.connectionId, c.params.session_id]).sort(), originals.sort())
  assert.ok(h.leases.every(l => l.releases === 1))
})

test('reload keeps unavailable accepted proof unresolved, never autonomously resubmits', async () => {
  const h = await harness(members(1), { unavailable: true })
  const pending = drive(h); await flush(); await h.advance(); await pending
  const durable = clone(h.storage.get('group-chats'))
  const cold = await harness(members(1), { unavailable: true })
  cold.gc.$groupChats.set(durable)
  await cold.gc.runGroupChatRounds('Room', cold.roster, 't1')
  assert.equal(cold.rpc('prompt.submit').length, 0)
  assert.equal(cold.posts().length, 0)
  assert.ok(cold.room().stranded['local::bot1'])
})

for (const code of [4090, 'POOL_CAPACITY_EXCEEDED']) {
  test(`known ${code} capacity rejection before acceptance has no retry, replay or permanent marker`, async () => {
    const options = code === 4090 ? { submitError: () => Object.assign(new Error('capacity full'), { code }) }
      : { retainError: Object.assign(new Error('pool full'), { code }) }
    const h = await harness(members(1), options)
    await drive(h); await flush()
    assert.equal(h.rpc('prompt.submit').length, code === 4090 ? 1 : 0)
    assert.equal(Object.keys(h.room().stranded).length, 0)
    assert.equal(active(h).length, 0)
    await h.gc.runGroupChatRounds('Room', h.roster, 't1')
    assert.equal(h.rpc('prompt.submit').length, code === 4090 ? 1 : 0)
  })
}

test('a text-only session-not-found submit failure is uncertain and never retried', async () => {
  const h = await harness(members(1), { submitError: () => new Error('session not found after possible acceptance') })
  await drive(h); await flush()
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.ok(h.room().stranded['local::bot1'])
})

test('four clarification waiters keep custody but permit unrelated ready peers to admit', async () => {
  const h = await harness(members(6)); const pending = drive(h); await flush()
  for (const session of h.sessions.values()) { session.state = 'waiting'; session.pending = { request_id: `ask-${session.profile}`, question: 'Choose?' } }
  await h.advance()
  assert.equal(h.rpc('prompt.submit').length, 6)
  assert.equal(active(h).filter(o => o.phase === 'waiting').length, 4)
  assert.equal(h.gc.groupRoomCoordinators.get('Room').active, 2)
  assert.ok(h.leases.slice(0, 4).every(l => l.releases === 0), 'parked leases retained')
  h.finish('bot5', 'READY_5'); h.finish('bot6', 'READY_6')
  await h.until(() => !h.room().running); await pending
  assert.ok(h.posts().some(e => e.text === 'READY_5'))
  assert.ok(h.posts().some(e => e.text === 'READY_6'))
  await h.gc.stopGroupThread('Room', 't1', h.roster); await h.advance(); await flush()
  assert.ok(h.leases.every(l => l.releases === 1))
})

test('clarification answered after hard cap renews exact proof collection without submitting text', async () => {
  const h = await harness(members(1)); const pending = drive(h); await flush()
  const session = [...h.sessions.values()][0]
  session.state = 'waiting'; session.pending = { request_id: 'late-human', question: 'Choose?' }
  await h.advance(); await pending
  await h.advance(1200000)
  assert.equal(active(h)[0].collectorDone, true)
  assert.equal(h.leases[0].releases, 0)
  session.text = 'LATE_HUMAN_ANSWER'
  h.switchConnection('another-source')
  const entry = h.gc.$groupClarify.get()['Room::local::bot1']
  await h.gc.answerGroupClarify(entry, { name: 'bot1', connectionId: 'wrong' }, 'yes')
  await flush()
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.equal(h.posts()[0].text, 'LATE_HUMAN_ANSWER')
  assert.equal(h.rpc('clarify.respond')[0].route.connectionId, 'local')
  assert.equal(h.leases[0].releases, 1)
  assert.equal(active(h).length, 0)
})

test('stale terminal harvest closes expired parked lease and exact exclusion once', async () => {
  const h = await harness(members(1)); const pending = drive(h); await flush()
  const session = [...h.sessions.values()][0]
  session.state = 'waiting'; session.pending = { request_id: 'old-question', question: 'Choose?' }
  await h.advance(); await pending; await h.advance(1200000)
  h.gc.appendGroupChatEntry('Room', { kind: 'user', name: 'You' }, 'new same-thread input', 't1')
  h.gc.updateGroupChat('Room', room => { room.epoch++; return room })
  session.state = 'complete'; session.pending = null; session.text = 'STALE_LATE'
  await h.gc.harvestStrandedGroupReply('Room', h.roster[0]); await flush()
  assert.equal(h.posts().length, 0)
  assert.equal(active(h).length, 0)
  assert.equal(h.leases[0].releases, 1)
  assert.equal(Object.keys(h.room().stranded).length, 0)
})

test('legacy room lifetime replacement during lease acquisition discards predecessor without admission', async () => {
  const gate = deferred(); const h = await harness(members(1), { retainGate: gate })
  const old = { ...h.room(), roomId: null }; h.gc.$groupChats.set({ Room: old })
  const pending = drive(h); await flush()
  h.gc.$groupChats.set({ Room: { ...old, log: [{ id: 'replacement', at: 100020,
    from: { kind: 'user', name: 'You' }, text: 'NEW_ROOM', thread: 'other' }], sessions: {}, stranded: {} } })
  gate.resolve(); await flush(); await pending
  assert.equal(h.rpc('prompt.submit').length, 0)
  assert.equal(h.rpc('session.create').length, 0)
  assert.equal(h.leases[0].releases, 1)
})

test('same-thread newer user evidence survives trimming of both captured anchor and newer input', async () => {
  const h = await harness(members(1)); const pending = drive(h); await flush()
  h.gc.appendGroupChatEntry('Room', { kind: 'user', name: 'You' }, 'supersede', 't1')
  h.gc.updateGroupChat('Room', room => {
    room.epoch++
    for (let i = 0; i < 110; i++) room.log.push({ id: `trim-${i}`, at: 100020 + i, from: { kind: 'member', name: 'filler' }, text: 'filler', thread: 'other' })
    return room
  })
  h.finish('bot1', 'MUST_DISCARD'); await h.advance(); await pending
  assert.equal(h.posts().filter(p => p.text === 'MUST_DISCARD').length, 0)
  assert.equal(h.room().watermarks['t1::local::bot1'], undefined)
})

test('Stop interrupt acknowledgements and old resume completion fence same-session replacement', async () => {
  const pollGate = deferred(), interruptGate = deferred()
  const h = await harness(members(1), { pollGate, interruptGate })
  const pending = drive(h); await flush(); await h.advance()
  const stopping = h.gc.stopGroupThread('Room', 't1', h.roster); await flush()
  h.gc.sendToGroupChat('Room', h.roster, '@all resume newer work', 't2')
  await h.advance(250)
  assert.equal(h.rpc('prompt.submit').length, 1)
  interruptGate.resolve(); await stopping; await flush()
  assert.equal(h.rpc('prompt.submit').length, 1, 'RPC still owns old captured runtime')
  pollGate.resolve(); await flush(); await pending
  await h.until(() => !h.room().running)
  assert.equal(h.rpc('prompt.submit').length, 2)
  assert.equal(h.rpc('session.interrupt').length, 1, 'old interrupt is never sent after replacement admission')
  assert.ok(h.leases.every(l => l.releases === 1))
})

test('duplicate coordinator drive calls share one admission owner', async () => {
  const h = await harness(members(2))
  const first = drive(h), second = drive(h)
  assert.equal(first, second)
  await flush(); assert.equal(h.rpc('prompt.submit').length, 2)
  h.finish('bot1'); h.finish('bot2'); await h.advance(); await first
  assert.equal(h.rpc('prompt.submit').length, 2)
})


test('failed clarification response after hard cap retains card and custody while returning its worker', async () => {
  let fail = true
  const h = await harness(members(1), { answerError: () => fail }); const pending = drive(h); await flush()
  const session = [...h.sessions.values()][0]
  session.state = 'waiting'; session.pending = { request_id: 'retry-human', question: 'Choose?' }
  await h.advance(); await pending; await h.advance(1200000)
  const entry = h.gc.$groupClarify.get()['Room::local::bot1']
  await assert.rejects(h.gc.answerGroupClarify(entry, h.roster[0], 'yes'), /response transport failed/); await flush()
  assert.equal(h.gc.groupRoomCoordinators.get('Room').active, 0)
  assert.equal(active(h)[0].phase, 'waiting')
  assert.equal(h.gc.$groupClarify.get()['Room::local::bot1'], entry)
  assert.equal(h.leases[0].releases, 0)
  fail = false; session.text = 'RECOVERED_ANSWER'
  await h.gc.answerGroupClarify(entry, h.roster[0], 'yes'); await h.until(() => h.posts().length > 0)
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.equal(h.posts()[0].text, 'RECOVERED_ANSWER')
  assert.equal(h.leases[0].releases, 1)
})

test('concurrent clarification answers share one queued resume-worker reservation', async () => {
  const h = await harness(members(6)); const pending = drive(h); await flush()
  const session = [...h.sessions.values()].find(s => s.profile === 'bot1')
  session.state = 'waiting'; session.pending = { request_id: 'double-human', question: 'Choose?' }
  await h.advance()
  assert.equal(h.gc.groupRoomCoordinators.get('Room').active, 4)
  const entry = h.gc.$groupClarify.get()['Room::local::bot1']
  const first = h.gc.answerGroupClarify(entry, h.roster[0], 'yes')
  const second = h.gc.answerGroupClarify(entry, h.roster[0], 'yes')
  assert.equal(first, second, 'same captured answer owns one response')
  assert.equal(h.gc.groupRoomCoordinators.get('Room').resumeQueue.length, 1)
  h.finish('bot2'); await h.advance(); await first; await second
  assert.ok(h.gc.groupRoomCoordinators.get('Room').active <= 4)
  await h.gc.stopGroupThread('Room', 't1', h.roster); await h.advance(); await pending; await flush()
  assert.equal(h.gc.groupRoomCoordinators.get('Room').active, 0)
  assert.ok(h.leases.every(l => l.releases === 1))
})


test('waiting polls preserve reserved worker throughout delayed human response acceptance', async () => {
  const answerGate = deferred()
  const h = await harness(members(6), { answerGate, resumeRunning: true })
  const pending = drive(h); await flush()
  const session = [...h.sessions.values()].find(s => s.profile === 'bot1')
  session.state = 'waiting'; session.pending = { request_id: 'slow-answer', question: 'Choose?' }
  await h.advance()
  const entry = h.gc.$groupClarify.get()['Room::local::bot1']
  const answering = h.gc.answerGroupClarify(entry, h.roster[0], 'yes')
  h.finish('bot2'); await h.advance()
  assert.equal(h.gc.groupRoomCoordinators.get('Room').active, 4)
  await h.advance(); answerGate.resolve(); await answering; await flush()
  assert.ok([...h.sessions.values()].filter(s => s.state === 'running').length <= 4)
  assert.equal(h.rpc('prompt.submit').length, 5, 'sixth stays queued behind reserved answer worker')
  await h.gc.stopGroupThread('Room', 't1', h.roster); await h.advance(); await pending
  assert.ok(h.leases.every(l => l.releases === 1))
})

for (const expired of [false, true]) {
  test(`Stop fences delayed human response before replacement when collector expired=${expired}`, async () => {
    const answerGate = deferred()
    const h = await harness(members(1), { answerGate, resumeRunning: true })
    const pending = drive(h); await flush()
    const session = [...h.sessions.values()][0]
    session.state = 'waiting'; session.pending = { request_id: 'answer-stop', question: 'Choose?' }
    await h.advance(); await pending
    if (expired) await h.advance(1200000)
    const entry = h.gc.$groupClarify.get()['Room::local::bot1']
    const answering = h.gc.answerGroupClarify(entry, h.roster[0], 'yes'); await flush()
    const stopping = h.gc.stopGroupThread('Room', 't1', h.roster); await flush()
    await h.advance()
    h.gc.sendToGroupChat('Room', h.roster, '@all resume successor', 'other'); await h.advance(250)
    assert.equal(h.rpc('prompt.submit').length, 1)
    assert.equal(h.leases[0].releases, 0, 'old response still owns exact session')
    answerGate.resolve(); await answering; await stopping; await flush()
    await h.until(() => h.rpc('prompt.submit').length === 2)
    assert.equal(h.rpc('session.interrupt').length, 2, 'post-response interrupt follows earlier Stop')
    assert.equal(h.leases[0].releases, 1)
    await h.until(() => !h.room().running)
  })
}


test('pre-answer waiting snapshot returned after response ACK preserves reserved worker', async () => {
  const answerGate = deferred(), staleWaitingGate = { ...deferred(), active: false }
  const h = await harness(members(6), { answerGate, staleWaitingGate, resumeRunning: true })
  const pending = drive(h); await flush()
  const session = [...h.sessions.values()].find(s => s.profile === 'bot1')
  session.state = 'waiting'; session.pending = { request_id: 'stale-answer-poll', question: 'Choose?' }
  await h.advance()
  const entry = h.gc.$groupClarify.get()['Room::local::bot1']
  const answering = h.gc.answerGroupClarify(entry, h.roster[0], 'yes')
  h.finish('bot2'); await h.advance()
  staleWaitingGate.active = true; await h.advance()
  answerGate.resolve(); await answering; await flush()
  staleWaitingGate.resolve(); await flush()
  assert.ok([...h.sessions.values()].filter(s => s.state === 'running').length <= 4)
  assert.equal(h.rpc('prompt.submit').length, 5)
  await h.gc.stopGroupThread('Room', 't1', h.roster); await h.advance(); await pending
  assert.ok(h.leases.every(l => l.releases === 1))
})


test('running hard-cap expiration keeps admission custody and does not start queued fifth member', async () => {
  const h = await harness(members(5)); const pending = drive(h); await flush()
  await h.advance(1200000)
  assert.equal(h.rpc('prompt.submit').length, 4)
  assert.equal(h.gc.groupRoomCoordinators.get('Room').active, 4)
  assert.ok(h.leases.every(l => l.releases === 0))
  assert.equal(active(h).filter(o => o.collectorDone && o.phase === 'running').length, 4)
  await h.gc.stopGroupThread('Room', 't1', h.roster); await pending; await flush()
  assert.ok(h.leases.every(l => l.releases === 1))
})

test('renewed observation expiry keeps four running answers owned and fifth answer queued', async () => {
  const h = await harness(members(5), { resumeRunning: true }); const pending = drive(h); await flush()
  for (const session of h.sessions.values()) { session.state = 'waiting'; session.pending = { request_id: `renew-${session.profile}`, question: 'Choose?' } }
  await h.advance()
  const fifth = [...h.sessions.values()].find(s => s.profile === 'bot5')
  fifth.state = 'waiting'; fifth.pending = { request_id: 'renew-bot5', question: 'Choose?' }
  await h.advance(); await pending; await h.advance(1200000)
  const entries = Object.values(h.gc.$groupClarify.get())
  for (const entry of entries.slice(0, 4)) await h.gc.answerGroupClarify(entry, h.roster.find(m => m.name === entry.member), 'yes')
  let fifthSettled = false
  const answering = h.gc.answerGroupClarify(entries[4], h.roster[4], 'yes').then(() => { fifthSettled = true }, () => undefined)
  for (let i = 0; i < 62; i++) await h.advance(5000)
  assert.equal(fifthSettled, false)
  assert.equal([...h.sessions.values()].filter(s => s.state === 'running').length, 4)
  assert.equal(h.gc.groupRoomCoordinators.get('Room').active, 4)
  assert.equal(h.rpc('clarify.respond').length, 4)
  assert.ok(h.leases.every(l => l.releases === 0))
  await h.gc.stopGroupThread('Room', 't1', h.roster); await answering; await flush()
  assert.ok(h.leases.every(l => l.releases === 1))
})


test('distinct clarification request arriving before prior response ACK sends its own answer', async () => {
  const answerAckGate = deferred()
  const h = await harness(members(1), { answerAckGate, resumeRunning: true }); const pending = drive(h); await flush()
  const session = [...h.sessions.values()][0]
  session.state = 'waiting'; session.pending = { request_id: 'first-question', question: 'First?' }
  await h.advance(); await pending
  const firstEntry = h.gc.$groupClarify.get()['Room::local::bot1']
  const first = h.gc.answerGroupClarify(firstEntry, h.roster[0], 'first answer'); await flush()
  session.state = 'waiting'; session.pending = { request_id: 'second-question', question: 'Second?' }
  await h.advance()
  const secondEntry = h.gc.$groupClarify.get()['Room::local::bot1']
  const second = h.gc.answerGroupClarify(secondEntry, h.roster[0], 'second answer')
  assert.notEqual(first, second)
  answerAckGate.resolve(); await first; await second; await flush()
  assert.deepEqual(h.rpc('clarify.respond').map(c => c.params.request_id), ['first-question', 'second-question'])
  await h.gc.stopGroupThread('Room', 't1', h.roster); await h.advance()
})

test('actual frozen scheduler caps quiet-round handoff continuations at two', async () => {
  const h = await harness(members(4))
  h.input.text = '@bot1 evaluate'
  h.gc.updateGroupChat('Room', room => { room.log.unshift({ id: 'older-handoff', at: 99990,
    from: { kind: 'member', name: 'bot1' }, text: '@bot2 pending earlier handoff', thread: 't1' }); return room })
  const pending = drive(h); await flush()
  assert.equal(h.rpc('prompt.submit').length, 1)
  h.finish('bot1'); await h.advance()
  assert.equal([...h.sessions.values()].find(s => s.profile === 'bot2').submits, 1)
  h.finish('bot2', 'HANDOFF_ONE @bot3'); await h.advance()
  h.gc.appendGroupChatEntry('Room', { kind: 'member', name: 'bot2' }, 'FRESH_FOR_BOT3 @bot3', 't1')
  h.finish('bot3'); await h.advance()
  assert.equal([...h.sessions.values()].find(s => s.profile === 'bot3').submits, 2)
  h.finish('bot3', 'HANDOFF_TWO @bot4'); await h.advance()
  h.gc.appendGroupChatEntry('Room', { kind: 'member', name: 'bot3' }, 'FRESH_FOR_BOT4 @bot4', 't1')
  h.finish('bot4'); await h.advance(); await pending
  assert.equal([...h.sessions.values()].find(s => s.profile === 'bot4').submits, 1, 'third continuation never dispatches')
  assert.ok(h.gc.currentGroupActivity('Room').some(e => e.kind === 'capped'))
  assert.equal(h.room().running, false)
  assert.ok(h.leases.every(l => l.releases === 1))
})
