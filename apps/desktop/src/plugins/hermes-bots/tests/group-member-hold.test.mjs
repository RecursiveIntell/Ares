import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

// #93129: a bot told to stop must STAY stopped. The hold helpers are pure and
// vm-sliced out of plugin.js exactly like group-turn-races.test.mjs does for
// the #93127 helpers. NOTE: vm-realm arrays/objects fail strict deepEqual
// against host literals (different realm prototypes) — normalize with spread
// or JSON round-trip before comparing.

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function loadHelpers() {
  const start = pluginSource.indexOf('// --- member-hold helpers (#93129)')
  const end = pluginSource.indexOf('// --- end member-hold helpers ---', start)
  assert.notEqual(start, -1, 'plugin carries the member-hold helper block')
  assert.notEqual(end, -1, 'member-hold helper block has a stable end marker')
  const context = {}
  vm.runInNewContext(
    `${pluginSource.slice(start, end)}
globalThis.classifyGroupHoldDirective = classifyGroupHoldDirective
globalThis.applyGroupHoldDirective = applyGroupHoldDirective
globalThis.heldMemberWatermarkAdvance = heldMemberWatermarkAdvance`,
    context
  )
  return context
}


// Use production mention parsing for local explicit-handle fixtures.
// No alias is configured; the five production source functions are unchanged.
function parseLocalMentions(text) {
  const names = ['botHandle', 'mentionNameForms', 'botFriendlyNames', 'groupMemberKey', 'parseGroupChatMentions']
  const functions = names.map(name => {
    const start = pluginSource.indexOf(`function ${name}(`)
    const end = pluginSource.indexOf('\n}', start)
    assert.notEqual(start, -1, `plugin carries ${name}`)
    assert.notEqual(end, -1, `${name} has a complete function boundary`)
    return pluginSource.slice(start, end + 2)
  })
  const context = { aliasIdentityFor: () => null }
  vm.runInNewContext(`${functions.join('\n')}\nglobalThis.parseGroupChatMentions = parseGroupChatMentions`, context)
  return context.parseGroupChatMentions(text, [
    { name: 'impl', handle: 'impl', title: 'Implementation specialist' },
    { name: 'docs', handle: 'docs' }
  ])
}

// ── stop detection ───────────────────────────────────────────────────────────

test('whole affirmative stop commands hold the mentioned member', () => {
  const { classifyGroupHoldDirective } = loadHelpers()
  for (const text of ['stop @impl', '@impl stop', '@impl please halt', 'pause @impl for now',
    'please stop @impl', '@impl, please pause now!', 'HALT @impl immediately.',
    '  @impl stop.  ']) {
    const action = classifyGroupHoldDirective(text, ['impl'], false)
    assert.deepEqual([...action.hold], ['impl'], `"${text}" should hold`)
    assert.deepEqual([...action.release], [])
    assert.equal(action.releaseAll, false)
  }
})


test('polite terminal member stop commands hold through production unquoted mention parsing', () => {
  const { classifyGroupHoldDirective, applyGroupHoldDirective } = loadHelpers()
  const stamp = { at: 2000, byMessageId: 'polite-stop', thread: 'polite-thread' }
  for (const text of ['@impl stop please', 'stop @impl, please', '@impl stop,please',
    '@impl pause now, please!', 'please halt @impl please.']) {
    const mentions = parseLocalMentions(text)
    assert.deepEqual([...mentions.mentioned], ['impl'], text)
    assert.equal(mentions.everyone, false)
    const action = classifyGroupHoldDirective(text, mentions.mentioned, mentions.everyone)
    assert.deepEqual([...action.hold], ['impl'], text)
    assert.deepEqual([...action.release], [], 'polite stop must not fall through to release')
    const held = { impl: { at: 1, byMessageId: 'prior-stop', thread: 'prior-thread' }, docs: { at: 2 } }
    const next = applyGroupHoldDirective(held, mentions, text, stamp, ['impl', 'docs'])
    assert.deepEqual(JSON.parse(JSON.stringify(next.impl)), stamp, text)
    assert.equal(next.docs, held.docs, 'another member is unchanged')
    assert.equal(held.impl.byMessageId, 'prior-stop', 'prior holds are not mutated')
  }
})

