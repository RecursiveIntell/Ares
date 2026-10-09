import assert from 'node:assert/strict'
import test from 'node:test'
import { harness, members, deferred, flush, drive } from './stop-custody-harness.mjs'

const boundedTest = (name, body) => test(name, { timeout: 10000 }, body)
const clone = value => JSON.parse(JSON.stringify(value))
const receipts = h => Object.values(h.room().stranded || {})
const cards = h => Object.values(h.gc.$groupClarify.get())
const nodes = tree => {
  const result = []
  const walk = value => {
    if (Array.isArray(value)) value.forEach(walk)
    else if (value && typeof value === 'object') { result.push(value); walk(value.props?.children) }
  }
  walk(tree)
  return result
}

// Render the shipped components with stateful React ports. Event handlers,
// answer ownership and the Stop visibility predicate remain production code.
function renderer() {
  const states = []
  let cursor = 0
  return {
    react: {
      useState: initial => {
        const index = cursor++
        if (!(index in states)) states[index] = typeof initial === 'function' ? initial() : initial
        return [states[index], value => { states[index] = typeof value === 'function' ? value(states[index]) : value }]
      },
      useRef: current => ({ current }), useEffect: () => {}, useMemo: factory => factory()
    },
    sdk: { useValue: atom => atom.get(), cn: (...values) => values.filter(Boolean).join(' '),
      profileColor: () => '#000000', relativeTime: () => '' },
    document: { getElementById: () => true, addEventListener: () => {}, removeEventListener: () => {} },
    setInterval: () => 1, clearInterval: () => {},
    render: (component, props, fresh = false) => {
      cursor = 0
      if (fresh) states.length = 0
      return component(props)
    }
  }
}

async function uiHarness(roster = members(1), options = {}) {
  const ui = renderer(), resumeProjection = options.resumeProjection
  Object.assign(options, ui, {
    resumeProjection: (session, method, projection) => {
      if (session.approval && session.state === 'waiting') projection.pending_approval = session.approval
      return resumeProjection?.(session, method, projection) || projection
    } })
  const h = await harness(roster, options)
  h.ui = ui
  h.workspace = () => ui.render(h.gc.GroupChatWorkspace,
    { group: 'Room', members: h.roster, onBack: () => {} }, true)
  h.stopButton = () => nodes(h.workspace()).find(node => node.type === 'button' &&
    node.props?.title?.startsWith('Stop this run'))
  return h
}

function preparedAnswer(h, entry = cards(h)[0]) {
  assert.ok(entry, 'the actual pending projection exposes a card')
  const props = { entry, members: h.roster }
  let tree = h.ui.render(h.gc.GroupClarifyCard, props, true)
  if (entry.kind === 'approval') {
    const choice = nodes(tree).find(node => node.props?.children === 'once' && node.props?.onClick)
    assert.ok(choice); choice.props.onClick()
  } else {
    const inputs = nodes(tree).filter(node => node.props?.['aria-label'] === `Answer @${entry.member}`)
    assert.equal(inputs.length, entry.questions?.length || 1)
    inputs.forEach((node, index) => node.props.onChange({ target: { value: `ANSWER_${index + 1}` } }))
  }
  tree = h.ui.render(h.gc.GroupClarifyCard, props)
  const submit = nodes(tree).find(node => ['Answer', 'Respond'].includes(node.props?.children) && node.props?.onClick)
  assert.ok(submit); assert.equal(submit.props.disabled, false)
  return () => submit.props.onClick()
}

async function park(h, kind = 'single') {
  const pending = drive(h)
  await flush()
  const session = [...h.sessions.values()][0]
  assert.ok(session)
  session.state = 'waiting'
  session.pending = kind === 'batch'
    ? { request_id: 'question-1', questions: [{ qid: 'first', question: 'First?' }, { qid: 'second', question: 'Second?' }] }
    : { request_id: 'question-1', question: 'Choose?', choices: [] }
  if (kind === 'approval') {
    session.pending = null
    session.approval = { request_id: 'question-1', command: 'echo offline', choices: ['once', 'deny'] }
  }
  await h.advance(); await pending
  if (kind === 'approval' && !cards(h).length) {
    h.gc.syncGroupClarify('Room', h.roster[0], {
      session_id: session.runtime, pending_approval: { request_id: 'question-1', command: 'echo offline', choices: ['once', 'deny'] }
    }, h.roster[0])
  }
  assert.equal(receipts(h).length, 1)
  return session
}

