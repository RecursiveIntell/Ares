/** Strict predicates for this same-root native restart fixture, not a lineage resolver. */
export type RecordedOwner = { pid: number; birth: string }
export type OwnerObservation = RecordedOwner & { status: string; observed_birth?: string }

type ReceiptState = {
  receipts: { db: string; receipt: { content: unknown; conversation_root: string } }[]
  messages: { db: string; session_id: string; role: string; content: string }[]
}

export function matchingReceiptMessages(state: ReceiptState, content: string) {
  const receipts = state.receipts.filter(row => row.receipt.content === content)
  if (receipts.length !== 1 || !receipts[0].receipt.conversation_root) return []
  const receipt = receipts[0]
  return state.messages.filter(row => row.db === receipt.db &&
    row.session_id === receipt.receipt.conversation_root && row.role === 'user' && row.content === content)
}

export function recordedOwnersSettled(expected: RecordedOwner[], observed: OwnerObservation[]) {
  if (expected.length === 0 || observed.length !== expected.length) return false
  if (new Set(expected.map(row => `${row.pid}:${row.birth}`)).size !== expected.length) return false
  return expected.every(owner => {
    const matches = observed.filter(row => row.pid === owner.pid && row.birth === owner.birth)
    if (matches.length !== 1) return false
    const row = matches[0]
    return row.status === 'gone' || (row.status === 'replaced' &&
      typeof row.observed_birth === 'string' && row.observed_birth !== owner.birth)
  })
}
