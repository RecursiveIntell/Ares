import { JsonRpcGatewayError } from '@hermes/shared'

/** Gateway JSON-RPC code for a runtime session that is no longer in memory. */
const GATEWAY_SESSION_NOT_FOUND_CODE = 4001

// A runtime id is terminal once the gateway says it is gone. Keep this state
// outside any mounted poller so a component remount cannot re-arm the dead id.
const goneSessions = new Set<string>()

/** Structured 4001 first; text fallback only when the numeric code was lost. */
export function isSessionGoneError(error: unknown): boolean {
  if (error instanceof JsonRpcGatewayError && typeof error.code === 'number') {
    return error.code === GATEWAY_SESSION_NOT_FOUND_CODE
  }

  const message = error instanceof Error ? error.message : String(error ?? '')

  return /session not found/i.test(message)
}

export function isSessionGone(sessionId: string | null | undefined): boolean {
  return Boolean(sessionId && goneSessions.has(sessionId))
}

export function markSessionGone(sessionId: string | null | undefined): void {
  if (sessionId) {
    goneSessions.add(sessionId)
  }
}

/** Clear one runtime after a fresh durable resume, or all entries in tests. */
export function resetSessionGone(sessionId?: string): void {
  if (sessionId) {
    goneSessions.delete(sessionId)
  } else {
    goneSessions.clear()
  }
}

/** @internal Test-only inspection. */
export function _sessionGoneIdsForTests(): string[] {
  return [...goneSessions]
}