for (const kind of ['single', 'batch', 'approval']) {
  boundedTest(`actual ${kind} card answer preserves accepted ownership and delivers one final`, async () => {
    const h = await uiHarness(), session = await park(h, kind)
    const accepted = clone(session.ref), beforeVersion = h.room().threadInputVersions?.t1 || 0
    session.text = `FINAL_${kind}`
    preparedAnswer(h)(); await flush()
    await h.until(() => h.posts().some(entry => entry.text === `FINAL_${kind}`))
    await h.gc.harvestStrandedGroupReply('Room', h.roster[0]); await flush()
    assert.equal(h.rpc(kind === 'approval' ? 'approval.respond' : 'clarify.respond').length, kind === 'batch' ? 2 : 1)
    assert.deepEqual(session.ref, accepted)
    assert.equal(h.room().threadInputVersions?.t1 || 0, beforeVersion)
    assert.equal(h.posts().filter(entry => entry.text === `FINAL_${kind}`).length, 1)
    assert.equal(h.rpc('prompt.submit').length, 1, 'accepted prompt is never replayed')
    assert.ok(h.room().log.some(entry => entry.from.kind === 'user' && entry.text.includes(kind === 'approval' ? 'once' : 'ANSWER_1')))
  })
}

for (const kind of ['single', 'batch', 'approval']) {
  boundedTest(`failed ${kind} card answer produces no successful echo`, async () => {
    const h = await uiHarness(members(1), { answerError: () => true })
    await park(h, kind)
    const before = clone(h.room().log)
    preparedAnswer(h)(); await flush(); await h.advance()
    assert.deepEqual(h.room().log, before)
    assert.equal(h.posts().length, 0)
    assert.equal(receipts(h).length, 1)
    await h.gc.stopGroupThread('Room', 't1', h.roster)
  })
}

boundedTest('remote projected answer echo preserves ownership; genuine remote input still supersedes', async () => {
  const h = await uiHarness(members(1), { resumeRunning: true }), session = await park(h)
  const preAnswer = { ...h.room(), log: [...h.room().log] }, accepted = clone(session.ref)
  preparedAnswer(h)(); await flush()
  const snapshot = h.gc.groupChatSyncSnapshot()
  const echo = Object.values(snapshot.rooms)[0].log.find(entry => entry.id !== 'user-1' && entry.from.kind === 'user')
  assert.ok(echo, 'sync projection retains the visible exchange')
  const merged = h.gc.mergeRemoteGroupChatSnapshotIntoRooms(snapshot, { Room: preAnswer })
  assert.equal(merged.Room.threadInputVersions?.t1 || 0, preAnswer.threadInputVersions?.t1 || 0)
  h.gc.$groupChats.set(merged)
  session.state = 'complete'; session.text = 'AFTER_REMOTE_ECHO'
  await h.gc.harvestStrandedGroupReply('Room', h.roster[0]); await h.advance()
  assert.equal(h.posts().filter(entry => entry.text === 'AFTER_REMOTE_ECHO').length, 1)
  assert.deepEqual(session.ref, accepted)

  const control = await uiHarness(), old = await park(control)
  const remote = control.gc.groupChatSyncSnapshot(), epoch = control.room().epoch
  Object.values(remote.rooms)[0].log.push({ id: 'remote-genuine', at: 100050,
    from: { kind: 'user', name: 'You' }, text: 'GENUINE_NEW_INSTRUCTION', thread: 't1' })
  control.gc.$groupChats.set(control.gc.mergeRemoteGroupChatSnapshotIntoRooms(remote, control.gc.$groupChats.get()))
  assert.equal(control.room().epoch, epoch, 'remote supersession is independent of epoch')
  old.state = 'complete'; old.pending = null; old.text = 'STALE_RESULT'
  await control.gc.harvestStrandedGroupReply('Room', control.roster[0]); await control.advance()
  assert.equal(control.posts().filter(entry => entry.text === 'STALE_RESULT').length, 0)
})

