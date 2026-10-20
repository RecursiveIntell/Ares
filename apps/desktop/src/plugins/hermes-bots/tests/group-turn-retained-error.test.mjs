import assert from 'node:assert/strict'
import test from 'node:test'
import { fixtureAdmission, fixtureProjection } from './group-turn-wire-fixture.mjs'
import { importGroupTurnPlugin } from './group-turn-test-loader.mjs'

const MEMBERS = [{ name: 'alpha', title: '' }, { name: 'beta', title: '' }]
const retainedError = {
  running: false,
  inflight: { status: 'error', streaming: false, error: 'context lineage failed',
    error_surface: { layer: 'context', code: 'test_failure' } }
}

async function harness(t, scripts, { onPoll } = {}) {
  let now = 100000
  const atom = initial => {
    let value = initial
    return { get: () => value, set: next => { value = next }, listen: () => () => undefined }
  }
  const sessions = new Map()
  const rpcLog = []
  let releases = 0
  let gc
  const handle = async (method, params) => {
    rpcLog.push({ method, params, at: now })
    if (method === 'session.create') {
      const profile = params.profile
      const session = { profile, runtime: `runtime-${profile}`, stored: `stored-${profile}`,
        messages: [], submits: 0, polls: 0 }
      sessions.set(profile, session)
      return { session_id: session.runtime, stored_session_id: session.stored }
    }
    const session = [...sessions.values()].find(s =>
      s.runtime === params.session_id || s.stored === params.session_id)
    if (!session) {
      throw Object.assign(new Error('session not found'), { code: 4007 })
    }
    if (method === 'prompt.submit') {
      session.submits++
      session.messages.push({ role: 'user', content: params.text })
      return { accepted_turn: fixtureAdmission(session, `owned-${session.profile}-${session.submits}`) }
    }
    if (method === 'session.resume' || method === 'session.turn.poll') {
      let state = {}
      if (session.submits && !params.omit_messages) {
        session.polls++
        const snapshots = scripts[session.profile] || [{ reply: '(pass)' }]
        state = { ...snapshots[Math.min(session.polls - 1, snapshots.length - 1)] }
        if ('reply' in state && session.messages.at(-1)?.role !== 'assistant') {
          session.messages.push({ role: 'assistant', content: state.reply })
        }
        delete state.reply
        onPoll?.(session.profile, session.polls, gc)
      }
      const error = state.inflight?.status === 'error' && !state.inflight.streaming && !state.running
      const active = state.running || state.inflight && !error
      const finals = (state.messages || session.messages).filter(message => message.role === 'assistant')
        .map(message => ({ text: message.content, status: 'complete' }))
      state.turn_outcomes = fixtureProjection(session, {
        state: state.pending_clarify || state.pending_approval ? 'waiting' : active ? 'running' : error ? 'error' : 'complete',
        finalized: finals, reason: error ? state.inflight.error || state.inflight.error_surface?.code || 'Member turn failed' : undefined })
      return { session_id: session.runtime, session_key: session.stored,
        running: false, inflight: null, messages: [...session.messages], ...state }
    }
    throw new Error(`unexpected RPC: ${method}`)
  }
  gc = await importGroupTurnPlugin({
    atom,
    Date: class extends Date { static now() { return now } },
    // Advance the turn's poll clock; housekeeping debounces execute without
    // adding serial time to the unrelated member deadline.
    setTimeout: (fn, delay = 0) => { if (delay === 2000) now += delay; fn(); return 0 },
    clearTimeout: () => undefined,
    host: {
      request: handle,
      requestProfile: (_route, method, params) => handle(method, params),
      retainProfile: async () => () => { releases++ },
      state: { profile: atom('default'), gateway: atom(null) },
      notify: () => undefined, notifyError: () => undefined
    }
  })
  // These gates exercise the turn engine; the optional server metadata mirror
  // has separate integration coverage and must not retry against this fixture.
  gc.stopGroupChatServerSync()
  gc.$groupChats.set({ Room: { log: [{ id: 'user-1', at: now,
    from: { kind: 'user', name: 'You' }, text: '@all answer', thread: 'thread-1' }],
    watermarks: {}, sessions: {}, epoch: 1, running: true, holds: {}, members: MEMBERS } })
  return { gc, sessions, rpcLog,
    elapsed: () => now - (rpcLog.find(c => c.method === 'prompt.submit')?.at ?? 100000),
    releases: () => releases, calls: method => rpcLog.filter(c => c.method === method) }
}

