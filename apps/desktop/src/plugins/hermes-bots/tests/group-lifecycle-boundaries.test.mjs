import assert from 'node:assert/strict'
import test from 'node:test'
import { harness, members, deferred, flush, drive } from './stop-custody-harness.mjs'

// Offline only: the shipped plugin runs against fake gateway/profile ports and
// a bounded fake clock. No provider, backend process or installed state is used.
const bounded = (name, body) => test(name, { timeout: 10000 }, body)
const clone = value => JSON.parse(JSON.stringify(value))
const receipt = (h, member = h.roster[0], name = 'Room') =>
  h.gc.$groupChats.get()[name]?.stranded?.[h.gc.groupMemberKey(member)]
const coordinator = (h, name = 'Room') => h.gc.groupRoomCoordinators.get(name)
const posts = (h, name = 'Room') => h.gc.$groupChats.get()[name]?.log.filter(e => e.from.kind === 'member') || []
const sessionFor = (h, profile = 'bot1') => [...h.sessions.values()].find(s => s.profile === profile)
const cards = h => Object.values(h.gc.$groupClarify.get())

async function settle(h, pending, name = 'Room') {
  for (let i = 0; i < 30 && coordinator(h, name)?.occurrences.size; i++) {
    for (const session of h.sessions.values()) { session.state = 'complete'; session.pending = null }
    await h.advance()
    for (const member of h.roster) await h.gc.harvestStrandedGroupReply(name, member)
  }
  if (pending) await pending
}

for (const terminal of ['complete', 'error', 'interrupted']) {
  bounded(`GC-TERM: saturated waiting collector retires ${terminal} without a worker reservation`, async () => {
    const h = await harness(members(6)), pending = drive(h)
    await flush()
    assert.equal(h.rpc('prompt.submit').length, 4)
    const parked = sessionFor(h), accepted = clone(parked.ref)
    parked.state = 'waiting'; parked.pending = { request_id: 'question-1', question: 'Choose?' }
    await h.advance()
    assert.equal(h.rpc('prompt.submit').length, 5, 'the fifth member uses the positively waiting slot')
    assert.equal(coordinator(h).active, 4)
    assert.equal(coordinator(h).queue.length, 1)
    parked.state = terminal; parked.pending = null; parked.text = 'TERMINAL_WITH_ALL_SLOTS_BUSY'
    await h.advance(); await flush()
    assert.equal(receipt(h), undefined, 'exact terminal does not wait behind the four busy workers')
    assert.equal(h.leases.find(l => l.route.targetProfile === 'bot1').releases, 1)
    assert.equal(coordinator(h).active, 4)
    assert.equal(h.rpc('prompt.submit').length, 5, 'terminal collection does not manufacture a sixth worker')
    assert.deepEqual(parked.ref, accepted)
    assert.equal(posts(h).filter(e => e.text === 'TERMINAL_WITH_ALL_SLOTS_BUSY').length, terminal === 'complete' ? 1 : 0)
    await settle(h, pending)
    assert.equal(coordinator(h).active, 0)
    assert.ok(h.leases.every(l => l.releases === 1))
  })
}

bounded('GC-HOLD: typed hold retains running custody and release never replays or publishes the held turn', async () => {
  const h = await harness(members(1)), pending = drive(h)
  await flush()
  const first = sessionFor(h), accepted = clone(first.ref), lease = h.leases[0]
  h.gc.sendToGroupChat('Room', h.roster, '@bot1 stop', 'hold-thread')
  await h.advance(); await pending
  assert.deepEqual(receipt(h)?.delivery.accepted_turn, accepted)
  assert.equal(lease.releases, 0, 'a future-turn hold is not terminal evidence')
  assert.equal(coordinator(h).active, 1)
  assert.equal(h.rpc('session.interrupt').length, 0)
  assert.equal(posts(h).length, 0)
  h.gc.sendToGroupChat('Room', h.roster, '@bot1 continue with NEW_INPUT', 'new-thread')
  await h.advance()
  assert.equal(h.rpc('prompt.submit').length, 1, 'accepted work owns the member until exact retirement')
  assert.equal(lease.releases, 0)
  first.state = 'complete'; first.pending = null; first.text = 'HELD_OLD_RESULT'
  await h.gc.harvestStrandedGroupReply('Room', h.roster[0]); await flush(); await h.advance()
  assert.equal(posts(h).filter(e => e.text === 'HELD_OLD_RESULT').length, 0)
  assert.equal(lease.releases, 1)
  // An instruction shown as blocked requires a fresh explicit drive. It is
  // not silently replayed when an unknown old turn later becomes terminal.
  void h.gc.runGroupChatRounds('Room', h.roster, 'new-thread')
  await flush(); await h.advance()
  assert.equal(h.rpc('prompt.submit').length, 2, 'the new instruction admits once after retirement')
  assert.ok(h.rpc('prompt.submit')[1].params.text.includes('NEW_INPUT'))
  await settle(h)
})

