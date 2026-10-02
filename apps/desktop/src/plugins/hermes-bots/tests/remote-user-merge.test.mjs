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
    if (options.pollGate && !params.omit_messages && session.ref) {
      options.pollEntered = (options.pollEntered || 0) + 1
      await options.pollGate.promise
    }
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


for (const thread of ['t1', 'other']) {
  test(`remote user merge during active turn: ${thread}`, async t => {
    const h = await harness(members(2)); const pending = drive(h); await flush()
    const snapshot = h.gc.groupChatSyncSnapshot()
    Object.values(snapshot.rooms)[0].log.push({ id: 'remote-new-user', at: 100050,
      from: {kind: 'user', name: 'You'}, text: 'REMOTE_NEW_INTENT', thread })
    h.gc.$groupChats.set(h.gc.mergeRemoteGroupChatSnapshotIntoRooms(snapshot, h.gc.$groupChats.get()))
    h.finish('bot1', 'STALE_FIRST_1'); h.finish('bot2', 'STALE_FIRST_2')
    await h.advance(); await h.until(() => !h.room().running); await pending
    t.diagnostic(JSON.stringify({ thread, epoch:h.room().epoch, inputVersions:h.room().threadInputVersions,
      posts:h.posts().map(e => ({text:e.text,thread:e.thread})), submits:h.rpc('prompt.submit').length,
      promptSawRemote: h.rpc('prompt.submit').some(c=>c.params.text.includes('REMOTE_NEW_INTENT')),
      consumed: h.room().consumedInputs, providerCalls: 0 }))
    if (thread === 't1') assert.equal(h.posts().filter(e=>e.text.startsWith('STALE_FIRST')).length,0,
      'same-thread new remote user input must supersede accepted old-input completions')
    else assert.equal(h.posts().filter(e=>e.text.startsWith('STALE_FIRST')).length,2)
  })
}

function mergeRemoteUser(h, thread = 't1') {
  const snapshot = h.gc.groupChatSyncSnapshot()
  Object.values(snapshot.rooms)[0].log.push({ id: 'remote-new-user', at: 100050,
    from: {kind: 'user', name: 'You'}, text: 'REMOTE_NEW_INTENT', thread })
  h.gc.$groupChats.set(h.gc.mergeRemoteGroupChatSnapshotIntoRooms(snapshot, h.gc.$groupChats.get()))
}

test('control: remote user merge while retain pending prevents admission', async t => {
  const gate = deferred(), h = await harness(members(2), {retainGate: gate})
  const pending = drive(h); await flush(); mergeRemoteUser(h)
  gate.resolve(); await flush(); await pending
  t.diagnostic(JSON.stringify({submits:h.rpc('prompt.submit').length, posts:h.posts().length,
    leases:h.leases.map(l=>l.releases), providerCalls:0}))
  assert.equal(h.rpc('prompt.submit').length,0)
  assert.ok(h.leases.every(l=>l.releases===1))
})

test('remote user merge distinguishes queued cancellation and active stale publication', async t => {
  const h = await harness(members(6)); const pending = drive(h); await flush()
  assert.equal(h.rpc('prompt.submit').length,4)
  mergeRemoteUser(h)
  for(const name of ['bot1','bot2','bot3','bot4']) h.finish(name, `STALE_${name}`)
  await h.advance(); await pending
  t.diagnostic(JSON.stringify({submits:h.rpc('prompt.submit').length, posts:h.posts().map(e=>e.text), providerCalls:0}))
  assert.equal(h.rpc('prompt.submit').length,4, 'queued members correctly reject new same-thread version')
  assert.equal(h.posts().length,0, 'active members must reject that same superseded version')
})

test('remote user merge before stranded terminal harvest rejects old intent', async t => {
  const h = await harness(members(1)); const pending = drive(h); await flush()
  const session=[...h.sessions.values()][0]
  session.state='waiting'; session.pending={request_id:'old-question',question:'Choose?'}
  await h.advance(); await pending; await h.advance(1200000)
  mergeRemoteUser(h)
  session.state='complete'; session.pending=null; session.text='STALE_HARVEST'
  await h.gc.harvestStrandedGroupReply('Room',h.roster[0]); await flush()
  t.diagnostic(JSON.stringify({submits:h.rpc('prompt.submit').length, posts:h.posts().map(e=>e.text),
    stranded:Object.keys(h.room().stranded).length,leases:h.leases.map(l=>l.releases),providerCalls:0}))
  assert.equal(h.posts().length,0)
})

function trimCapturedUsers(h) {
  h.gc.updateGroupChat('Room', room => {
    for (let i = 0; i < 120; i++) room.log.push({ id: `trim-peer-${i}`, at: 100100 + i,
      from: { kind: 'member', name: 'observer' }, text: 'unrelated peer history', thread: 'other' })
    return room
  })
  assert.equal(h.room().log.some(e => e.id === 'user-1' || e.id === 'remote-new-user'), false,
    'both captured and superseding user rows have actually been trimmed')
  assert.equal(h.room().threadInputVersions.t1, 1, 'input receipt survives row trimming')
}

