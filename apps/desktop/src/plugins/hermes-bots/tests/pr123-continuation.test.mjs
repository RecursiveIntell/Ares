import assert from 'node:assert/strict'
import test from 'node:test'
import { harness, members, deferred, flush, drive } from './stop-custody-harness.mjs'

const boundedTest = (name, body) => test(name, { timeout: 12000 }, body)
const clone = value => JSON.parse(JSON.stringify(value))
const receipts = h => Object.values(h.room().stranded || {})
const submits = (h, name) => h.rpc('prompt.submit').filter(call => call.route?.profile === name)
const nodes = tree => {
  const out = []
  const walk = value => {
    if (Array.isArray(value)) value.forEach(walk)
    else if (value && typeof value === 'object') { out.push(value); walk(value.props?.children) }
  }
  walk(tree); return out
}

async function uiHarness(roster = members(2), options = {}) {
  const states = []; let cursor = 0
  Object.assign(options, {
    react: { useState: initial => {
      const index = cursor++
      if (!(index in states)) states[index] = typeof initial === 'function' ? initial() : initial
      return [states[index], value => { states[index] = typeof value === 'function' ? value(states[index]) : value }]
    }, useRef: current => ({ current }), useEffect: () => {}, useMemo: factory => factory() },
    sdk: { useValue: atom => atom.get(), cn: (...values) => values.filter(Boolean).join(' '), profileColor: () => '#000000', relativeTime: () => '' },
    document: { getElementById: () => true, addEventListener: () => {}, removeEventListener: () => {} }
  })
  const h = await harness(roster, options)
  h.answer = () => {
    const entry = Object.values(h.gc.$groupClarify.get()).find(card => card.member === 'bot1')
    assert.ok(entry, 'production waiting projection exposes the actual card')
    states.length = 0; cursor = 0
    const props = { entry, members: h.roster }
    let tree = h.gc.GroupClarifyCard(props)
    nodes(tree).find(node => node.props?.['aria-label'] === 'Answer @bot1').props.onChange({ target: { value: 'ANSWER' } })
    cursor = 0; tree = h.gc.GroupClarifyCard(props)
    const button = nodes(tree).find(node => node.props?.children === 'Answer' && node.props?.onClick)
    assert.ok(button); assert.equal(button.props.disabled, false)
    button.props.onClick()
  }
  return h
}

function terminalProjection(session, projection, text) {
  session.state = 'complete'; session.text = text
  projection.running = false
  delete projection.pending_clarify
  projection.turn_outcomes.turns[0] = { accepted_turn: { ...session.ref }, state: 'complete', finalized: [{ status: 'complete', text }] }
  return projection
}

async function thirdRoundWaiting({ chain = false } = {}) {
  let h
  h = await uiHarness(members(2), { resumeProjection: (session, method, projection) => {
    if (method !== 'session.turn.poll' || !session.ref) return projection
    if (session.profile === 'bot1' && session.submits === 3 && !session.answeredRequest) {
      session.state = 'waiting'; session.pending = { request_id: 'third-round-question', question: 'Continue?' }
      projection.pending_clarify = session.pending
      projection.running = false
      projection.turn_outcomes.turns[0] = { accepted_turn: { ...session.ref }, state: 'waiting', finalized: [] }
      return projection
    }
    if (session.profile === 'bot1' && session.submits === 3 && session.answeredRequest) return projection
    if (chain && session.submits === 4) return terminalProjection(session, projection,
      session.profile === 'bot2' ? 'CONTINUATION_ONE @bot1' : 'CONTINUATION_TWO @bot2')
    return terminalProjection(session, projection, session.submits < 3 ? `NORMAL_${session.profile}_${session.submits} @all` : '(pass)')
  } })
  const pending = drive(h)
  await h.until(() => submits(h, 'bot1').length === 3 && receipts(h).length === 1)
  await h.advance(); await pending
  assert.equal(submits(h, 'bot2').length, 3)
  assert.equal(h.room().running, false)
  return h
}

async function firstRoundWaiting(roster = members(2)) {
  const h = await uiHarness(roster)
  h.gc.updateGroupChat('Room', room => {
    room.log[0].text = '@bot1 ORIGINAL_INSTRUCTION'
    room.log[0].images = [{ kind: 'file', data: 'ORIGINAL_ATTACHMENT', name: 'original.txt' }]
    return room
  })
  const pending = drive(h); await flush()
  const session = [...h.sessions.values()][0]
  session.state = 'waiting'; session.pending = { request_id: 'cold-question', question: 'Continue?' }
  await h.advance(); await pending
  assert.equal(receipts(h).length, 1)
  return h
}