for (const unavailable of [false, true]) {
  bounded(`GC-ACK: application ACK retains hot capacity while outcome is ${unavailable ? 'unavailable' : 'running'}`, async () => {
    const options = { interruptReply: { status: 'interrupted' }, unavailable }
    const h = await harness(members(6), options), pending = drive(h)
    await flush()
    const accepted = [...h.sessions.values()].map(s => clone(s.ref))
    const result = await h.gc.stopGroupThread('Room', 't1', h.roster)
    await h.advance(); await pending
    assert.equal(result.status, 'stopping')
    assert.equal(result.unconfirmed, 0)
    assert.equal(Object.keys(h.room().stranded).length, 4)
    assert.equal(coordinator(h).active, 4)
    assert.equal(h.activeLeases(), 4)
    assert.ok(h.leases.every(l => l.releases === 0))
    const repeats = await h.gc.stopGroupThread('Room', 't1', h.roster)
    assert.equal(repeats.status, 'stopping')
    assert.equal(h.rpc('session.interrupt').length, 4, 'confirmed application need not repeat the control write')
    h.gc.sendToGroupChat('Room', h.roster, '@all resume NEW_INPUT', 't-new')
    await h.advance()
    assert.equal(h.rpc('prompt.submit').length, 4, 'all four unknown workers still occupy capacity')
    options.unavailable = false
    for (const session of h.sessions.values()) { session.state = 'interrupted'; session.pending = null }
    for (const member of h.roster) await h.gc.harvestStrandedGroupReply('Room', member)
    await h.advance()
    assert.ok(h.rpc('prompt.submit').length > 4, 'exact terminal releases capacity for the new instruction')
    assert.equal(posts(h).length, 0, 'Stop reconciliation never publishes the old result')
    for (const ref of accepted) assert.ok(h.rpc('session.turn.poll').some(c =>
      c.params.accepted_turn?.request_id === ref.request_id && c.params.accepted_turn?.host_boot_id === ref.host_boot_id))
    await settle(h)
    assert.equal(coordinator(h).active, 0)
    assert.ok(h.leases.every(l => l.releases === 1))
  })
}

for (const cold of [false, true]) {
  bounded(`GC-ACK: ${cold ? 'cold' : 'hot'} lost ACK retries the captured generation and still awaits terminal`, async () => {
    const options = { interruptError: true, interruptReply: { status: 'interrupted' } }
    const hot = await harness(members(1), cold ? {} : options), pending = drive(hot)
    await flush()
    let h = hot
    if (cold) {
      h = await harness(hot.roster, options)
      for (const [id, session] of hot.sessions) h.sessions.set(id, clone(session))
      const rooms = clone(hot.gc.durableGroupChatRooms())
      rooms.Room.running = false; rooms.Room.turns = []; rooms.Room.turn = null
      h.gc.$groupChats.set(rooms)
    }
    const ref = clone(receipt(h).delivery.accepted_turn)
    const first = await h.gc.stopGroupThread('Room', 't1', h.roster)
    await h.advance()
    assert.equal(first.status, 'unconfirmed')
    assert.deepEqual(receipt(h)?.delivery.accepted_turn, ref)
    options.interruptError = false
    const second = await h.gc.stopGroupThread('Room', 't1', h.roster)
    assert.equal(second.status, 'stopping')
    assert.deepEqual(h.rpc('session.interrupt').map(c => c.params.session_id), [ref.session_id, ref.session_id])
    assert.deepEqual(receipt(h)?.delivery.accepted_turn, ref)
    sessionFor(h).state = 'interrupted'; sessionFor(h).pending = null
    await h.gc.harvestStrandedGroupReply('Room', h.roster[0]); await h.advance()
    assert.equal(receipt(h), undefined)
    assert.equal(posts(h).length, 0)
    if (!cold) assert.equal(h.activeLeases(), 0)
    await settle(hot, pending)
  })
}

