import type { GatewayRequest } from './utils'

export interface FrozenInput {
  sessionId: string
  eventId: string
  text: string
}

export function inputRecoveryEvidence(error: unknown) {
  const data = (error as { data?: Record<string, unknown> } | null)?.data

  return {
    accepted: typeof data?.durable_input_accepted === 'boolean' ? data.durable_input_accepted : 'unknown',
    started: typeof data?.execution_started === 'boolean' ? data.execution_started : 'unknown',
    eventId: typeof data?.input_event_id === 'string' ? data.input_event_id : undefined,
    changedOwner: Array.isArray(data?.changed_owner) ? data.changed_owner : []
  }
}

export function reconcileInput(request: GatewayRequest, input: FrozenInput) {
  return request<{ durable_input_accepted?: boolean; execution_started?: boolean; can_manual_retry?: boolean }>(
    'prompt.input_status',
    { session_id: input.sessionId, input_event_id: input.eventId, text: input.text }
  )
}
