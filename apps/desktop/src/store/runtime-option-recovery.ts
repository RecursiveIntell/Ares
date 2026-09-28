import { translateNow } from '@/i18n'
import { REASONING_EFFORT_VALUES } from '@/lib/reasoning-effort'

import { dismissNotification, notify } from './notifications'
import { $sessionStates, sessionTileDelegate } from './session-states'

export type RuntimeOptionDimension = 'effort' | 'fast'

type RequestGateway = <T>(method: string, params?: Record<string, unknown>) => Promise<T>

const noticeId = (sessionId: string, dimension: RuntimeOptionDimension) =>
  `option-unconfirmed:${sessionId}:${dimension}`

function markUnconfirmed(sessionId: string, dimension: RuntimeOptionDimension, unconfirmed: boolean): void {
  if (!$sessionStates.get()[sessionId]) {
    return
  }

  sessionTileDelegate()?.updateSession(sessionId, state => {
    const previous = state.unconfirmedRuntimeOptions ?? []

    if (previous.includes(dimension) === unconfirmed) {
      return state
    }

    const next = unconfirmed ? [...previous, dimension] : previous.filter(value => value !== dimension)

    return { ...state, unconfirmedRuntimeOptions: next.length ? next : undefined }
  })
}

/** Clear only this dimension after an acknowledged new write. */
export function clearRuntimeOptionUncertainty(sessionId: string, dimension: RuntimeOptionDimension): void {
  markUnconfirmed(sessionId, dimension, false)
  dismissNotification(noticeId(sessionId, dimension))
}

/** Recognized transport/host uncertainty is not proof of rejection. */
export function isUncertainRuntimeOptionError(error: unknown): boolean {
  if (error && typeof error === 'object' && 'code' in error && error.code === 5019) {
    return true
  }

  const message = error instanceof Error ? error.message : String(error)

  return /request timed out|gateway connection closed|\bunconfirmed\b/i.test(message)
}

/** Reconcile a failed option acknowledgement using one read, never a write retry.
 * Returns false only for errors outside this bounded uncertainty path. A fresh
 * observation says what the host reports now, not which write produced it. */
export async function reconcileRuntimeOptionFailure(
  error: unknown,
  context: {
    sessionId: string
    dimension: RuntimeOptionDimension
    request: RequestGateway
    owns: () => boolean
    applyObserved: (value: string | boolean) => void
  }
): Promise<boolean> {
  if (!isUncertainRuntimeOptionError(error)) {
    return false
  }

  if (!context.owns() || !$sessionStates.get()[context.sessionId]) {
    return true
  }

  const { dimension, sessionId } = context
  markUnconfirmed(sessionId, dimension, true)
  let observed = false

  try {
    const response = await context.request<Record<string, unknown>>('config.get', {
      key: dimension === 'effort' ? 'reasoning' : 'fast',
      session_id: sessionId
    })

    if (!context.owns() || !$sessionStates.get()[sessionId]) {
      return true
    }

    if (response?.owner === 'compute_host' && response.session_id === sessionId) {
      const value = response.value

      const validEffort =
        typeof value === 'string' && (value === '' || REASONING_EFFORT_VALUES.some(item => item === value))

      if (dimension === 'effort' ? validEffort : value === 'fast' || value === 'normal') {
        context.applyObserved(dimension === 'effort' ? (value as string) : value === 'fast')
        markUnconfirmed(sessionId, dimension, false)
        observed = true
      }
    }
  } catch {
    // Readback failure leaves the desired selection explicitly unconfirmed.
  }

  if (context.owns() && $sessionStates.get()[sessionId]) {
    notify({
      id: noticeId(sessionId, dimension),
      kind: 'warning',
      meta: sessionId,
      durationMs: 0,
      title: translateNow('shell.modelOptions.unconfirmed'),
      message: translateNow(
        observed ? 'shell.modelOptions.unconfirmedObserved' : 'shell.modelOptions.unconfirmedUnavailable'
      )
    })
  }

  return true
}