test('polite terminal all-member stop and resume use real mention parsing and owned stamps', () => {
  const { classifyGroupHoldDirective, applyGroupHoldDirective } = loadHelpers()
  const stamp = { at: 3000, byMessageId: 'polite-all', thread: 'all-thread' }
  for (const text of ['@all stop please', 'stop @all, please', '@everyone pause,please!']) {
    const mentions = parseLocalMentions(text)
    assert.equal(mentions.everyone, true, text)
    assert.deepEqual([...mentions.mentioned], [])
    const action = classifyGroupHoldDirective(text, mentions.mentioned, mentions.everyone)
    assert.equal(action.holdAll, true, text)
    assert.equal(action.releaseAll, false)
    const next = applyGroupHoldDirective({ impl: { at: 1 }, docs: { at: 2 } }, mentions, text, stamp, ['impl', 'docs'])
    assert.deepEqual(JSON.parse(JSON.stringify(next)), { impl: stamp, docs: stamp }, text)
  }
  for (const text of ['@all resume please', 'resume @all, please', '@everyone proceed now, please!']) {
    const mentions = parseLocalMentions(text)
    const action = classifyGroupHoldDirective(text, mentions.mentioned, mentions.everyone)
    assert.equal(action.holdAll, false)
    assert.equal(action.releaseAll, true, text)
    const next = applyGroupHoldDirective({ impl: { at: 1 }, docs: { at: 2 } }, mentions, text, stamp, ['impl', 'docs'])
    assert.deepEqual(JSON.parse(JSON.stringify(next)), {}, text)
  }
  for (const text of ['@all stop please when done', '@all resume please if approved', '@all stopplease']) {
    const mentions = parseLocalMentions(text)
    const action = classifyGroupHoldDirective(text, mentions.mentioned, mentions.everyone)
    assert.equal(action.holdAll, false, text)
    assert.equal(action.releaseAll, false, text)
  }
})

test('quoted display-name targets are unsupported by the unchanged production mention grammar', () => {
  const { classifyGroupHoldDirective, applyGroupHoldDirective } = loadHelpers()
  for (const text of ['stop @"Implementation specialist"', '@"Implementation specialist" stop']) {
    const mentions = parseLocalMentions(text)
    assert.deepEqual([...mentions.mentioned], [])
    assert.equal(mentions.everyone, false)
    const action = classifyGroupHoldDirective(text, mentions.mentioned, mentions.everyone)
    assert.deepEqual([...action.hold], [])
    assert.deepEqual([...action.release], [])
    const held = { impl: { at: 1 } }
    assert.equal(applyGroupHoldDirective(held, mentions, text, {}), held)
  }
})

test('stop word without any mention holds nobody', () => {
  const { classifyGroupHoldDirective } = loadHelpers()
  const action = classifyGroupHoldDirective('stop', [], false)
  assert.deepEqual([...action.hold], [])
})

test('negated instructions never create an immediate member stop', () => {
  const { classifyGroupHoldDirective } = loadHelpers()
  for (const text of ["don't stop @impl", '@impl do not stop', '@impl never stop',
    '@impl do not halt until done', '@impl please do not pause', "@impl don't stop until complete"]) {
    const action = classifyGroupHoldDirective(text, ['impl'], false)
    assert.deepEqual([...action.hold], [], `"${text}" is ordinary instruction text`)
    assert.equal(action.holdAll, false)
    assert.deepEqual([...action.release], ['impl'], 'ordinary direct address keeps its existing release behavior')
  }
})

test('quoted and descriptive stop language is ordinary text', () => {
  const { classifyGroupHoldDirective } = loadHelpers()
  for (const text of ['"stop @impl"', "'@impl stop'", '`@impl pause`', '“@impl halt”',
    '@impl explain the stop condition', '@impl the pause command is documented',
    'The log says stop @impl', '@impl report why the user said "stop"',
    '@impl stop signs must be detected', '@impl report stop status']) {
    const action = classifyGroupHoldDirective(text, ['impl'], false)
    assert.deepEqual([...action.hold], [], `"${text}" is not a control command`)
    assert.equal(action.holdAll, false)
  }
})

test('conditional and completion-boundary instructions do not stop now', () => {
  const { classifyGroupHoldDirective } = loadHelpers()
  for (const text of ['@impl stop when done', 'stop @impl after completion',
    'if the budget is exhausted, pause @impl', '@impl halt only if approval is required',
    '@impl continue working and stop after the final result',
    '@impl stop unless there is work left', '@impl stop?',
    '@impl stop please when done', 'stop @impl, please after completion', '@impl stopplease']) {
    const action = classifyGroupHoldDirective(text, ['impl'], false)
    assert.deepEqual([...action.hold], [], `"${text}" does not authorize an immediate hold`)
    assert.equal(action.holdAll, false)
  }
})

test('ordinary group instructions with stop or resume words neither hold nor release all', () => {
  const { classifyGroupHoldDirective } = loadHelpers()
  for (const text of ['@all do not stop until done', "@all don't stop", '@all never stop',
    '@all continue working; do not stop until complete', '@all stop when the task is complete',
    '"@all stop"', '@all explain how to resume after a pause', '@all do not resume yet']) {
    const action = classifyGroupHoldDirective(text, [], true)
    assert.deepEqual([...action.hold], [])
    assert.equal(action.holdAll, false, text)
    assert.equal(action.releaseAll, false, text)
  }
})

test('affirmative controls support several explicit targets', () => {
  const { classifyGroupHoldDirective } = loadHelpers()
  for (const text of ['stop @impl @docs', '@impl, @docs please halt',
    'pause @impl and @docs for now', '@impl & @docs: stop!']) {
    const action = classifyGroupHoldDirective(text, ['impl', 'docs'], false)
    assert.deepEqual([...action.hold], ['impl', 'docs'], text)
    assert.deepEqual([...action.release], [])
  }
})

