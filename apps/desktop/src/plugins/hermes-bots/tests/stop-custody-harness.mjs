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
      if (options.interruptBehavior) return options.interruptBehavior(session)
      if (options.interruptError) throw new Error('simulated lost interrupt request or ACK')
      if (options.interruptReply !== undefined) return options.interruptReply
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
        if (options.onRetain) await options.onRetain(route)
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


export { harness, members, deferred, flush, drive };