test('retained terminal error ends at the first poll without waiting for history', async t => {
  const h = await harness(t, { alpha: [{ ...retainedError, messages: [] }] })
  try {
    await assert.rejects(h.gc.runGroupChatMemberTurn('Room', MEMBERS[0], 'prompt', 'thread-1'), error => {
      assert.equal(error.message, 'context lineage failed')
      assert.equal(error.data.reason, 'context lineage failed')
      return true
    })
  } finally {
    t.diagnostic(JSON.stringify({ virtualElapsedMs: h.elapsed(), polls: h.sessions.get('alpha').polls,
      submits: h.calls('prompt.submit').length, providerCalls: 0 }))
  }
  assert.equal(h.sessions.get('alpha').polls, 1)
  assert.equal(h.elapsed(), 2000)
  assert.equal(h.calls('prompt.submit').length, 1)
  assert.equal(h.releases(), 0) // Active-gateway members need no pooled route lease.
  assert.equal(h.gc.$groupChats.get().Room.stranded?.alpha, undefined)
})

test('failed partial assistant history is never published as a healthy reply', async t => {
  const h = await harness(t, { alpha: [{ ...retainedError,
    messages: [{ role: 'assistant', content: 'uncommitted partial result' }] }] })
  await assert.rejects(h.gc.runGroupChatMemberTurn('Room', MEMBERS[0], 'prompt', 'thread-1'))
  assert.equal(h.sessions.get('alpha').polls, 1)
  assert.equal(h.gc.$groupChats.get().Room.log.length, 1)
})

for (const [name, state] of [
  ['running without inflight', { running: true }],
  ['legacy inflight without status', { inflight: { assistant: 'partial', streaming: false } }],
  ['legacy boolean inflight', { inflight: true }],
  ['streaming retained error', { ...retainedError, inflight: { ...retainedError.inflight, streaming: true } }],
  ['running retained error', { ...retainedError, running: true }]
]) {
  test(`${name} keeps waiting for the final reply`, async t => {
    const h = await harness(t, { alpha: [state, { reply: 'finished' }] })
    assert.equal(await h.gc.runGroupChatMemberTurn('Room', MEMBERS[0], 'prompt', 'thread-1'), 'finished')
    assert.equal(h.sessions.get('alpha').polls, 2)
    assert.equal(h.calls('prompt.submit').length, 1)
  })
}

test('idle successful turn returns once on the first poll', async t => {
  const h = await harness(t, { alpha: [{ reply: 'finished' }] })
  assert.equal(await h.gc.runGroupChatMemberTurn('Room', MEMBERS[0], 'prompt', 'thread-1'), 'finished')
  assert.equal(h.sessions.get('alpha').polls, 1)
})

for (const [field, kind] of [['pending_clarify', 'clarify'], ['pending_approval', 'approval']]) {
  test(`${kind} preserves request/session identity and waits even alongside an error`, async t => {
    let mirrored
    const pending = { request_id: 'request-1', question: 'Choose?', command: 'sensitive-command' }
    const h = await harness(t, { alpha: [
      { ...retainedError, [field]: pending }, { ...retainedError, [field]: pending }, { reply: 'after answer' }
    ] }, { onPoll: (_profile, poll, gc) => {
      if (poll === 2) mirrored = gc.$groupClarify.get()['Room::alpha']
      if (poll === 3) assert.equal(gc.$groupClarify.get()['Room::alpha'], mirrored)
    } })
    assert.equal(await h.gc.runGroupChatMemberTurn('Room', MEMBERS[0], 'prompt', 'thread-1'), 'after answer')
    assert.equal(mirrored.kind, kind)
    assert.equal(mirrored.requestId, 'request-1')
    assert.equal(mirrored.sessionId, 'runtime-alpha')
    assert.equal(h.gc.$groupClarify.get()['Room::alpha'], undefined)
    assert.equal(h.calls('prompt.submit').length, 1)
    assert.equal(h.calls('approval.respond').length, 0)
    assert.equal(h.calls('clarify.respond').length, 0)
  })
}

