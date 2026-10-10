import test from 'node:test'
import assert from 'node:assert/strict'
import { harness, members, flush } from './stop-custody-harness.mjs'

const payload = 'PRIVATE_ATTACHMENT_PAYLOAD'
const transportSecret = 'PRIVATE_RPC_ERROR_WITH_TOKEN'
const methods = { image: 'image.attach_bytes', pdf: 'pdf.attach', file: 'file.attach' }
const attachment = (kind, name = `report.${kind}`) => ({ kind, name, data: payload })

// Import and exercise the shipped plugin through the existing dependency loader.
// Complete any incorrectly admitted baseline turn so RED cannot hang on polling.
async function turn(h, attachments) {
  let settled = false
  const result = h.gc.runGroupChatMemberTurn('Room', h.roster[0], 'Analyze evidence', 't1', attachments)
    .then(value => ({ value }), error => ({ error }))
    .then(value => { settled = true; return value })
  await flush()
  h.finish('bot1', 'Evidence reviewed')
  await h.until(() => settled)
  return result
}

async function assertBlocked(h, attachments, expected) {
  const result = await turn(h, attachments)
  assert.equal(h.rpc('prompt.submit').length, 0, 'required attachment failure must prevent prompt admission')
  assert.ok(result.error instanceof Error, 'attachment failure is surfaced, never text-only success')
  assert.equal(result.error.code, 'GROUP_ATTACHMENT_FAILED')
  assert.deepEqual(result.error.data, {
    outcomeState: 'attachment-failed',
    reason: result.error.message,
    member: 'bot1',
    attachmentKind: expected.kind,
    filename: expected.filename
  })
  assert.match(result.error.message, /bot1/)
  assert.ok(result.error.message.includes(expected.kind))
  assert.ok(result.error.message.includes(expected.filename))
  const surfaced = `${result.error.message} ${JSON.stringify(result.error.data)}`
  assert.ok(!surfaced.includes(payload), 'attachment bytes are never exposed')
  assert.ok(!surfaced.includes(transportSecret), 'transport error is never exposed')
  assert.equal(result.error.cause, undefined, 'raw transport error is not retained as a cause')
  assert.deepEqual(h.room().stranded, {}, 'no attempted submit means marker can be safely consumed')
  assert.equal(h.activeLeases(), 0, 'failed preparation releases the source lease')
  assert.equal(h.posts().length, 0, 'failed attachment cannot publish a member reply')
  await h.gc.harvestStrandedGroupReply('Room', h.roster[0])
  assert.equal(h.rpc('prompt.submit').length, 0, 'harvest cannot turn attachment failure into text-only replay')
}

for (const kind of ['image', 'pdf', 'file']) {
  test(`${kind} RPC rejection blocks admission with safe typed member failure`, async () => {
    const h = await harness(members(1), { rpcResponse: (_route, method) => {
      if (method === methods[kind]) throw new Error(`${transportSecret}: ${payload}`)
    } })
    await assertBlocked(h, [attachment(kind, `/private/customer\\folder/report\n.${kind}`)],
      { kind, filename: `report_.${kind}` })
    assert.equal(h.rpc(methods[kind]).length, 1)
  })
}

test('partial staging failure stops remaining attachments and consumes the unsubmitted marker', async () => {
  const h = await harness(members(1), { rpcResponse: (_route, method) => {
    if (method === 'pdf.attach') throw new Error(transportSecret)
  } })
  await assertBlocked(h, [attachment('image'), attachment('pdf'), attachment('file')],
    { kind: 'pdf', filename: 'report.pdf' })
  assert.deepEqual(h.calls.filter(c => c.method.includes('attach')).map(c => c.method),
    ['image.attach_bytes', 'pdf.attach'])
})

test('successful image PDF and file staging preserves payloads routing order and file references', async () => {
  const h = await harness(members(1))
  const attachments = [attachment('image'), attachment('pdf'), attachment('file')]
  const result = await turn(h, attachments)
  assert.equal(result.error, undefined)
  assert.equal(result.value, 'Evidence reviewed')
  const staged = h.calls.filter(c => c.method.includes('attach'))
  assert.deepEqual(staged.map(c => c.method), ['image.attach_bytes', 'pdf.attach', 'file.attach'])
  const submit = h.rpc('prompt.submit')
  assert.equal(submit.length, 1)
  for (const call of staged) {
    assert.ok(h.calls.indexOf(call) < h.calls.indexOf(submit[0]), 'all staging precedes admission')
    assert.equal(call.params.session_id, submit[0].params.session_id)
    assert.equal(call.route.targetProfile, 'bot1')
    assert.equal(call.params.content_base64 ?? call.params.data_url, payload)
  }
  assert.equal(staged[0].params.filename, 'report.image')
  assert.equal(staged[1].params.filename, 'report.pdf')
  assert.equal(staged[2].params.name, 'report.file')
  assert.equal(submit[0].params.text,
    'Analyze evidence\n\nAttached files staged in your session workspace:\nreport.file → staged-file')
  assert.deepEqual(h.room().stranded, {})
  assert.equal(h.activeLeases(), 0)
})