function renderer() {
  const states = []; let cursor = 0
  return { react: {
    useState: initial => {
      const index = cursor++
      if (!(index in states)) states[index] = typeof initial === 'function' ? initial() : initial
      return [states[index], value => { states[index] = typeof value === 'function' ? value(states[index]) : value }]
    }, useRef: current => ({ current }), useEffect: () => {}, useMemo: factory => factory()
  }, sdk: { useValue: atom => atom.get(), cn: (...values) => values.filter(Boolean).join(' '),
    profileColor: () => '#000000', relativeTime: () => '' },
  render: (component, props, fresh = false) => {
    cursor = 0; if (fresh) states.length = 0
    return component(props)
  } }
}
function nodes(tree) {
  const result = []
  const walk = node => {
    if (Array.isArray(node)) node.forEach(walk)
    else if (node && typeof node === 'object') { result.push(node); walk(node.props?.children) }
  }
  walk(tree); return result
}
function preparedAnswer(h, entry, approvalChoice = 'once') {
  const props = { entry, members: h.roster }
  let tree = h.ui.render(h.gc.GroupClarifyCard, props, true)
  if (entry.kind === 'approval') nodes(tree).find(n => n.props?.children === approvalChoice && n.props?.onClick).props.onClick()
  else nodes(tree).find(n => n.props?.['aria-label'] === `Answer @${entry.member}`).props.onChange({ target: { value: 'ANSWER' } })
  tree = h.ui.render(h.gc.GroupClarifyCard, props)
  const button = nodes(tree).find(n => ['Answer', 'Respond'].includes(n.props?.children) && n.props?.onClick)
  assert.equal(button.props.disabled, false)
  return () => button.props.onClick()
}
async function park(kind = 'approval', options = {}) {
  const ui = renderer(), notices = [], originalProjection = options.resumeProjection
  Object.assign(options, ui, { onNotify: notice => notices.push(notice),
    resumeProjection: (session, method, projection) => {
      if (session.approval && session.state === 'waiting') projection.pending_approval = session.approval
      return originalProjection?.(session, method, projection) || projection
    } })
  const h = await harness(members(1), options), pending = drive(h)
  h.ui = ui; h.notices = notices; await flush()
  const session = sessionFor(h)
  session.state = 'waiting'
  if (kind === 'approval') session.approval = { request_id: 'approval-1', command: 'echo offline', choices: ['once', 'deny'] }
  else session.pending = { request_id: 'clarify-1', question: 'Choose?' }
  await h.advance(); await pending
  assert.equal(cards(h).length, 1)
  return { h, session, options }
}

for (const approvalResult of [{ resolved: 0 }, {}, { resolved: '1' }, { resolved: -1 }]) {
  bounded(`GC-APPROVAL: ${JSON.stringify(approvalResult)} produces no successful echo`, async () => {
    const { h, session } = await park('approval', { approvalResult })
    const accepted = clone(session.ref), log = clone(h.room().log), entry = cards(h)[0]
    await preparedAnswer(h, entry)(); await flush()
    assert.deepEqual(h.room().log, log)
    assert.equal(cards(h)[0], entry, 'a negative or unknown acknowledgement keeps the card')
    assert.equal(h.notices.filter(n => n.kind === 'error').length, 1)
    assert.deepEqual(receipt(h)?.delivery.accepted_turn, accepted)
    assert.equal(h.rpc('prompt.submit').length, 1)
    await settle(h)
  })
}
for (const choice of ['once', 'deny']) {
  bounded(`GC-APPROVAL: resolved=1 confirms ${choice} and delivers only the exact accepted turn`, async () => {
    const { h, session } = await park('approval', { approvalResult: { resolved: 1 } })
    const accepted = clone(session.ref)
    session.text = `AFTER_${choice}`
    await preparedAnswer(h, cards(h)[0], choice)(); await h.advance()
    assert.equal(cards(h).length, 0)
    assert.equal(h.room().log.filter(e => e.from.kind === 'user' && e.answer_to).length, 1)
    assert.equal(posts(h).filter(e => e.text === `AFTER_${choice}`).length, 1)
    assert.deepEqual(session.ref, accepted)
    assert.equal(h.rpc('prompt.submit').length, 1)
    await settle(h)
  })
}
bounded('GC-APPROVAL: lost response ACK retains card, receipt and honest error', async () => {
  const { h } = await park('approval', { answerError: () => true })
  const entry = cards(h)[0], log = clone(h.room().log)
  await preparedAnswer(h, entry)(); await flush()
  assert.deepEqual(h.room().log, log)
  assert.equal(cards(h)[0], entry)
  assert.equal(h.notices.filter(n => n.kind === 'error').length, 1)
  assert.ok(receipt(h))
  await settle(h)
})