async function reload(hot, options = {}) {
  const h = await uiHarness(hot.roster, options), durable = clone(hot.gc.durableGroupChatRooms())
  // A fresh fake wire starts its sequence at zero. Namespace copied runtime
  // handles so newly-created continuation sessions cannot collide with them.
  const ids = new Map()
  for (const session of hot.sessions.values()) {
    const copied = clone(session), runtime = `cold:${session.runtime}`, stored = `cold:${session.stored}`
    ids.set(session.runtime, runtime); ids.set(session.stored, stored)
    copied.runtime = runtime; copied.stored = stored
    if (copied.ref) copied.ref.session_id = runtime
    h.sessions.set(runtime, copied)
  }
  for (const room of Object.values(durable)) {
    for (const key of Object.keys(room.sessions || {})) room.sessions[key] = ids.get(room.sessions[key]) || room.sessions[key]
    for (const marker of Object.values(room.stranded || {})) {
      if (marker.delivery?.accepted_turn) marker.delivery.accepted_turn.session_id = ids.get(marker.delivery.accepted_turn.session_id) || marker.delivery.accepted_turn.session_id
    }
  }
  h.gc.$groupChats.set({})
  h.gc.default.register({ storage: { get: key => key === 'group-chats' ? durable : null,
    set: (key, value) => h.storage.set(key, clone(value)) }, register: () => {}, onDispose: () => {} })
  await flush(); h.gc.stopGroupChatServerSync()
  assert.equal(h.room().running, false)
  assert.equal(h.room().turns?.length || 0, 0)
  return h
}

boundedTest('third normal round waiting answer schedules a bounded fourth teammate turn', async () => {
  const h = await thirdRoundWaiting(), session = [...h.sessions.values()].find(s => s.profile === 'bot1')
  session.text = 'HOT_LATE @bot2'
  h.answer(); await flush()
  await h.until(() => submits(h, 'bot2').length === 4)
  await h.advance()
  assert.equal(submits(h, 'bot1').length, 3)
  assert.equal(submits(h, 'bot2').length, 4)
  assert.match(submits(h, 'bot2')[3].params.text, /HOT_LATE/)
  assert.doesNotMatch(submits(h, 'bot2')[3].params.text, /FIRST_INPUT/)
  assert.equal(h.posts().filter(entry => entry.text === 'HOT_LATE @bot2').length, 1)
})

boundedTest('cold hydrated accepted terminal harvest continues its teammate once using peer delta', async () => {
  const hot = await firstRoundWaiting(), cold = await reload(hot)
  cold.finish('bot1', 'COLD_LATE @bot2')
  await cold.gc.harvestStrandedGroupReply('Room', cold.roster[0]); await flush()
  await cold.until(() => submits(cold, 'bot2').length === 1)
  const teammate = submits(cold, 'bot2')[0]
  assert.match(teammate.params.text, /COLD_LATE/)
  assert.doesNotMatch(teammate.params.text, /ORIGINAL_INSTRUCTION|ORIGINAL_ATTACHMENT/)
  assert.equal(cold.calls.filter(call => call.method.includes('attach') && call.route?.profile === 'bot2').length, 0)
  await cold.gc.harvestStrandedGroupReply('Room', cold.roster[0])
  await cold.gc.harvestStrandedGroupReply('Room', cold.roster[0]); await cold.advance()
  assert.equal(submits(cold, 'bot2').length, 1, 'duplicate harvest cannot schedule a second teammate')
  cold.finish('bot2'); await cold.advance()
  assert.equal(cold.posts().filter(entry => entry.text === 'COLD_LATE @bot2').length, 1)
  assert.equal(submits(cold, 'bot1').length, 0)
  const reloaded = await reload(cold)
  await reloaded.gc.harvestStrandedGroupReply('Room', reloaded.roster[0]); await reloaded.advance()
  assert.equal(reloaded.rpc('prompt.submit').length, 0, 'consumed cold publication cannot replay after another reload')
  assert.equal(reloaded.posts().filter(entry => entry.text === 'COLD_LATE @bot2').length, 1)
  await hot.gc.stopGroupThread('Room', 't1', hot.roster)
})