async function reload(hot, options = {}) {
  const h = await uiHarness(hot.roster, options), durable = clone(hot.gc.durableGroupChatRooms())
  for (const [id, session] of hot.sessions) h.sessions.set(id, clone(session))
  h.gc.$groupChats.set({})
  h.gc.default.register({ storage: { get: key => key === 'group-chats' ? durable : null,
    set: (key, value) => h.storage.set(key, clone(value)) }, register: () => {}, onDispose: () => {} })
  await flush(); h.gc.stopGroupChatServerSync()
  assert.equal(h.room().running, false)
  assert.equal(h.room().turns?.length || 0, 0)
  assert.equal(cards(h).length, 0)
  assert.equal(h.gc.currentGroupActivity('Room').length, 0)
  return h
}

for (const failsFirst of [false, true]) {
  boundedTest(`cold Stop clears card and fences captured Answer; retry=${failsFirst}`, async () => {
    const hot = await uiHarness(); await park(hot)
    const options = { interruptError: failsFirst }, cold = await reload(hot, options)
    await cold.gc.harvestStrandedGroupReply('Room', cold.roster[0])
    assert.equal(cards(cold).length, 1)
    const staleClick = preparedAnswer(cold), accepted = clone(receipts(cold)[0].delivery.accepted_turn)
    await cold.gc.stopGroupThread('Room', 't1', cold.roster)
    assert.equal(cards(cold).length, 0)
    staleClick(); await flush()
    assert.equal(cold.rpc('clarify.respond').length, 0, 'a captured stale handler cannot authorize an RPC')
    assert.equal(receipts(cold).length, failsFirst ? 1 : 0)
    if (failsFirst) {
      assert.ok(receipts(cold)[0].stop_requested)
      options.interruptError = false
      await cold.gc.stopGroupThread('Room', 't1', cold.roster)
      await flush() // control completion precedes exact terminal observation
      assert.deepEqual(cold.rpc('session.interrupt').map(call => call.params.session_id), [accepted.session_id, accepted.session_id])
      assert.equal(receipts(cold).length, 0)
    }
    await hot.gc.stopGroupThread('Room', 't1', hot.roster)
  })
}

boundedTest('cold current card accepts equivalent cloned receipt metadata after fresh waiting sync', async () => {
  const hot = await uiHarness(); await park(hot)
  const cold = await reload(hot)
  await cold.gc.harvestStrandedGroupReply('Room', cold.roster[0])
  const original = receipts(cold)[0], accepted = clone(original.delivery.accepted_turn)
  cold.gc.updateGroupChat('Room', room => ({ ...room, stranded: {
    ...room.stranded, 'local::bot1': { ...clone(original), reported: true, reason: 'temporarily unavailable' }
  } }))
  assert.notEqual(receipts(cold)[0], original, 'metadata reconciliation clones the receipt object')
  assert.deepEqual(receipts(cold)[0].delivery.accepted_turn, accepted)
  await cold.gc.harvestStrandedGroupReply('Room', cold.roster[0])
  const session = [...cold.sessions.values()][0]
  session.text = 'CLONED_RECEIPT_RESULT'
  preparedAnswer(cold)(); await flush()
  assert.equal(cold.rpc('clarify.respond').length, 1, 'same accepted identity keeps its current card actionable')
  await cold.gc.harvestStrandedGroupReply('Room', cold.roster[0])
  assert.equal(cold.posts().filter(entry => entry.text === 'CLONED_RECEIPT_RESULT').length, 1)
  await hot.gc.stopGroupThread('Room', 't1', hot.roster)
})

