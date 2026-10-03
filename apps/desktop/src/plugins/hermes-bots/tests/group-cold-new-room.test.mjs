import assert from 'node:assert/strict'
import test from 'node:test'
import { importGroupTurnPlugin } from './group-turn-test-loader.mjs'

const clone = value => JSON.parse(JSON.stringify(value))
const flush = async () => { for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve)) }
const bot = (connectionId, name = 'alpha') => ({ name, title: '', connectionId,
  connectionLabel: connectionId, sourceScoped: true, remoteSource: connectionId !== 'local',
  route: { connectionId, mode: connectionId === 'local' ? 'local' : 'remote', profile: name, targetProfile: name } })
const MEMBERS = [bot('local'), bot('remote-A')]
const OWNER = { name: 'alpha', connectionId: 'local', route: MEMBERS[0].route }
const originalRoom = () => ({ roomId: 'original-room', epoch: 0, running: false,
  members: clone(MEMBERS), watermarks: { 'old-thread::local::alpha': 1 },
  sessions: { 'local::alpha': 'OLD_STORED_ID', 'remote-A::alpha': 'OLD_REMOTE_STORED_ID' },
  sessionOwners: { 'local::alpha': OWNER }, holds: { 'local::alpha': { at: 12, byMessageId: 'stop-old' } },
  stranded: {
    'local::alpha': { thread: 'old-thread', anchor_id: 'old-entry', reported: true, epoch: 3,
      delivery: { member_key: 'local::alpha', owner: OWNER,
        accepted_turn: { request_id: 'old-accepted', session_id: 'OLD_RUNTIME_ID', route: 'compute_host', host_boot_id: 'old-boot' } } },
    'remote-A::alpha': { before: 326, thread: 'old-thread', reported: true }
  },
  log: [
    { id: 'old-entry', thread: 'old-thread', from: { kind: 'user', name: 'You' }, at: 1,
      text: 'OLD_UNCERTAIN_INSTRUCTION', images: [{ kind: 'file', data: 'OLD_ATTACHMENT', name: 'old.txt' }] },
    { id: 'selected-entry', thread: 'new-thread', from: { kind: 'user', name: 'You' }, at: 2,
      text: '@all SELECTED_NEW_TEXT', images: [{ kind: 'file', data: 'SELECTED_ATTACHMENT_OMITTED', name: 'new.txt' }] }
  ] })

