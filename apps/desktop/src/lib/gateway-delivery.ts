const GATEWAY_TRANSPORT_ERROR_CODES = new Set([
  'ECONNABORTED',
  'ECONNREFUSED',
  'ECONNRESET',
  'EHOSTUNREACH',
  'ENETUNREACH',
  'ENOTFOUND',
  'EPIPE',
  'ETIMEDOUT',
  'ERR_NETWORK',
  'ERR_SOCKET_CLOSED'
])

export const NON_REPLAYABLE_GATEWAY_METHODS = new Set([
  'prompt.submit',
  'session.interrupt',
  'session.redirect',
  'session.steer'
])

export class GatewayDeliveryUnknownError extends Error {
  readonly cause: unknown

  constructor(readonly method: string, cause: unknown) {
    super(`${method} delivery status is unknown; the gateway may have accepted it, so the request was not retried.`)
    this.name = 'GatewayDeliveryUnknownError'
    this.cause = cause
  }
}

function errorCode(value: unknown): string | null {
  if (typeof value !== 'object' || value === null) {
    return null
  }

  const code = (value as { code?: unknown }).code

  return typeof code === 'string' ? code.toUpperCase() : null
}

export function isGatewayTransportFailure(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)

  if (/not connected|connection closed|connection reset|ECONNRESET/i.test(message)) {
    return true
  }

  const cause = typeof error === 'object' && error !== null ? (error as { cause?: unknown }).cause : undefined

  return [error, cause].some(value => {
    const code = errorCode(value)

    return code !== null && GATEWAY_TRANSPORT_ERROR_CODES.has(code)
  })
}

export function isGatewayDeliveryUnknownError(error: unknown): error is GatewayDeliveryUnknownError {
  return error instanceof GatewayDeliveryUnknownError
}

export function gatewayDeliveryUnknownError(method: string, error: unknown): GatewayDeliveryUnknownError | null {
  if (error instanceof GatewayDeliveryUnknownError) {
    return error
  }

  if (!NON_REPLAYABLE_GATEWAY_METHODS.has(method)) {
    return null
  }

  const message = error instanceof Error ? error.message : String(error)
  const requestTimedOut = /request timed out/i.test(message) && message.includes(method)

  return isGatewayTransportFailure(error) || requestTimedOut ? new GatewayDeliveryUnknownError(method, error) : null
}