for (const changed of ['accepted tuple', 'source']) {
  boundedTest(`replacement cold receipt with changed ${changed} fences captured Answer`, async () => {
    const hot = await uiHarness(); await park(hot)
    const cold = await reload(hot)
    await cold.gc.harvestStrandedGroupReply('Room', cold.roster[0])
    const staleClick = preparedAnswer(cold), replacement = clone(receipts(cold)[0])
    if (changed === 'accepted tuple') replacement.delivery.accepted_turn.request_id = 'different-accepted-request'
    else replacement.delivery.owner.route.connectionId = 'different-source'
    cold.gc.updateGroupChat('Room', room => ({ ...room,
      stranded: { ...room.stranded, 'local::bot1': replacement } }))
    staleClick(); await flush()
    assert.equal(cold.rpc('clarify.respond').length, 0)
    assert.equal(cold.room().log.filter(entry => entry.from.kind === 'user').length, 1)
    assert.equal(receipts(cold).length, 1, 'replacement acceptance custody survives rejection')
    await hot.gc.stopGroupThread('Room', 't1', hot.roster)
  })
}

boundedTest('old answer completion preserves an unrelated replacement question card', async () => {
  const answerAckGate = deferred(), h = await uiHarness(members(1), { answerAckGate })
  const session = await park(h)
  preparedAnswer(h)(); await flush()
  assert.equal(h.rpc('clarify.respond').length, 1)
  session.state = 'waiting'
  session.pending = { request_id: 'replacement-question', question: 'A distinct question?' }
  h.gc.syncGroupClarify('Room', h.roster[0], {
    session_id: session.runtime, pending_clarify: session.pending
  }, h.roster[0])
  const replacement = cards(h)[0]
  assert.equal(replacement.requestId, 'replacement-question')
  answerAckGate.resolve(); await flush(); await h.advance()
  assert.equal(cards(h)[0], replacement)
  assert.equal(cards(h)[0].requestId, 'replacement-question')
  await h.gc.stopGroupThread('Room', 't1', h.roster)
})

boundedTest('actual UI exposes cold Stop, retains it after failed interrupt and reload, then removes retry', async () => {
  const hot = await uiHarness(); await park(hot)
  const cold = await reload(hot, { interruptError: true }), first = cold.stopButton()
  assert.ok(first, 'durable accepted custody exposes the actual Stop button without transient state')
  const accepted = clone(receipts(cold)[0].delivery.accepted_turn)
  first.props.onClick(); await flush()
  assert.equal(receipts(cold).length, 1)
  assert.ok(cold.room().holds['local::bot1'])
  const retried = await reload(cold), retry = retried.stopButton()
  assert.ok(retry, 'unconfirmed held custody remains actionable after reload')
  retry.props.onClick(); await flush()
  assert.deepEqual(retried.rpc('session.interrupt').map(call => [call.route.connectionId, call.params.session_id]),
    [['local', accepted.session_id]])
  assert.equal(receipts(retried).length, 0)
  assert.ok(retried.room().holds['local::bot1'])
  assert.equal(retried.stopButton(), undefined)
  await hot.gc.stopGroupThread('Room', 't1', hot.roster)
})

for (const cold of [false, true]) {
  boundedTest(`actual Stop button projects pending retirement honestly after applied ACK; cold=${cold}`, async () => {
    const notices = [], options = { interruptReply: { status: 'interrupted' }, onNotify: value => notices.push(value) }
    const hot = await uiHarness(members(1), cold ? {} : options), pending = drive(hot)
    await flush()
    const h = cold ? await reload(hot, options) : hot
    const accepted = clone(receipts(h)[0].delivery.accepted_turn), button = h.stopButton()
    assert.ok(button)
    button.props.onClick(); await flush()
    assert.equal(notices.length, 1)
    assert.equal(notices[0].kind, 'info')
    assert.match(notices[0].message, /^Stopping Room/)
    assert.match(notices[0].message, /waiting for the remaining turns to finish/)
    assert.doesNotMatch(notices[0].message, /unconfirmed|Stop can retry/)
    assert.deepEqual(receipts(h)[0].delivery.accepted_turn, accepted)
    assert.equal(h.rpc('session.interrupt').length, 1)
    assert.equal(h.rpc('prompt.submit').length, cold ? 0 : 1)
    if (!cold) {
      assert.equal(h.activeLeases(), 1)
      assert.equal(h.gc.groupRoomCoordinators.get('Room').active, 1)
    }
    const session = [...h.sessions.values()][0]
    session.state = 'interrupted'; session.pending = null
    await h.gc.harvestStrandedGroupReply('Room', h.roster[0]); await h.advance()
    assert.equal(receipts(h).length, 0)
    if (cold) await hot.gc.stopGroupThread('Room', 't1', hot.roster)
    await hot.advance(); await pending
  })
}