async function harness({ hydrate = false, members = MEMBERS } = {}) {
  let now = 100000, counter = 0, stateCursor = 0
  const calls = [], sessions = new Map(), storage = new Map(), states = [], disposers = []
  const atom = initial => { let value = initial; const listeners = new Set(); return {
    get: () => value, set: next => { value = next; for (const fn of listeners) fn(next) },
    listen: fn => { listeners.add(fn); return () => listeners.delete(fn) }
  } }
  const request = async (route, method, params = {}) => {
    calls.push({ route: route && { ...route }, method, params: clone(params) })
    if (method === 'session.create') {
      const id = ++counter, session = { runtime: `NEW_RUNTIME_${id}`, stored: `NEW_STORED_${id}`, title: params.title, route }
      sessions.set(session.runtime, session)
      return { session_id: session.runtime, stored_session_id: session.stored }
    }
    if (method === 'session.resume') {
      assert.ok(!String(params.session_id).startsWith('OLD_'), 'no old stored/runtime session may be prepared')
      assert.notEqual(params.session_id, 'Group: original-room')
      const session = [...sessions.values()].find(s => s.route?.connectionId === route?.connectionId &&
        [s.runtime, s.stored, s.title].includes(params.session_id))
      if (!session) throw Object.assign(new Error('not found'), { code: 4007 })
      // Match the current backend's live-unpersisted resume shape.
      return { session_id: session.runtime, stored_session_id: session.stored, messages: [],
        info: { title: session.title }, running: false }
    }
    if (method === 'prompt.submit') {
      const session = sessions.get(params.session_id); assert.ok(session)
      session.accepted = { request_id: `new-accepted-${calls.length}`, session_id: session.runtime, route: 'inline', host_boot_id: null }
      return { accepted_turn: session.accepted }
    }
    if (method === 'session.turn.poll') {
      const session = sessions.get(params.session_id)
      if (!session) throw Object.assign(new Error('accepted turn runtime is unavailable'), { code: 4001 })
      if (!params.accepted_turn) throw Object.assign(new Error('session_id and full accepted_turn identity required'), { code: 4006 })
      assert.deepEqual(params.accepted_turn, session.accepted)
      return { session_id: session.runtime, turn_outcomes: { version: 1, scope: 'process_local', availability: 'available',
        turns: [{ accepted_turn: session.accepted, state: 'complete', finalized: [{ status: 'complete', text: '(pass)' }] }] } }
    }
    if (method === 'profiles.configure') return { applied: { ui_meta: true } }
    return {}
  }
  const imported = await importGroupTurnPlugin({ atom,
    Date: class extends Date { static now() { return now } },
    setTimeout: (fn, delay) => { if (delay === 2000) { now += delay; fn() } return 1 }, clearTimeout: () => {},
    setInterval: () => 1, clearInterval: () => {},
    document: { getElementById: () => true, addEventListener: () => {}, removeEventListener: () => {} },
    useEffect: () => {}, useRef: current => ({ current }), useState: initial => { const index = stateCursor++; if (!(index in states)) states[index] = typeof initial === 'function' ? initial() : initial;
      return [states[index], value => { states[index] = typeof value === 'function' ? value(states[index]) : value }] },
    sdk: { useValue: value => value.get(), cn: (...values) => values.filter(Boolean).join(' '), profileColor: () => '#000000', relativeTime: () => '' },
    host: { request: (method, params) => request(null, method, params), requestProfile: request,
      retainProfile: async () => () => {},
      state: { profile: atom('default'), gateway: atom(null), connectionId: atom('local') },
      notify: () => {}, notifyError: () => {} }
  })
  const gc = { ...imported, ...imported.groupRecoveryTestAPI }
  storage.set('group-chats', { Original: originalRoom() })
  const storagePort = { get: key => clone(storage.get(key) ?? null), set: (key, value) => storage.set(key, clone(value)) }
  if (hydrate) {
    gc.default.register({ storage: storagePort, register: () => {}, onDispose: fn => disposers.push(fn) })
    await flush()
  } else {
    gc.bindGroupTurnTestStorage(storagePort)
    gc.$groupChats.set({ Original: originalRoom() })
  }
  gc.stopGroupChatServerSync()
  const source = (withText = true) => {
    const room = gc.$groupChats.get().Original
    const entry = room.log.find(item => item.id === 'selected-entry')
    return { group: 'Original', roomId: room.roomId, room, members,
      entry: withText ? { id: entry.id, text: entry.text, thread: entry.thread } : null }
  }
  return { gc, calls, storage, sessions, source, members,
    create: (withText = true) => gc.createFreshGroupChat('Original', members, { recoverySource: source(withText) }),
    execution: () => calls.filter(call => /^(prompt\.|session\.(create|resume|interrupt)|(?:file|image|pdf)\.attach)/.test(call.method)),
    renderWorkspace: () => { states.length = 0; stateCursor = 0; return gc.GroupChatWorkspace({ group: 'Original', members, onBack: () => {} }) },
    rerenderWorkspace: () => { stateCursor = 0; return gc.GroupChatWorkspace({ group: 'Original', members, onBack: () => {} }) },
    renderDialog: props => { stateCursor = 0; return gc.CreateGroupChatDialog(props) },
    setDialogStates: values => { states.splice(0, states.length, ...values) },
    dispose: () => disposers.forEach(fn => fn()) }
}

function nodes(tree) {
  const result = []
  const walk = node => { if (Array.isArray(node)) node.forEach(walk); else if (node && typeof node === 'object') { result.push(node); walk(node.props?.children) } }
  walk(tree); return result
}

test('actual cold hydrate shows both accepted and legacy unknown markers despite reported flags and empty activity', async () => {
  const h = await harness({ hydrate: true }), room = h.gc.$groupChats.get().Original
  assert.equal(room.running, false)
  assert.deepEqual(h.gc.groupBlockedMembers(room, MEMBERS).map(m => m.connectionId), ['local', 'remote-A'])
  assert.equal(h.gc.currentGroupActivity('Original').length, 0)
  const tree = h.gc.GroupBlockedNotice({ room, members: MEMBERS, onCreate: () => {} })
  assert.equal(nodes(tree).filter(n => typeof n.props?.children === 'string' && n.props.children.includes('earlier outcome unknown')).length, 2)
  assert.equal(h.execution().length, 0)
  h.dispose()
})

