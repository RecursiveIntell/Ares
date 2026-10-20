import assert from 'node:assert/strict'
import test from 'node:test'
import { harness, members, flush, drive } from './stop-custody-harness.mjs'

async function stoppedWithFailure() {
  const h = await harness(members(6), { interruptError: true }); const pending = drive(h); await flush()
  await h.gc.stopGroupThread('Room', 't1', h.roster); await h.advance(); await pending; await flush()
  return h
}

test('ACCEPTANCE: failed interrupt retains every exact cancellation receipt', async () => {
  const h = await stoppedWithFailure()
  assert.equal(Object.keys(h.room().stranded).length, 4, 'Unconfirmed running turns must retain exact source/runtime receipts')
})

test('ACCEPTANCE: repeated Stop retries a previously failed interruption', async () => {
  const h = await stoppedWithFailure()
  const prior = h.rpc('session.interrupt').length
  await h.gc.stopGroupThread('Room', 't1', h.roster); await h.advance()
  assert.ok(h.rpc('session.interrupt').length > prior, 'Failed interrupts must remain retryable')
})

test('ACCEPTANCE: failed interrupt cannot free four unresolved running workers', async () => {
  const h = await stoppedWithFailure()
  assert.equal(h.gc.groupRoomCoordinators.get('Room').active, 4, 'Only exact terminal or confirmed cancellation proof frees unknown running capacity')
})