for (const legacy of [false, true]) {
  bounded(`GC-RENAME: ${legacy ? 'legacy token' : 'room ID'} retains running owner through repeated local renames`, async t => {
    const h = await harness(members(1))
    if (!h.gc.renameGroupChat) return t.skip('Exact baseline lacks the rename test port; final source must run this gate')
    if (legacy) delete h.room().roomId
    const pending = drive(h); await flush()
    const owner = coordinator(h), session = sessionFor(h), accepted = clone(session.ref), title = session.title
    await h.gc.renameGroupChat('Room', 'Renamed', h.roster)
    await h.gc.renameGroupChat('Renamed', 'RenamedAgain', h.roster)
    assert.equal(coordinator(h, 'RenamedAgain'), owner)
    assert.equal(coordinator(h), undefined)
    assert.equal([...owner.occurrences][0].group, 'RenamedAgain')
    assert.deepEqual(receipt(h, h.roster[0], 'RenamedAgain')?.delivery.accepted_turn, accepted)
    session.state = 'complete'; session.text = 'FINAL_AFTER_RENAME'
    await h.advance(); await pending
    assert.equal(posts(h, 'RenamedAgain').filter(e => e.text === 'FINAL_AFTER_RENAME').length, 1)
    assert.equal(h.gc.$groupChats.get().Room, undefined)
    assert.equal(h.gc.$groupChats.get().Renamed, undefined)
    assert.equal(h.gc.$groupChats.get().RenamedAgain.running, false)
    assert.equal(session.title, title)
    assert.equal(h.rpc('session.create').length, 1)
    assert.equal(h.rpc('prompt.submit').length, 1)
    await settle(h, null, 'RenamedAgain')
  })
}
bounded('GC-RENAME: pending card response and echo follow the same lifetime across rename', async t => {
  const answerAckGate = deferred(), { h, session } = await park('clarify', { answerAckGate })
  if (!h.gc.renameGroupChat) return t.skip('Exact baseline lacks the rename test port; final source must run this gate')
  const entry = cards(h)[0], owner = coordinator(h), accepted = clone(session.ref)
  session.text = 'FINAL_AFTER_RENAMED_ANSWER'
  const answering = preparedAnswer(h, entry)(); await flush()
  await h.gc.renameGroupChat('Room', 'AnsweredRoom', h.roster)
  assert.equal(cards(h)[0], entry)
  assert.equal(entry.group, 'AnsweredRoom')
  answerAckGate.resolve(); await answering; await h.advance()
  const room = h.gc.$groupChats.get().AnsweredRoom
  assert.equal(h.gc.$groupChats.get().Room, undefined)
  assert.equal(room.log.filter(e => e.answer_to).length, 1)
  assert.equal(posts(h, 'AnsweredRoom').filter(e => e.text === 'FINAL_AFTER_RENAMED_ANSWER').length, 1)
  assert.equal(coordinator(h, 'AnsweredRoom'), owner)
  assert.deepEqual(session.ref, accepted)
  assert.equal(h.rpc('prompt.submit').length, 1)
  await settle(h, null, 'AnsweredRoom')
})
bounded('GC-RENAME: in-flight Stop follows rename and retains custody until exact terminal', async t => {
  const interruptGate = deferred(), h = await harness(members(1), { interruptGate, interruptReply: { status: 'interrupted' } })
  if (!h.gc.renameGroupChat) return t.skip('Exact baseline lacks the rename test port; final source must run this gate')
  const pending = drive(h); await flush()
  const accepted = clone(sessionFor(h).ref)
  const stopping = h.gc.stopGroupThread('Room', 't1', h.roster); await flush()
  await h.gc.renameGroupChat('Room', 'StoppedRoom', h.roster)
  interruptGate.resolve(); const result = await stopping; await h.advance(); await pending
  assert.equal(result.status, 'stopping')
  assert.deepEqual(receipt(h, h.roster[0], 'StoppedRoom')?.delivery.accepted_turn, accepted)
  assert.equal(h.activeLeases(), 1)
  sessionFor(h).state = 'interrupted'
  await h.gc.harvestStrandedGroupReply('StoppedRoom', h.roster[0]); await flush()
  assert.equal(receipt(h, h.roster[0], 'StoppedRoom'), undefined)
  assert.equal(h.activeLeases(), 0)
  assert.equal(h.gc.$groupChats.get().Room, undefined)
  assert.equal(posts(h, 'StoppedRoom').length, 0)
})
bounded('GC-RENAME: remote receive rehomes running owner before room listeners see the new name', async t => {
  const options = { rpcResponse: (_route, method) => method === 'profiles.list'
    ? { profiles: [{ name: 'default', ui_meta: { 'hermes-bots-groups': options.remote } }] }
    : method === 'profiles.configure' ? { applied: { ui_meta: true } } : undefined }
  const h = await harness(members(1), options)
  if (!h.gc.pullGroupChatServerState || !h.gc.renameGroupChat) return t.skip('Exact baseline lacks the rename test port; final source must run this gate')
  const pending = drive(h); await flush()
  const owner = coordinator(h), session = sessionFor(h), accepted = clone(session.ref)
  const snapshot = h.gc.groupChatSyncSnapshot(), [roomKey, sourceRoom] = Object.entries(snapshot.rooms)[0]
  const projected = clone(sourceRoom)
  assert.ok(projected)
  options.remote = { ...snapshot, rooms: { [roomKey]: { ...projected, name: 'RemoteRoom', revision: projected.revision + 10 } } }
  // This receive starts after local writes have settled. An intentionally
  // pending local edit has separate preserveRooms authority over the name.
  h.gc.stopGroupChatServerSync()
  await h.gc.pullGroupChatServerState('local')
  assert.equal(coordinator(h, 'RemoteRoom'), owner)
  assert.equal(h.gc.$groupChats.get().Room, undefined)
  assert.deepEqual(receipt(h, h.roster[0], 'RemoteRoom')?.delivery.accepted_turn, accepted)
  session.state = 'complete'; session.text = 'REMOTE_RENAMED_FINAL'
  await h.advance(); await pending
  assert.equal(posts(h, 'RemoteRoom').filter(e => e.text === 'REMOTE_RENAMED_FINAL').length, 1)
  assert.equal(h.gc.$groupChats.get().RemoteRoom.running, false)
  assert.equal(h.rpc('prompt.submit').length, 1)
  await settle(h, null, 'RemoteRoom')
})
bounded('GC-RENAME: same display name with a new room lifetime cannot adopt the old final', async () => {
  const h = await harness(members(1)), pending = drive(h)
  await flush()
  const session = sessionFor(h)
  h.gc.$groupChats.set({ Room: { ...clone(h.room()), roomId: 'replacement-room', epoch: 1, running: false,
    sessions: {}, sessionOwners: {}, stranded: {}, log: [], turns: [], turn: null } })
  session.state = 'complete'; session.text = 'OLD_LIFETIME_FINAL'
  await h.advance(); await pending
  assert.equal(posts(h).length, 0)
  assert.equal(h.room().roomId, 'replacement-room')
  assert.equal(h.rpc('prompt.submit').length, 1)
})
bounded('GC-RENAME: late Stop ACK cannot poll or update a replacement room at the same name', async () => {
  const interruptGate = deferred(), h = await harness(members(1), { interruptGate, interruptReply: { status: 'interrupted' } })
  const pending = drive(h); await flush()
  const stopping = h.gc.stopGroupThread('Room', 't1', h.roster); await flush()
  const before = h.rpc('session.turn.poll').length
  const replacement = { ...clone(h.room()), roomId: 'new-lifetime', running: true, epoch: 20,
    stranded: {}, sessions: {}, sessionOwners: {}, holds: {}, log: [], turns: [], turn: null }
  h.gc.$groupChats.set({ Room: replacement })
  interruptGate.resolve(); const result = await stopping; await h.advance(); await pending
  assert.equal(result.status, 'unconfirmed')
  assert.equal(h.rpc('session.turn.poll').length, before)
  assert.equal(h.room().roomId, 'new-lifetime')
  assert.equal(h.room().epoch, 20)
  assert.equal(h.room().running, true)
  assert.equal(h.room().log.length, 0)
})
bounded('GC-HOLD: waiting hold retains the exact question custody while suppressing stale Answer', async () => {
  const { h, session } = await park('clarify')
  const accepted = clone(session.ref), staleAnswer = preparedAnswer(h, cards(h)[0])
  assert.equal(coordinator(h).active, 0)
  h.gc.sendToGroupChat('Room', h.roster, '@bot1 pause', 'hold-thread')
  await h.advance(); await staleAnswer(); await flush()
  assert.equal(h.rpc('clarify.respond').length, 0)
  assert.deepEqual(receipt(h)?.delivery.accepted_turn, accepted)
  assert.equal(h.activeLeases(), 1)
  assert.equal(coordinator(h).active, 0, 'verified waiting keeps no execution worker')
  assert.equal(h.rpc('session.interrupt').length, 0)
  session.state = 'complete'; session.pending = null; session.text = 'HELD_QUESTION_FINAL'
  await h.gc.harvestStrandedGroupReply('Room', h.roster[0]); await h.advance()
  assert.equal(receipt(h), undefined)
  assert.equal(h.activeLeases(), 0)
  assert.equal(posts(h).length, 0)
})
bounded('GC-ACK: unavailable timeout and repeated Stop retain four occupied workers', async () => {
  const options = { unavailable: true, interruptReply: { status: 'interrupted' } }
  const h = await harness(members(6), options), pending = drive(h)
  await flush(); await h.advance(20 * 60000)
  assert.equal(h.rpc('prompt.submit').length, 4)
  assert.equal(coordinator(h).active, 4)
  assert.equal(h.activeLeases(), 4)
  const first = await h.gc.stopGroupThread('Room', 't1', h.roster); await h.advance(); await pending
  assert.equal(first.status, 'stopping')
  const second = await h.gc.stopGroupThread('Room', 't1', h.roster)
  assert.equal(second.status, 'stopping')
  assert.equal(Object.keys(h.room().stranded).length, 4)
  assert.equal(coordinator(h).active, 4)
  assert.equal(h.rpc('prompt.submit').length, 4)
  options.unavailable = false
  for (const session of h.sessions.values()) session.state = 'interrupted'
  for (const member of h.roster) await h.gc.harvestStrandedGroupReply('Room', member)
  assert.equal(h.activeLeases(), 0)
  assert.equal(coordinator(h).active, 0)
  assert.equal(posts(h).length, 0)
})