for (const withText of [true, false]) {
  test(`new group is empty and original state intact; selected text draft=${withText}`, async () => {
    const h = await harness(), before = clone(h.gc.$groupChats.get().Original)
    const oldDraftKey = h.gc.groupComposerDraftKey('Original', before)
    h.gc.updateGroupComposerDraft(oldDraftKey, draft => ({ ...draft, main: 'PRIVATE_UNSENT_OLD_DRAFT', pendingAttachments: { main: [{ data: 'OLD_PRIVATE_DRAFT_ATTACHMENT' }] } }))
    const oldDraft = clone(h.gc.groupComposerDraftSnapshot(oldDraftKey)), name = h.create(withText)
    await flush()
    const room = h.gc.$groupChats.get()[name]
    assert.notEqual(name, 'Original'); assert.notEqual(room.roomId, before.roomId)
    for (const field of ['log', 'sessions', 'watermarks', 'stranded', 'sessionOwners', 'holds']) assert.equal(Object.keys(room[field]).length, 0, field)
    assert.deepEqual(h.gc.$groupChats.get().Original, before)
    assert.deepEqual(h.gc.groupComposerDraftSnapshot(oldDraftKey), oldDraft)
    assert.deepEqual(room.members.map(m => [m.name, m.route.connectionId, m.route.targetProfile]), [['alpha', 'local', 'alpha'], ['alpha', 'remote-A', 'alpha']])
    const draft = h.gc.groupComposerDraftSnapshot(h.gc.groupComposerDraftKey(name, room))
    assert.equal(draft.main, withText ? '@all SELECTED_NEW_TEXT' : '')
    assert.deepEqual(draft.pendingAttachments, {})
    assert.equal(h.execution().length, 0)
    assert.ok(!JSON.stringify(room).includes('OLD_'))
    assert.ok(!JSON.stringify(room).includes('SELECTED_ATTACHMENT'))
  })
}

test('real dialog open/cancel and Create handler never submit; duplicate Create preserves edited draft', async () => {
  const h = await harness(); let closed = 0, created = null
  const props = { open: true, roster: MEMBERS, recoverySource: h.source(), onClose: () => { closed++ }, onCreated: name => { created = name } }
  h.setDialogStates(['', { 'local::alpha': true, 'remote-A::alpha': true }, 'Original', null])
  const tree = h.renderDialog(props)
  assert.match(JSON.stringify(tree), /Attachments are omitted/)
  tree.props.onOpenChange(false)
  assert.equal(closed, 1); assert.equal(created, null); assert.equal(h.execution().length, 0)
  const create = nodes(tree).find(n => typeof n.props?.children === 'string' && n.props.children.startsWith('Create Group'))
  assert.ok(create); assert.equal(create.props.disabled, false)
  create.props.onClick(); await flush()
  assert.ok(created)
  const key = h.gc.groupComposerDraftKey(created, h.gc.$groupChats.get()[created])
  h.gc.updateGroupComposerDraft(key, draft => ({ ...draft, main: 'EDITED_DRAFT' }))
  create.props.onClick(); await flush()
  assert.equal(Object.keys(h.gc.$groupChats.get()).length, 2)
  assert.equal(h.gc.groupComposerDraftSnapshot(key).main, 'EDITED_DRAFT')
  assert.equal(h.execution().length, 0)
})

test('explicit Send and next ordinary message use only new-room sessions and selected input', async () => {
  const h = await harness(), before = clone(h.gc.$groupChats.get().Original), name = h.create()
  const created = h.gc.$groupChats.get()[name], key = h.gc.groupComposerDraftKey(name, created)
  h.gc.sendToGroupChat(name, created.members, h.gc.groupComposerDraftSnapshot(key).main, 'new-only-thread')
  await flush()
  assert.equal(h.calls.filter(call => call.method === 'prompt.submit').length, 2)
  assert.ok(h.calls.filter(call => call.method === 'session.create').every(call => call.params.title === `Group: ${created.roomId}`))
  h.gc.sendToGroupChat(name, created.members, '@all NEXT_ORDINARY_MESSAGE', 'new-only-thread')
  await flush()
  const submits = h.calls.filter(call => call.method === 'prompt.submit')
  assert.equal(submits.length, 4)
  assert.ok(submits.slice(2).every(call => call.params.text.includes('NEXT_ORDINARY_MESSAGE') && !call.params.text.includes('SELECTED_NEW_TEXT')))
  assert.ok(submits.every(call => !call.params.text.includes('OLD_UNCERTAIN_INSTRUCTION')))
  assert.equal(h.calls.filter(call => /attach/.test(call.method)).length, 0)
  assert.deepEqual(h.gc.$groupChats.get().Original, before)
})