test('serial round emits one failure and starts the next member exactly once', async t => {
  const h = await harness(t, { alpha: [retainedError], beta: [{ reply: '(pass)' }] })
  await h.gc.runGroupChatRounds('Room', MEMBERS, 'thread-1')
  const failures = h.gc.currentGroupActivity('Room').filter(e => e.kind === 'failed')
  assert.equal(failures.length, 1)
  assert.equal(failures[0].member, 'alpha')
  assert.equal(failures[0].reason, 'context lineage failed')
  assert.equal(h.gc.groupActivityLabel(failures[0]), 'alpha hit an error: context lineage failed')
  assert.deepEqual(h.calls('prompt.submit').map(c => c.params.session_id), ['runtime-alpha', 'runtime-beta'])
  assert.equal(h.sessions.get('alpha').polls, 1)
  assert.equal(h.sessions.get('beta').polls, 1)
  assert.equal(h.elapsed(), 4000)
  assert.equal(h.gc.$groupChats.get().Room.running, false)
  assert.equal(h.gc.$groupChats.get().Room.watermarks['thread-1::alpha'], 1)
  await h.gc.runGroupChatRounds('Room', MEMBERS, 'thread-1')
  assert.equal(h.calls('prompt.submit').length, 2)
  assert.equal(h.gc.currentGroupActivity('Room').filter(e => e.kind === 'failed').length, 1)
})

test('a stale epoch drops a completed result and never starts the next member', async t => {
  const h = await harness(t, { alpha: [{ reply: 'obsolete' }] }, {
    onPoll: (_profile, poll, gc) => {
      if (poll !== 1) return
      const room = gc.$groupChats.get().Room
      gc.$groupChats.set({ Room: { ...room, epoch: 2,
        log: [...room.log, { id: 'user-2', from: { kind: 'user', name: 'You' },
          text: 'new request', thread: 'thread-1' }] } })
    }
  })
  await h.gc.runGroupChatRounds('Room', MEMBERS, 'thread-1')
  assert.equal(h.calls('prompt.submit').length, 2, 'frozen independent members admitted before stale completion')
  assert.equal(h.gc.$groupChats.get().Room.log.some(e => e.text === 'obsolete'), false)
  assert.equal(h.gc.$groupChats.get().Room.watermarks['thread-1::alpha'], undefined)
})

test('stale terminal failure advances neither watermark nor next member', async t => {
  const h = await harness(t, { alpha: [retainedError] }, {
    onPoll: (_profile, poll, gc) => {
      if (poll !== 1) return
      const room = gc.$groupChats.get().Room
      gc.$groupChats.set({ Room: { ...room, epoch: 2,
        log: [...room.log, { id: 'user-2', from: { kind: 'user', name: 'You' },
          text: 'new request', thread: 'thread-1' }] } })
    }
  })
  await h.gc.runGroupChatRounds('Room', MEMBERS, 'thread-1')
  assert.equal(h.calls('prompt.submit').length, 2, 'frozen independent members admitted before stale completion')
  assert.equal(h.gc.$groupChats.get().Room.watermarks['thread-1::alpha'], undefined)
  assert.equal(h.gc.$groupChats.get().Room.log.filter(e => e.from.kind === 'member').length, 0)
})