for (const change of ['other thread', 'newer same thread', 'Stop', 'replaced room', 'changed owner', 'changed recipient']) {
  boundedTest(`cold continuation control: ${change}`, async () => {
    const hot = await firstRoundWaiting(), cold = await reload(hot)
    if (change === 'other thread') {
      cold.gc.sendToGroupChat('Room', cold.roster, '@bot1 OTHER_THREAD_INPUT', 't2')
      await cold.advance(250)
    } else if (change === 'newer same thread') cold.gc.appendGroupChatEntry('Room', { kind: 'user', name: 'You' }, 'NEWER_INPUT', 't1')
    else if (change === 'Stop') await cold.gc.stopGroupThread('Room', 't1', cold.roster)
    else if (change === 'replaced room') cold.gc.updateGroupChat('Room', room => ({ ...room, roomId: 'replacement-room' }))
    else if (change === 'changed owner') cold.gc.updateGroupChat('Room', room => ({ ...room, sessionOwners: {
      ...room.sessionOwners, 'local::bot1': { ...room.sessionOwners['local::bot1'],
        route: { ...room.sessionOwners['local::bot1'].route, connectionId: 'replacement-source' } }
    } }))
    else cold.gc.updateGroupChat('Room', room => ({ ...room, members: room.members.map(member => member.name !== 'bot2'
      ? member : { ...member, connectionId: 'replacement-peer', route: { ...member.route, connectionId: 'replacement-peer' } }) }))
    cold.finish('bot1', 'CONTROL_LATE @bot2')
    await cold.gc.harvestStrandedGroupReply('Room', cold.roster[0]); await cold.advance()
    if (change === 'other thread') {
      await cold.until(() => submits(cold, 'bot2').length === 1)
      assert.match(submits(cold, 'bot2')[0].params.text, /CONTROL_LATE/)
      assert.doesNotMatch(submits(cold, 'bot2')[0].params.text, /OTHER_THREAD_INPUT|ORIGINAL_INSTRUCTION/)
      cold.finish('bot2'); await cold.advance()
    } else {
      assert.equal(submits(cold, 'bot2').length, 0)
      assert.equal(cold.posts().filter(entry => entry.text === 'CONTROL_LATE @bot2').length,
        change === 'changed recipient' ? 1 : 0,
        'a valid accepted source reply may publish while the changed recipient loses admission authority')
    }
    assert.equal(cold.rpc('prompt.submit').filter(call => call.route?.connectionId === 'replacement-source').length, 0)
    assert.equal(cold.rpc('prompt.submit').filter(call => call.route?.connectionId === 'replacement-peer').length, 0)
    await hot.gc.stopGroupThread('Room', 't1', hot.roster)
  })
}

boundedTest('hot resumed handoffs retain the two-continuation bound after normal rounds are exhausted', async () => {
  const h = await thirdRoundWaiting({ chain: true }), session = [...h.sessions.values()].find(s => s.profile === 'bot1')
  session.text = 'HOT_CHAIN_START @bot2'
  h.answer(); await flush()
  await h.until(() => submits(h, 'bot1').length === 4)
  await h.advance(5000); await h.advance(5000)
  assert.equal(submits(h, 'bot1').length, 4)
  assert.equal(submits(h, 'bot2').length, 4, 'third late handoff is beyond the continuation budget')
  assert.equal(h.posts().length, 7)
  assert.ok(h.posts().length <= 10)
  assert.ok(h.maxLeases() <= 4)
})

boundedTest('cold handoff chain preserves its two-continuation budget across repeated reloads', async () => {
  const hot = await firstRoundWaiting(members(4))
  let cold = await reload(hot)
  for (const [from, to] of [['bot1', 'bot2'], ['bot2', 'bot3']]) {
    cold.finish(from, `COLD_CHAIN_${from} @${to}`)
    await cold.gc.harvestStrandedGroupReply('Room', cold.roster.find(member => member.name === from))
    await cold.until(() => submits(cold, to).length === 1)
    const session = [...cold.sessions.values()].find(item => item.profile === to)
    session.state = 'waiting'; session.pending = { request_id: `chain-${to}`, question: 'Continue?' }
    await cold.advance(); await cold.until(() => !cold.room().running)
    assert.equal(receipts(cold).length, 1)
    cold = await reload(cold)
  }
  cold.finish('bot3', 'COLD_CHAIN_THIRD @bot4')
  await cold.gc.harvestStrandedGroupReply('Room', cold.roster[2]); await cold.advance(5000)
  assert.equal(cold.posts().length, 3)
  assert.equal(submits(cold, 'bot4').length, 0, 'a third handoff cannot reset the durable continuation budget')
  await hot.gc.stopGroupThread('Room', 't1', hot.roster)
})