for (const cold of [false, true]) {
  for (const failed of [false, true]) {
    bounded(`GC-ACK-02: ${cold ? 'cold' : 'parked hot'} terminal retains custody through ${failed ? 'rejected' : 'applied'} pending interrupt`, async () => {
      const interruptGate = deferred(), options = { unavailable: true, interruptGate, interruptError: failed }
      const hot = await harness(members(1), cold ? { unavailable: true } : options), initial = drive(hot)
      await flush(); await hot.advance(); await initial
      const oldOwner = [...coordinator(hot).occurrences][0]
      assert.equal(oldOwner.collectorDone, true, 'the collector has parked on unavailable outcome')
      let h = hot
      if (cold) {
        h = await harness(hot.roster, options)
        h.gc.$groupChats.set(clone(hot.gc.durableGroupChatRooms()))
        for (const [id, session] of hot.sessions) h.sessions.set(id, clone(session))
      }
      const marker = receipt(h), accepted = clone(marker.delivery.accepted_turn)
      const stopping = h.gc.stopGroupThread('Room', 't1', h.roster)
      const repeated = h.gc.stopGroupThread('Room', 't1', h.roster)
      await flush()
      options.unavailable = false
      const session = sessionFor(h)
      session.state = 'complete'; session.pending = null; session.text = 'OLD_STOPPED_FINAL'
      h.gc.sendToGroupChat('Room', h.roster, '@all resume REPLACEMENT_INPUT', 'replacement-thread')
      await h.advance(250)
      await h.gc.harvestStrandedGroupReply('Room', h.roster[0]); await flush()
      assert.equal(receipt(h), marker, 'exact terminal cannot erase a pending session control target')
      assert.equal(h.rpc('prompt.submit').length, cold ? 0 : 1, 'no successor may reuse the runtime before control settles')
      assert.equal(h.rpc('session.interrupt').length, 1, 'concurrent Stop shares the exact pending request')
      assert.deepEqual(session.ref, accepted)
      if (!cold) {
        assert.equal(oldOwner.released, undefined)
        assert.equal(coordinator(h).members.get(oldOwner.memberLock), oldOwner)
        assert.equal(h.gc.groupRuntimeSessionOwners.get(oldOwner.sessionLock), oldOwner)
        assert.equal(h.leases[0].releases, 0)
        assert.equal(coordinator(h).active, 1)
        assert.equal(oldOwner.reservation, true, 'pending retirement keeps its publication reservation')
      }
      interruptGate.resolve()
      await stopping; await repeated; await flush()
      await h.gc.harvestStrandedGroupReply('Room', h.roster[0]); await h.advance()
      // A cold blocked instruction can require a fresh explicit drive. That is
      // intentional; it cannot replay the uncertain predecessor automatically.
      if (h.rpc('prompt.submit').length === (cold ? 0 : 1)) {
        void h.gc.runGroupChatRounds('Room', h.roster, 'replacement-thread')
        await flush(); await h.advance()
      }
      assert.equal(h.rpc('prompt.submit').length, cold ? 1 : 2)
      assert.ok(h.rpc('prompt.submit').at(-1).params.text.includes('REPLACEMENT_INPUT'))
      assert.equal(h.rpc('session.interrupt').length, 1, 'no delayed cleanup targets the admitted replacement')
      assert.equal(posts(h).filter(e => e.text === 'OLD_STOPPED_FINAL').length, 0)
      if (!cold) assert.equal(h.leases[0].releases, 1)
      await settle(h)
      await settle(hot)
    })
  }
}