test('reopened handoff after send does not restage input; persisted origin preserves that rule after reload', async () => {
  const h = await harness(), name = h.create(), created = h.gc.$groupChats.get()[name]
  const key = h.gc.groupComposerDraftKey(name, created)
  h.gc.updateGroupComposerDraft(key, draft => ({ ...draft, main: '' }))
  assert.equal(h.create(), name)
  assert.equal(h.gc.groupComposerDraftSnapshot(key).main, '')
  const cold = await harness()
  cold.gc.$groupChats.set(clone(h.gc.durableGroupChatRooms()))
  assert.equal(cold.create(), name)
  assert.equal(cold.gc.groupComposerDraftSnapshot(key).main, '')
  assert.equal(cold.execution().length, 0)
})

test('source-qualified removed member stays unavailable, never replaced by active same-name profile', async () => {
  const removed = { name: 'alpha', connectionId: 'removed-source', connectionLabel: 'Removed', remoteSource: true, sourceMissing: true, sourceReachable: false }
  const h = await harness({ members: [MEMBERS[0], removed] }), name = h.create(false)
  const copy = h.gc.$groupChats.get()[name].members.find(m => m.connectionId === 'removed-source')
  assert.equal(copy.sourceMissing, true)
  assert.equal(copy.sourceReachable, false)
  assert.equal(copy.route.connectionId, 'removed-source')
  assert.equal(h.execution().length, 0)
})

test('changed selected text or replaced original room rejects stale creation before mutation', async () => {
  const h = await harness(), source = h.source(), beforeCount = Object.keys(h.gc.$groupChats.get()).length
  h.gc.$groupChats.get().Original.log.at(-1).text = 'REPLACEMENT_TEXT'
  assert.throws(() => h.gc.createFreshGroupChat('Original', MEMBERS, { recoverySource: source }), /selected message changed/)
  const newSource = h.source()
  h.gc.$groupChats.set({ Original: { ...h.gc.$groupChats.get().Original, roomId: 'replacement-room' } })
  assert.throws(() => h.gc.createFreshGroupChat('Original', MEMBERS, { recoverySource: newSource }), /original group changed/)
  assert.equal(Object.keys(h.gc.$groupChats.get()).length, beforeCount)
  assert.equal(h.execution().length, 0)
})

test('both persistence paths preserve original holds, exact session owners and legacy/accepted receipts', async () => {
  const h = await harness(), before = clone(h.gc.$groupChats.get().Original)
  h.create()
  for (const rooms of [h.gc.durableGroupChatRooms(), h.storage.get('group-chats')]) {
    for (const field of ['holds', 'sessionOwners', 'stranded', 'sessions', 'watermarks']) assert.deepEqual(rooms.Original[field], before[field])
  }
})

test('real workspace blocked notice opens recovery dialog without dispatch or changing old custody', async () => {
  const h = await harness(), before = clone(h.gc.$groupChats.get().Original)
  const tree = h.renderWorkspace()
  const notice = nodes(tree).find(n => n.type === h.gc.GroupBlockedNotice)
  assert.ok(notice, 'workspace renders its recovery affordance')
  const renderedNotice = notice.type(notice.props)
  const action = nodes(renderedNotice).find(n => n.props?.children === 'Create a new group')
  assert.ok(action)
  action.props.onClick()
  const updated = h.rerenderWorkspace()
  const dialog = nodes(updated).find(n => n.type === h.gc.CreateGroupChatDialog && n.props.open)
  assert.ok(dialog, 'explicit action opens the canonical dialog')
  assert.equal(dialog.props.recoverySource.entry, null, 'no backlog message is selected for replay')
  assert.deepEqual(h.gc.$groupChats.get().Original, before)
  assert.equal(h.execution().length, 0)
  dialog.props.onClose()
  assert.ok(nodes(h.rerenderWorkspace()).some(n => n.type === h.gc.CreateGroupChatDialog && !n.props.open))
  assert.equal(h.execution().length, 0)
})