boundedTest('cold resumed targeted work uses at most four execution workers across five teammates', async () => {
  const hot = await firstRoundWaiting(members(6)), cold = await reload(hot)
  cold.finish('bot1', 'COLD_BROADCAST @bot2 @bot3 @bot4 @bot5 @bot6')
  await cold.gc.harvestStrandedGroupReply('Room', cold.roster[0]); await flush()
  await cold.until(() => cold.rpc('prompt.submit').length >= 1)
  assert.ok(cold.gc.groupRoomCoordinators.get('Room').active <= 4)
  for (let attempt = 0; attempt < 20 && (cold.rpc('prompt.submit').length < 5 ||
      cold.gc.groupRoomCoordinators.get('Room').active); attempt++) {
    assert.ok(cold.gc.groupRoomCoordinators.get('Room').active <= 4)
    for (const name of ['bot2', 'bot3', 'bot4', 'bot5', 'bot6']) cold.finish(name)
    await cold.advance()
  }
  await cold.until(() => cold.rpc('prompt.submit').length === 5 &&
    cold.gc.groupRoomCoordinators.get('Room').active === 0)
  assert.equal(cold.rpc('prompt.submit').length, 5)
  assert.ok(cold.maxLeases() <= 4)
  assert.equal(cold.posts().length, 1)
  await hot.gc.stopGroupThread('Room', 't1', hot.roster)
})

async function capacityWaiting(unknown = false) {
  const h = await uiHarness(members(6), { resumeProjection: (session, method, projection) => {
    if (method !== 'session.turn.poll' || !session.ref) return projection
    if (session.submits === 1) return terminalProjection(session, projection, `INITIAL_${session.profile} @all`)
    if (session.profile === 'bot2') {
      session.state = 'waiting'; session.pending = { request_id: 'capacity-question', question: 'Continue?' }
      projection.pending_clarify = session.pending; projection.running = false
      projection.turn_outcomes.turns[0] = { accepted_turn: { ...session.ref }, state: 'waiting', finalized: [] }
      return projection
    }
    if (unknown) {
      session.state = 'running'; projection.running = true
      projection.turn_outcomes.availability = 'unavailable'; projection.turn_outcomes.turns = []
      return projection
    }
    return terminalProjection(session, projection, `SECOND_${session.profile} @all`)
  } })
  const pending = drive(h)
  await h.until(() => h.rpc('prompt.submit').length === 10)
  await h.until(() => !h.room().running); await pending
  assert.equal(receipts(h).length, unknown ? 4 : 1)
  assert.equal(h.posts().length, unknown ? 6 : 9)
  return h
}

boundedTest('cold terminal publication respects the cumulative ten-message cap', async () => {
  const hot = await capacityWaiting(), cold = await reload(hot)
  cold.finish('bot2', 'TENTH_PUBLICATION @bot6')
  await cold.gc.harvestStrandedGroupReply('Room', cold.roster[1]); await cold.advance(5000)
  assert.equal(cold.posts().length, 10)
  assert.equal(cold.posts().filter(entry => entry.text === 'TENTH_PUBLICATION @bot6').length, 1)
  assert.equal(cold.rpc('prompt.submit').length, 0, 'no eleventh publication is reserved through a cold handoff')
  const reloaded = await reload(cold)
  await reloaded.gc.harvestStrandedGroupReply('Room', reloaded.roster[1]); await reloaded.advance()
  assert.equal(reloaded.posts().length, 10)
  assert.equal(reloaded.rpc('prompt.submit').length, 0)
  await hot.gc.stopGroupThread('Room', 't1', hot.roster)
})