function assertSupersededCleanup(h) {
  assert.equal(h.posts().filter(e => e.from.name === 'bot1').length, 0, 'no stale terminal reply')
  assert.equal(h.rpc('prompt.submit').length, 1, 'remote projection never replays or admits another turn')
  assert.deepEqual(h.room().watermarks, {}, 'no stale captured boundary is consumed')
  assert.deepEqual(h.room().consumedInputs || {}, {}, 'no consumed-ID receipt advances')
  assert.deepEqual(h.gc.$botAttention.get(), {}, 'stale failure does not publish attention')
  assert.equal(h.gc.currentGroupActivity('Room').filter(e => e.kind === 'failed').length, 0,
    'stale failure does not publish a failed activity')
  assert.deepEqual(h.room().stranded, {}, 'terminal marker is consumed exactly once')
  assert.equal(h.activeLeases(), 0)
  assert.equal(h.leases.length, 1)
  assert.equal(h.leases[0].releases, 1)
  assert.equal(h.gc.groupRuntimeSessionOwners.size, 0, 'captured session exclusion is released')
}

for (const state of ['complete', 'error']) {
  for (const trim of [false, true]) {
    test(`same-thread remote supersession during live poll await: ${state}, trim=${trim}`, async () => {
      const gate = deferred(), options = { pollGate: gate }, h = await harness(members(1), options)
      const pending = drive(h)
      await flush()
      await h.until(() => options.pollEntered > 0)
      mergeRemoteUser(h)
      if (trim) trimCapturedUsers(h)
      h.finish('bot1', 'STALE_POLLED_TERMINAL', state)
      gate.resolve()
      await flush()
      await h.until(() => !h.room().running)
      await pending
      assert.equal(h.room().epoch, 1, 'remote input did not change the local epoch')
      assertSupersededCleanup(h)
    })

    test(`same-thread remote supersession during stranded poll await: ${state}, trim=${trim}`, async () => {
      const options = {}, h = await harness(members(1), options)
      const pending = drive(h)
      await flush()
      const session = [...h.sessions.values()][0]
      session.state = 'waiting'
      session.pending = { request_id: 'parked-question', question: 'Choose?' }
      await h.advance()
      await pending
      await h.advance(1200000)
      const gate = deferred(), pollsBefore = h.rpc('session.turn.poll').length
      options.pollGate = gate
      h.finish('bot1', 'STALE_STRANDED_TERMINAL', state)
      session.pending = null
      const harvesting = h.gc.harvestStrandedGroupReply('Room', h.roster[0])
      await flush()
      assert.equal(h.rpc('session.turn.poll').length, pollsBefore + 1, 'captured harvest read started')
      assert.equal(options.pollEntered, 1, 'the harvest projection is blocked across its await')
      mergeRemoteUser(h)
      if (trim) trimCapturedUsers(h)
      gate.resolve()
      await harvesting
      await flush()
      assert.equal(h.room().epoch, 1)
      assertSupersededCleanup(h)
    })
  }
}

test('display-only remote projection with the same user identity preserves live completion', async () => {
  const h = await harness(members(1)), pending = drive(h)
  await flush()
  const snapshot = h.gc.groupChatSyncSnapshot()
  const projected = Object.values(snapshot.rooms)[0]
  projected.revision = 20
  projected.image = 'display-image'
  projected.log[0].text = 'compact display copy of the same input'
  h.gc.$groupChats.set(h.gc.mergeRemoteGroupChatSnapshotIntoRooms(snapshot, h.gc.$groupChats.get()))
  assert.equal(h.room().threadInputVersions?.t1 || 0, 0)
  assert.equal(h.room().log[0].text, h.input.text, 'existing rich input remains authoritative')
  h.finish('bot1', 'VALID_CURRENT_INPUT')
  await h.advance()
  await h.until(() => !h.room().running)
  await pending
  assert.deepEqual(h.posts().map(e => e.text), ['VALID_CURRENT_INPUT'])
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.equal(h.leases[0].releases, 1)
})

test('legacy marker without captured per-thread input identity retains equal-epoch harvest behavior', async () => {
  const h = await harness(members(1)), pending = drive(h)
  await flush()
  const session = [...h.sessions.values()][0]
  session.state = 'waiting'
  session.pending = { request_id: 'legacy-question', question: 'Choose?' }
  await h.advance()
  await pending
  await h.advance(1200000)
  const marker = h.room().stranded[h.gc.groupMemberKey(h.roster[0])]
  delete marker.input_version
  delete marker.user_ids
  mergeRemoteUser(h)
  h.finish('bot1', 'LEGACY_SAME_EPOCH')
  session.pending = null
  await h.gc.harvestStrandedGroupReply('Room', h.roster[0])
  await flush()
  assert.equal(h.room().epoch, 1)
  assert.deepEqual(h.posts().map(e => e.text), ['LEGACY_SAME_EPOCH'])
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.deepEqual(h.room().stranded, {})
  assert.equal(h.leases[0].releases, 1)
})