boundedTest('actual Stop button keeps failed interruption wording and retry custody', async () => {
  const notices = [], h = await uiHarness(members(1), { interruptError: true, onNotify: value => notices.push(value) })
  const pending = drive(h); await flush()
  const accepted = clone(receipts(h)[0].delivery.accepted_turn)
  h.stopButton().props.onClick(); await flush(); await h.advance(); await pending
  assert.equal(notices.length, 1)
  assert.equal(notices[0].kind, 'info')
  assert.match(notices[0].message, /1 interruption\(s\) are unconfirmed.*Stop can retry/)
  assert.deepEqual(receipts(h)[0].delivery.accepted_turn, accepted)
  assert.equal(h.activeLeases(), 1)
  assert.ok(h.stopButton())
  h.finish('bot1', '', 'interrupted')
  await h.gc.harvestStrandedGroupReply('Room', h.roster[0])
  assert.equal(h.activeLeases(), 0)
})

boundedTest('legacy unknown receipt does not manufacture an interrupt target or Stop affordance', async () => {
  const h = await uiHarness()
  h.gc.updateGroupChat('Room', room => ({ ...room, running: false, turns: [],
    stranded: { 'local::bot1': { before: 1, thread: 't1', reported: true } } }))
  assert.equal(h.stopButton(), undefined)
  await h.gc.stopGroupThread('Room', 't1', h.roster)
  assert.equal(h.rpc('session.interrupt').length, 0)
  assert.equal(receipts(h).length, 1)
})

boundedTest('late accepted answer drives its mentioned teammate once with peer delta', async () => {
  const h = await uiHarness(members(2))
  h.gc.updateGroupChat('Room', room => {
    room.log[0].text = '@bot1 FIRST_INPUT'
    room.log[0].images = [{ kind: 'file', data: 'ORIGINAL_ATTACHMENT', name: 'original.txt' }]
    return room
  })
  const session = await park(h)
  assert.equal(h.rpc('prompt.submit').length, 1)
  assert.equal(h.room().running, false, 'first drive is quiescent while the accepted turn waits')
  session.text = 'LATE_PEER_REPLY @bot2'
  preparedAnswer(h)(); await flush()
  await h.until(() => h.rpc('prompt.submit').some(call => call.route.profile === 'bot2'))
  h.finish('bot2')
  await h.advance(); await h.gc.harvestStrandedGroupReply('Room', h.roster[0])
  await h.gc.harvestStrandedGroupReply('Room', h.roster[0]); await h.advance()
  const teammate = h.rpc('prompt.submit').filter(call => call.route.profile === 'bot2')
  assert.equal(teammate.length, 1)
  assert.match(teammate[0].params.text, /LATE_PEER_REPLY/)
  assert.doesNotMatch(teammate[0].params.text, /FIRST_INPUT/)
  assert.equal(h.calls.filter(call => call.method.includes('attach') && call.route.profile === 'bot2').length, 0)
  assert.equal(h.rpc('prompt.submit').filter(call => call.route.profile === 'bot1').length, 1)
  assert.ok(h.maxLeases() <= 4)
  assert.ok(h.posts().length <= 10)
})