async function delayedRenameHarness(extra = {}) {
  const metadataGate = deferred(), ui = renderer(), options = { ...ui, ...extra,
    rpcResponse: async (route, method, params) => {
      const supplied = await extra.rpcResponse?.(route, method, params)
      if (supplied !== undefined) return supplied
      if (method === 'profiles.configure') {
        if (params.name === 'bot1' && params.ui_meta?.['hermes-bots']?.groups?.includes('Renamed')) {
          await metadataGate.promise
        }
        return { applied: { ui_meta: true } }
      }
      return undefined
    } }
  const h = await harness(members(2), options)
  h.ui = ui
  // Seed memberships through the actual creation handler, then use the same
  // room/metadata owners as the real rename and settings actions.
  h.gc.$groupChats.set({})
  assert.equal(h.gc.createFreshGroupChat('Room', h.roster), 'Room')
  await flush()
  h.gc.updateGroupChat('Room', room => { room.log = [clone(h.input)]; return room }, { sync: false })
  h.gc.$groupChatWorkspace.set('Room')
  return { h, metadataGate, options }
}

bounded('GC-RENAME-02: overlapping local renames cannot resurrect a name or stale later-member membership', async () => {
  const { h, metadataGate } = await delayedRenameHarness()
  const roomId = h.room().roomId
  const first = h.gc.renameGroupChat('Room', 'Renamed', h.roster); await flush()
  const second = await h.gc.renameGroupChat('Renamed', 'RenamedAgain', h.roster)
  metadataGate.resolve()
  assert.equal(await first, 'RenamedAgain', 'the late return names the same current lifetime')
  assert.equal(second, 'RenamedAgain')
  assert.equal(h.gc.$groupChats.get().Room, undefined)
  assert.equal(h.gc.$groupChats.get().Renamed, undefined)
  assert.equal(h.gc.$groupChats.get().RenamedAgain.roomId, roomId)
  assert.equal(h.gc.$groupChatWorkspace.get(), 'RenamedAgain')
  for (const member of h.roster) {
    const last = h.rpc('profiles.configure').filter(c => c.params.name === member.name).at(-1)
    assert.deepEqual(last.params.ui_meta['hermes-bots'].groups, ['RenamedAgain'])
  }
  assert.equal(h.rpc('session.create').length, 0)
  assert.equal(h.rpc('prompt.submit').length, 0)
})