test('whole affirmative resume commands preserve release controls', () => {
  const { classifyGroupHoldDirective } = loadHelpers()
  for (const text of ['@impl resume', 'resume @impl', '@impl please continue',
    'please proceed @impl now', '@impl go immediately!']) {
    const action = classifyGroupHoldDirective(text, ['impl'], false)
    assert.deepEqual([...action.hold], [])
    assert.deepEqual([...action.release], ['impl'], text)
  }
  for (const text of ['@all resume', 'continue @everyone', '@all please proceed now', 'go @all']) {
    const action = classifyGroupHoldDirective(text, [], true)
    assert.equal(action.holdAll, false)
    assert.equal(action.releaseAll, true, text)
  }
})

test('"stopped" as part of another word does not trigger a hold', () => {
  const { classifyGroupHoldDirective } = loadHelpers()
  // \b(stop|halt|pause)\b — "stopped" is a different token
  const action = classifyGroupHoldDirective('@impl unstoppable work ahead', ['impl'], false)
  assert.deepEqual([...action.hold], [])
  // a plain non-stop mention releases instead (direct address overrides hold)
  assert.deepEqual([...action.release], ['impl'])
})

// ── hold lifecycle ───────────────────────────────────────────────────────────

test('stop sets a hold; resume for the same member clears it', () => {
  const { applyGroupHoldDirective } = loadHelpers()
  const stamp = { at: 1000, byMessageId: 'm1', thread: 't1' }
  const held = applyGroupHoldDirective({}, { mentioned: ['impl'], everyone: false }, 'stop @impl', stamp)
  assert.ok(held.impl)
  assert.equal(held.impl.at, 1000)
  const released = applyGroupHoldDirective(held, { mentioned: ['impl'], everyone: false }, '@impl resume', stamp)
  assert.equal(released.impl, undefined)
})

test('a direct non-stop mention of a held member releases the hold', () => {
  const { applyGroupHoldDirective } = loadHelpers()
  const held = { impl: { at: 1, byMessageId: null, thread: null } }
  // Preserved behavior; not a claim to solve all natural-language resume intent.
  for (const text of ['@impl what is your status?', '@impl explain the stop condition', '@impl do not resume yet']) {
    const mentions = parseLocalMentions(text)
    const next = applyGroupHoldDirective(held, mentions, text, {})
    assert.equal(next.impl, undefined, text)
  }
})

test('@all resume releases every hold', () => {
  const { applyGroupHoldDirective } = loadHelpers()
  const held = { impl: { at: 1 }, docs: { at: 2 } }
  const next = applyGroupHoldDirective(held, { mentioned: [], everyone: true }, '@all resume', {})
  assert.deepEqual(JSON.parse(JSON.stringify(next)), {})
})

test('@all stop holds every member — symmetric with @all resume', () => {
  const { applyGroupHoldDirective } = loadHelpers()
  const next = applyGroupHoldDirective(
    {},
    { mentioned: [], everyone: true },
    '@all stop',
    { at: 5 },
    ['impl', 'docs']
  )
  assert.ok(next.impl)
  assert.ok(next.docs)
  assert.equal(next.impl.at, 5)
})

test('the causal negated group instruction creates no holds while explicit stop stamps every member', () => {
  const { applyGroupHoldDirective } = loadHelpers()
  const mentions = { mentioned: [], everyone: true }
  const stamp = { at: 1000, byMessageId: 'continue-until-done', thread: 'new-thread' }
  const ordinary = applyGroupHoldDirective({}, mentions, '@all do not stop until done', stamp, ['impl', 'docs'])
  assert.deepEqual(JSON.parse(JSON.stringify(ordinary)), {})
  const explicit = applyGroupHoldDirective({}, mentions, '@all stop', stamp, ['impl', 'docs'])
  assert.deepEqual(JSON.parse(JSON.stringify(explicit)), { impl: stamp, docs: stamp })
})

test('an unrelated room message leaves holds untouched (same object back)', () => {
  const { applyGroupHoldDirective } = loadHelpers()
  const held = { impl: { at: 1 } }
  const next = applyGroupHoldDirective(held, { mentioned: [], everyone: false }, 'receipt round complete', {})
  assert.equal(next, held)
})

test('holding one member does not disturb another\'s hold', () => {
  const { applyGroupHoldDirective } = loadHelpers()
  const held = { impl: { at: 1 } }
  const next = applyGroupHoldDirective(held, { mentioned: ['docs'], everyone: false }, 'stop @docs', { at: 2 })
  assert.ok(next.impl)
  assert.ok(next.docs)
})

// ── skip must not spin ───────────────────────────────────────────────────────

test('held skip consumes the delta exactly once', () => {
  const { heldMemberWatermarkAdvance } = loadHelpers()
  // fresh delta → advance to log length
  assert.equal(heldMemberWatermarkAdvance(3, 7), 7)
  // already consumed → no write, no spin
  assert.equal(heldMemberWatermarkAdvance(7, 7), null)
  assert.equal(heldMemberWatermarkAdvance(9, 7), null)
  // unset watermark treated as 0
  assert.equal(heldMemberWatermarkAdvance(undefined, 2), 2)
})