boundedTest('cold continuation retains publication capacity for three unknown accepted receipts', async () => {
  const hot = await capacityWaiting(true)
  const cold = await reload(hot, { resumeProjection: (session, method, projection) => {
    if (method === 'session.turn.poll' && session.profile !== 'bot2' && session.submits === 2) {
      projection.turn_outcomes.availability = 'unavailable'; projection.turn_outcomes.turns = []
    }
    return projection
  } })
  const unknown = receipts(cold).filter(marker => marker.delivery.member_key !== 'local::bot2')
    .map(marker => clone(marker.delivery.accepted_turn))
  cold.finish('bot2', 'UNKNOWN_CAPACITY_LATE @bot6')
  await cold.gc.harvestStrandedGroupReply('Room', cold.roster[1]); await cold.advance(5000)
  assert.equal(cold.posts().length, 7)
  assert.equal(cold.rpc('prompt.submit').length, 0, 'unknown accepted work reserves the remaining three slots')
  assert.deepEqual(receipts(cold).map(marker => marker.delivery.accepted_turn), unknown)
  assert.equal(cold.rpc('session.interrupt').length, 0)
  await hot.gc.stopGroupThread('Room', 't1', hot.roster)
})

boundedTest('three cold unknown accepted workers permit one fresh teammate, then terminal releases backfill the second', async () => {
  const hot = await uiHarness(members(6))
  hot.gc.updateGroupChat('Room', room => {
    room.log[0].text = '@bot1 @bot2 @bot3 @bot4 ORIGINAL_WORKER_INPUT'
    room.log[0].images = [{ kind: 'file', data: 'ORIGINAL_WORKER_ATTACHMENT', name: 'original.txt' }]
    return room
  })
  const hotDrive = drive(hot); await flush()
  assert.equal(hot.rpc('prompt.submit').length, 4)
  assert.equal(receipts(hot).length, 4)
  const unresolved = new Set(['bot2', 'bot3', 'bot4'])
  const cold = await reload(hot, { resumeProjection: (session, method, projection) => {
    if (method === 'session.turn.poll' && unresolved.has(session.profile)) {
      projection.turn_outcomes.availability = 'unavailable'; projection.turn_outcomes.turns = []
    }
    return projection
  } })
  cold.finish('bot1', 'FRESH_HANDOFF @bot5 @bot6')
  await cold.gc.harvestStrandedGroupReply('Room', cold.roster[0]); await flush()
  await cold.until(() => cold.rpc('prompt.submit').length >= 1)
  const coordinator = cold.gc.groupRoomCoordinators.get('Room')
  assert.equal(cold.posts().length, 1, 'publication capacity is far below ten')
  assert.equal(cold.rpc('prompt.submit').length, 1, 'three uncertain cold workers leave one fresh worker slot')
  assert.equal(coordinator.active, 1)
  assert.equal(receipts(cold).filter(marker => unresolved.has(marker.delivery.owner.name)).length, 3)
  await cold.advance(5000)
  assert.equal(cold.rpc('prompt.submit').length, 1, 'uncertain occupancy does not expire into free capacity')
  for (const name of ['bot2', 'bot3', 'bot4']) {
    unresolved.delete(name); cold.finish(name)
    await cold.gc.harvestStrandedGroupReply('Room', cold.roster.find(member => member.name === name)); await flush()
    assert.ok(coordinator.active + unresolved.size <= 4,
      'cold accepted custody and fresh active workers share the same four-worker ceiling')
  }
  await cold.until(() => cold.rpc('prompt.submit').length === 2)
  assert.equal(coordinator.active, 2, 'freed cold capacity backfills the pending fresh teammate')
  assert.deepEqual(cold.rpc('prompt.submit').map(call => call.route.profile).sort(), ['bot5', 'bot6'])
  for (const call of cold.rpc('prompt.submit')) {
    assert.match(call.params.text, /FRESH_HANDOFF/)
    assert.doesNotMatch(call.params.text, /ORIGINAL_WORKER_INPUT|ORIGINAL_WORKER_ATTACHMENT/)
  }
  assert.equal(cold.calls.filter(call => call.method.includes('attach')).length, 0)
  for (const name of ['bot5', 'bot6']) cold.finish(name)
  await cold.advance(); await cold.until(() => coordinator.active === 0)
  assert.equal(cold.rpc('prompt.submit').length, 2)
  assert.ok(cold.maxLeases() <= 4)
  await hot.gc.stopGroupThread('Room', 't1', hot.roster); await hot.advance(); await hotDrive
})