function prepareSettings(h, renamed = []) {
  let closed = 0
  const props = { group: 'Room', members: h.roster, open: true, onClose: () => { closed++ }, onRenamed: name => renamed.push(name) }
  let tree = h.ui.render(h.gc.GroupChatSettingsDialog, props, true)
  nodes(tree).find(n => n.props?.['aria-label'] === 'Group name').props.onChange({ target: { value: 'Renamed' } })
  nodes(tree).find(n => n.props?.onImage).props.onImage('OFFLINE_ROOM_IMAGE')
  tree = h.ui.render(h.gc.GroupChatSettingsDialog, props)
  const click = nodes(tree).find(n => n.props?.children === 'Save' && n.props?.onClick).props.onClick
  return { click, renamed, closed: () => closed }
}
function submitSettings(h, renamed = []) {
  const settings = prepareSettings(h, renamed)
  settings.click()
  return settings
}

bounded('GC-RENAME-02: the actual settings image and callback follow an overlapping rename', async () => {
  const { h, metadataGate } = await delayedRenameHarness(), settings = submitSettings(h)
  await flush()
  await h.gc.renameGroupChat('Renamed', 'RenamedAgain', h.roster)
  metadataGate.resolve(); await flush()
  assert.equal(h.gc.$groupChats.get().Renamed, undefined)
  assert.equal(h.gc.$groupChats.get().RenamedAgain.image, 'OFFLINE_ROOM_IMAGE')
  assert.deepEqual(settings.renamed, ['RenamedAgain'])
  assert.equal(settings.closed(), 1)
})