test('duplicate harvest polls consume a failed stranded marker and report once', async t => {
  const h = await harness(t, { alpha: [{ ...retainedError,
    messages: [{ role: 'assistant', content: 'partial failed reply' }] }] })
  // Create a session through the actual turn, then simulate an older stranded marker.
  await h.gc.runGroupChatMemberTurn('Room', MEMBERS[0], 'prompt', 'thread-1').catch(() => undefined)
  const room = h.gc.$groupChats.get().Room
  h.gc.$groupChats.set({ Room: { ...room, stranded: { alpha: { delivery: { accepted_turn: h.sessions.get('alpha').accepted_turn, member_key: 'alpha', owner: { name: 'alpha' } }, thread: 'thread-1', epoch: room.epoch, anchor_id: room.log.at(-1)?.id } } } })
  await Promise.all([1, 2].map(() => h.gc.harvestStrandedGroupReply('Room', MEMBERS[0])))
  await h.gc.harvestStrandedGroupReply('Room', MEMBERS[0])
  assert.equal(h.gc.$groupChats.get().Room.stranded.alpha, undefined)
  assert.equal(h.gc.currentGroupActivity('Room').filter(e => e.kind === 'failed').length, 1)
  assert.equal(h.gc.$groupChats.get().Room.log.filter(e => e.from.kind === 'member').length, 0)
  assert.equal(h.calls('prompt.submit').length, 1)
})

test('a routed retained-error turn releases its session lease exactly once', async t => {
  const h = await harness(t, { alpha: [retainedError] })
  const member = { ...MEMBERS[0], connectionId: 'mini', remoteSource: true }
  await assert.rejects(h.gc.runGroupChatMemberTurn('Room', member, 'prompt', 'thread-1'))
  assert.equal(h.releases(), 1)
  assert.equal(h.calls('prompt.submit').length, 1)
  assert.equal(h.calls('prompt.submit')[0].params.session_id, 'runtime-alpha')
  assert.equal(h.gc.$groupChats.get().Room.sessions['mini::alpha'], 'stored-alpha')
})

for (const [inflight, message] of [
  [{ status: 'error', streaming: false, error_surface: { code: 'CONTEXT_PREFLIGHT_EXHAUSTED' } }, 'CONTEXT_PREFLIGHT_EXHAUSTED'],
  [{ status: 'error', streaming: false }, 'Member turn failed']
]) {
  test(`terminal errors without text surface ${message}`, async t => {
    const h = await harness(t, { alpha: [{ running: false, inflight, messages: [] }] })
    await assert.rejects(h.gc.runGroupChatMemberTurn('Room', MEMBERS[0], 'prompt', 'thread-1'), error => {
      assert.equal(error.message, message)
      assert.equal(error.data.reason, message)
      return true
    })
    assert.equal(h.sessions.get('alpha').polls, 1)
  })
}

test('pending clarification extends a retained-error turn beyond the base deadline', async t => {
  const waiting = { ...retainedError, pending_clarify: { request_id: 'request-long', question: 'Choose?' } }
  const h = await harness(t, { alpha: [...Array.from({ length: 100 }, () => waiting), { reply: 'after answer' }] })
  assert.equal(await h.gc.runGroupChatMemberTurn('Room', MEMBERS[0], 'prompt', 'thread-1'), 'after answer')
  assert.equal(h.sessions.get('alpha').polls, 101)
  assert.equal(h.elapsed(), 202000)
  assert.equal(h.calls('prompt.submit').length, 1)
})

test('a harvest racing a replacement marker does not consume or publish the newer work', async t => {
  let replaceMarker = false
  const newer = { before: 17, thread: 'thread-new' }
  const h = await harness(t, { alpha: [retainedError] }, {
    onPoll: (_profile, _poll, gc) => {
      if (!replaceMarker) return
      const room = gc.$groupChats.get().Room
      gc.$groupChats.set({ Room: { ...room, stranded: { alpha: newer } } })
    }
  })
  await h.gc.runGroupChatMemberTurn('Room', MEMBERS[0], 'prompt', 'thread-1').catch(() => undefined)
  const room = h.gc.$groupChats.get().Room
  h.gc.$groupChats.set({ Room: { ...room, stranded: { alpha: { delivery: { accepted_turn: h.sessions.get('alpha').accepted_turn, member_key: 'alpha', owner: { name: 'alpha' } }, thread: 'thread-1', epoch: room.epoch, anchor_id: room.log.at(-1)?.id } } } })
  replaceMarker = true
  await h.gc.harvestStrandedGroupReply('Room', MEMBERS[0])
  assert.equal(h.gc.$groupChats.get().Room.stranded.alpha, newer)
  assert.equal(h.gc.currentGroupActivity('Room').filter(e => e.kind === 'failed').length, 0)
})