for (const change of ['delete', 'same-name replacement']) {
  boundedTest(`cold active continuation finalizer respects room ${change}`, async () => {
    const hot = await firstRoundWaiting(), cold = await reload(hot)
    cold.finish('bot1', 'ACTIVE_BEFORE_REMOVAL @bot2')
    await cold.gc.harvestStrandedGroupReply('Room', cold.roster[0]); await flush()
    await cold.until(() => submits(cold, 'bot2').length === 1)
    const coordinator = cold.gc.groupRoomCoordinators.get('Room'), running = [...coordinator.drives.values()]
    assert.equal(coordinator.active, 1)
    // Direct atom deletion/replacement is the room-removal state transition;
    // no runtime lifecycle or profile mutation is exercised by this fixture.
    const replacement = { roomId: 'same-name-new-room', coordinationId: 'same-name-new-token', epoch: 17,
      running: false, log: [{ id: 'new-room-user', at: 200000, thread: 'new-thread',
        from: { kind: 'user', name: 'You' }, text: 'REPLACEMENT_ROOM_INPUT' }],
      members: clone(cold.roster), sessions: {}, sessionOwners: {}, stranded: {}, holds: {}, watermarks: {} }
    const expected = change === 'delete' ? {} : { Room: clone(replacement) }
    cold.gc.$groupChats.set(clone(expected))
    cold.finish('bot2', 'OLD_CONTINUATION_FINAL @bot1')
    await cold.advance(5000); await Promise.all(running); await flush()
    assert.deepEqual(cold.gc.$groupChats.get(), expected, 'old finalization cannot recreate or mutate another room lifetime')
    await cold.advance(5000)
    assert.deepEqual(cold.gc.$groupChats.get(), expected)
    assert.equal(cold.rpc('prompt.submit').length, 1)
    await hot.gc.stopGroupThread('Room', 't1', hot.roster)
  })
}

for (const change of ['owner changed', 'recipient removed']) {
  boundedTest(`cold teammate retain race: ${change} prevents old owner submission`, async () => {
    const hot = await firstRoundWaiting(), retainGate = deferred(), cold = await reload(hot, { retainGate })
    cold.finish('bot1', 'RETAIN_RACE_HANDOFF @bot2')
    await cold.gc.harvestStrandedGroupReply('Room', cold.roster[0]); await flush()
    await cold.until(() => cold.gc.groupRoomCoordinators.get('Room')?.active === 1)
    const coordinator = cold.gc.groupRoomCoordinators.get('Room')
    assert.equal(submits(cold, 'bot2').length, 0, 'old owner has not passed its pending retain boundary')
    cold.gc.updateGroupChat('Room', room => ({ ...room, members: change === 'recipient removed'
      ? room.members.filter(member => member.name !== 'bot2')
      : room.members.map(member => member.name !== 'bot2' ? member : { ...member,
        connectionId: 'changed-during-retain', route: { ...member.route, connectionId: 'changed-during-retain' } }) }))
    retainGate.resolve(); await flush(); await cold.advance()
    await cold.until(() => coordinator.active === 0)
    assert.equal(submits(cold, 'bot2').length, 0)
    assert.equal(cold.rpc('prompt.submit').length, 0)
    assert.equal(cold.posts().filter(entry => entry.text === 'RETAIN_RACE_HANDOFF @bot2').length, 1,
      'the valid accepted source publication remains delivered')
    assert.ok(cold.leases.every(lease => lease.releases === 1))
    await hot.gc.stopGroupThread('Room', 't1', hot.roster)
  })
}

boundedTest('malformed durable captured request owner cannot route a handoff but preserves accepted source publication', async () => {
  const hot = await firstRoundWaiting(), cold = await reload(hot)
  const marker = receipts(cold)[0]
  cold.gc.updateGroupChat('Room', room => {
    const contexts = clone(room.driveContexts), captured = contexts[marker.drive_key].members.find(item => item.member.name === 'bot2')
    captured.requestMember.route.connectionId = 'malformed-request-owner'
    return { ...room, driveContexts: contexts }
  })
  cold.finish('bot1', 'MALFORMED_CONTEXT_FINAL @bot2')
  await cold.gc.harvestStrandedGroupReply('Room', cold.roster[0]); await cold.advance(5000)
  assert.equal(cold.posts().filter(entry => entry.text === 'MALFORMED_CONTEXT_FINAL @bot2').length, 1)
  assert.equal(cold.rpc('prompt.submit').length, 0)
  assert.equal(cold.calls.filter(call => call.route?.connectionId === 'malformed-request-owner').length, 0)
  assert.equal(receipts(cold).length, 0)
  await hot.gc.stopGroupThread('Room', 't1', hot.roster)
})