bounded('GC-RENAME-02: remote rename during delayed member persistence keeps the current lifetime only', async () => {
  let remote
  const { h, metadataGate } = await delayedRenameHarness({ rpcResponse: (_route, method) => method === 'profiles.list'
    ? { profiles: [{ name: 'default', ui_meta: { 'hermes-bots-groups': remote } }] } : undefined })
  const first = h.gc.renameGroupChat('Room', 'Renamed', h.roster); await flush()
  const snapshot = h.gc.groupChatSyncSnapshot(), [key, room] = Object.entries(snapshot.rooms)[0]
  remote = { ...snapshot, rooms: { [key]: { ...clone(room), name: 'RemoteRenamed', revision: room.revision + 10 } } }
  // Clear only the fake mirror's pending edit, matching a settled/received
  // remote revision. The real receive handler retains its existing CAS rule.
  h.gc.stopGroupChatServerSync()
  await h.gc.pullGroupChatServerState('local')
  metadataGate.resolve()
  assert.equal(await first, 'RemoteRenamed')
  assert.equal(h.gc.$groupChats.get().Room, undefined)
  assert.equal(h.gc.$groupChats.get().Renamed, undefined)
  assert.equal(h.gc.$groupChats.get().RemoteRenamed.roomId, room.roomId)
  assert.equal(h.rpc('prompt.submit').length, 0)
})

for (const replaced of [false, true]) {
  bounded(`GC-RENAME-02: delayed actual settings cannot update a ${replaced ? 'replacement' : 'deleted'} room`, async () => {
    const { h, metadataGate } = await delayedRenameHarness(), settings = submitSettings(h)
    await flush()
    const replacement = { ...clone(h.gc.$groupChats.get().Renamed), roomId: 'replacement-lifetime',
      log: [], sessions: {}, sessionOwners: {}, stranded: {}, image: 'REPLACEMENT_IMAGE' }
    h.gc.$groupChats.set(replaced ? { Renamed: replacement } : {})
    metadataGate.resolve(); await flush()
    assert.equal(h.gc.$groupChats.get().Room, undefined)
    assert.deepEqual(settings.renamed, [])
    assert.equal(settings.closed(), 0)
    if (replaced) {
      assert.equal(h.gc.$groupChats.get().Renamed, replacement)
      assert.equal(replacement.image, 'REPLACEMENT_IMAGE')
    } else assert.equal(h.gc.$groupChats.get().Renamed, undefined)
  })
}

for (const replaced of [false, true]) {
  bounded(`GC-RENAME-02: an already rendered Save cannot adopt a ${replaced ? 'replacement' : 'deleted'} lifetime`, async () => {
    const { h, metadataGate } = await delayedRenameHarness(), settings = prepareSettings(h)
    const replacement = { ...clone(h.room()), roomId: 'replacement-before-click', image: 'REPLACEMENT_IMAGE', log: [] }
    const before = h.rpc('profiles.configure').length
    h.gc.$groupChats.set(replaced ? { Room: replacement } : {})
    settings.click(); metadataGate.resolve(); await flush()
    assert.deepEqual(h.gc.$groupChats.get(), replaced ? { Room: replacement } : {})
    assert.equal(h.rpc('profiles.configure').length, before, 'a stale button cannot rename members of a vanished room')
    assert.deepEqual(settings.renamed, [])
    assert.equal(settings.closed(), 0)
  })
}

bounded('GC-RENAME-02: actual settings still create and rename a legitimate metadata-only group', async () => {
  const { h, metadataGate } = await delayedRenameHarness()
  h.gc.$groupChats.set({})
  const settings = submitSettings(h); await flush()
  metadataGate.resolve(); await flush()
  const room = h.gc.$groupChats.get().Renamed
  assert.ok(room)
  assert.ok(room.coordinationId, 'the existing legacy lifetime token fences the save')
  assert.equal(room.image, 'OFFLINE_ROOM_IMAGE')
  assert.equal(h.gc.$groupChats.get().Room, undefined)
  assert.deepEqual(settings.renamed, ['Renamed'])
  assert.equal(settings.closed(), 1)
  assert.equal(h.rpc('prompt.submit').length, 0)
})
