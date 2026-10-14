import { describe, expect, it, vi } from 'vitest'

import { inputRecoveryEvidence, reconcileInput } from './input-recovery'

describe('manual receipt recovery', () => {
  it('keeps missing and blank evidence unknown', () => {
    expect(inputRecoveryEvidence({ data: {} }).accepted).toBe('unknown')
    expect(inputRecoveryEvidence({ data: { durable_input_accepted: false, execution_started: false } }).accepted).toBe(
      false
    )
  })
  it('surfaces receipt and changed owner without treating it as permission', () => {
    expect(
      inputRecoveryEvidence({
        data: {
          durable_input_accepted: true,
          execution_started: false,
          input_event_id: 'e',
          changed_owner: ['transport']
        }
      })
    ).toMatchObject({ accepted: true, started: false, eventId: 'e', changedOwner: ['transport'] })
  })
  it('reconciles exact frozen receipt without resubmitting', async () => {
    const request = vi.fn().mockResolvedValue({ durable_input_accepted: true, execution_started: false })
    await reconcileInput(request, { sessionId: 's', eventId: 'e', text: 'frozen' })
    expect(request).toHaveBeenCalledExactlyOnceWith('prompt.input_status', {
      session_id: 's',
      input_event_id: 'e',
      text: 'frozen'
    })
  })
})