boundedTest('new user send in another thread preserves the parked thread late-answer handoff', async () => {
  const h = await uiHarness(members(2))
  h.gc.updateGroupChat('Room', room => { room.log[0].text = '@bot1 FIRST_INPUT'; return room })
  const session = await park(h), originalEpoch = h.room().epoch
  h.gc.sendToGroupChat('Room', h.roster, '@bot1 OTHER_THREAD_INSTRUCTION', 't2')
  await h.advance(250)
  assert.ok(h.room().epoch > originalEpoch, 'ordinary Send advances the room drive generation')
  assert.equal(h.rpc('prompt.submit').length, 1, 'parked A keeps its accepted custody while t2 is blocked')
  session.text = 'OLDER_THREAD_PEER_REPLY @bot2'
  preparedAnswer(h)(); await flush()
  await h.until(() => h.rpc('prompt.submit').some(call => call.route.profile === 'bot2'))
  h.finish('bot2'); await h.advance()
  await h.gc.harvestStrandedGroupReply('Room', h.roster[0]); await h.advance()
  const teammate = h.rpc('prompt.submit').filter(call => call.route.profile === 'bot2')
  assert.equal(teammate.length, 1)
  assert.match(teammate[0].params.text, /OLDER_THREAD_PEER_REPLY/)
  assert.doesNotMatch(teammate[0].params.text, /FIRST_INPUT|OTHER_THREAD_INSTRUCTION/)
  assert.equal(h.posts().filter(entry => entry.text === 'OLDER_THREAD_PEER_REPLY @bot2' && entry.thread === 't1').length, 1)
})

for (const invalidation of ['Stop', 'newer input', 'replacement']) {
  boundedTest(`${invalidation} prevents stale late-answer teammate continuation`, async () => {
    const h = await uiHarness(members(2), { resumeRunning: true })
    h.gc.updateGroupChat('Room', room => { room.log[0].text = '@bot1 FIRST_INPUT'; return room })
    const session = await park(h)
    preparedAnswer(h)(); await flush()
    if (invalidation === 'Stop') await h.gc.stopGroupThread('Room', 't1', h.roster)
    else if (invalidation === 'newer input') h.gc.appendGroupChatEntry('Room', { kind: 'user', name: 'You' },
      'GENUINE_NEW_INSTRUCTION', 't1')
    else h.gc.updateGroupChat('Room', room => ({ ...room, roomId: 'replacement-room', epoch: room.epoch + 1 }))
    session.state = 'complete'; session.pending = null; session.text = 'STALE_HANDOFF @bot2'
    await h.gc.harvestStrandedGroupReply('Room', h.roster[0]); await h.advance(5000)
    assert.equal(h.posts().filter(entry => entry.text.includes('STALE_HANDOFF')).length, 0)
    assert.equal(h.rpc('prompt.submit').filter(call => call.route.profile === 'bot2').length, 0)
    assert.equal(h.rpc('prompt.submit').filter(call => call.route.profile === 'bot1').length, 1)
  })
}

for (const terminal of ['complete', 'error']) {
  boundedTest(`publication capacity backfills after four ${terminal === 'complete' ? 'passes' : 'terminal failures'}`, async () => {
    const secondAdmitted = new Set(), maxWorkers = { value: 0 }
    let h
    const options = { resumeProjection: (session, method, projection) => {
      if (method !== 'session.turn.poll' || !session.ref) return projection
      maxWorkers.value = Math.max(maxWorkers.value, h.gc.groupRoomCoordinators.get('Room').active)
      if (session.submits === 2) secondAdmitted.add(session.profile)
      const omitted = session.submits === 2 && [...secondAdmitted].indexOf(session.profile) < 4
      session.state = omitted ? terminal : 'complete'
      session.text = session.submits === 1 ? `ROUND_ONE_${session.profile} @all`
        : omitted || session.submits > 2 ? '(pass)' : `BACKFILL_${session.profile}`
      projection.running = false
      projection.turn_outcomes.turns[0] = { accepted_turn: { ...session.ref }, state: session.state,
        finalized: [{ status: session.state, text: session.text, ...(session.state === 'error' ? { error: 'offline terminal failure' } : {}) }] }
      return projection
    } }
    h = await uiHarness(members(6), options)
    const pending = drive(h)
    await h.until(() => !h.room().running); await pending
    assert.equal(secondAdmitted.size, 6, 'all frozen second-round jobs get their freed-capacity admission')
    assert.equal(h.posts().filter(entry => entry.text.startsWith('ROUND_ONE_')).length, 6)
    assert.equal(h.posts().filter(entry => entry.text.startsWith('BACKFILL_')).length, 2)
    assert.ok(maxWorkers.value <= 4)
    assert.ok(h.posts().length <= 10)
    for (const session of h.sessions.values()) {
      const prompts = h.rpc('prompt.submit').filter(call => call.params.session_id === session.runtime)
      assert.ok(prompts.length >= 2)
      assert.doesNotMatch(prompts[1].params.text, /FIRST_INPUT/, 'backfill receives its frozen peer delta without replay')
    }
  })
}

for (const invalidation of ['Stop', 'newer input']) {
boundedTest(`unresolved waiting reservations retain capacity; ${invalidation} cancels frozen excess jobs`, async () => {
  const secondAdmitted = new Set()
  const h = await uiHarness(members(6), { resumeProjection: (session, method, projection) => {
    if (method !== 'session.turn.poll' || !session.ref) return projection
    if (session.state === 'interrupted') return projection // preserve exact terminal after fake Stop
    if (session.submits === 2) secondAdmitted.add(session.profile)
    session.state = session.submits === 1 ? 'complete' : 'waiting'
    session.text = `FIRST_${session.profile} @all`
    if (session.state === 'waiting') {
      session.pending = { request_id: `waiting-${session.profile}`, question: 'Continue?' }
      projection.pending_clarify = session.pending
    }
    projection.running = false
    projection.turn_outcomes.turns[0] = { accepted_turn: { ...session.ref }, state: session.state,
      finalized: session.state === 'complete' ? [{ status: 'complete', text: session.text }] : [] }
    return projection
  } })
  const pending = drive(h)
  await h.until(() => secondAdmitted.size === 4)
  await h.advance(); await pending
  assert.equal(secondAdmitted.size, 4, 'waiting may release workers, but its publication custody stays reserved')
  assert.equal(h.posts().length, 6)
  assert.equal(receipts(h).length, 4)
  const accepted = receipts(h).map(marker => clone(marker.delivery.accepted_turn))
  if (invalidation === 'Stop') await h.gc.stopGroupThread('Room', 't1', h.roster)
  else h.gc.appendGroupChatEntry('Room', { kind: 'user', name: 'You' }, 'NEWER_GENUINE_INPUT', 't1')
  await h.advance()
  assert.equal(secondAdmitted.size, 4, 'superseded budget-pending frozen jobs never start')
  assert.equal(h.rpc('prompt.submit').length, 10)
  assert.equal(h.posts().length, 6)
  const coordinator = h.gc.groupRoomCoordinators.get('Room')
  assert.equal(coordinator.occurrences.size, invalidation === 'Stop' ? 0 : 4,
    'only unresolved accepted occurrences retain custody')
  if (invalidation === 'newer input') {
    assert.deepEqual(receipts(h).map(marker => marker.delivery.accepted_turn), accepted)
    assert.equal(h.rpc('session.interrupt').length, 0, 'newer intent does not prove old accepted work ended')
    await h.gc.stopGroupThread('Room', 't1', h.roster)
  }
})
}

boundedTest('chatty continuations share the cumulative ten-message cap and four-worker ceiling', async () => {
  let h, maxWorkers = 0
  h = await uiHarness(members(6), { resumeProjection: (session, method, projection) => {
    if (method !== 'session.turn.poll' || !session.ref) return projection
    maxWorkers = Math.max(maxWorkers, h.gc.groupRoomCoordinators.get('Room').active)
    session.state = 'complete'; session.text = `CHATTER_${session.profile}_${session.submits} @all`
    projection.running = false
    projection.turn_outcomes.turns[0] = { accepted_turn: { ...session.ref }, state: 'complete',
      finalized: [{ status: 'complete', text: session.text }] }
    return projection
  } })
  const pending = drive(h)
  await h.until(() => !h.room().running); await pending
  assert.equal(h.posts().length, 10)
  assert.equal(h.rpc('prompt.submit').length, 10)
  assert.ok(maxWorkers <= 4)
  assert.equal(h.gc.groupRoomCoordinators.get('Room').occurrences.size, 0,
    'the message cap releases never-admitted frozen occurrences')
  await h.advance(5000)
  assert.equal(h.posts().length, 10)
  assert.equal(h.rpc('prompt.submit').length, 10)
})
