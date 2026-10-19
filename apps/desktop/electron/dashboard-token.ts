/** Child-secret possession proof. Request/response contain no raw secret or bearer. */
import { createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto'

const DEFAULT_TOKEN_FETCH_TIMEOUT_MS = 3_000
const PROTOCOL = 'hermes-native-v1'

function mac(key: string, parts: unknown[]): string {
  return createHmac('sha256', key).update(JSON.stringify(parts), 'utf8').digest('hex')
}

function equalHex(actual: unknown, expected: string): boolean {
  return (
    typeof actual === 'string' &&
    /^[0-9a-f]{64}$/.test(actual) &&
    timingSafeEqual(Buffer.from(actual, 'hex'), Buffer.from(expected, 'hex'))
  )
}

async function boundedJson(response: Response): Promise<any> {
  const reader = response.body?.getReader()

  if (!reader) {
    throw new Error('Missing native bootstrap proof')
  }
  const chunks: Uint8Array[] = []
  let size = 0

  try {
    while (true) {
      const row = await reader.read()

      if (row.done) {
        break
      }
      size += row.value.length

      if (size > 4096) {
        await reader.cancel()
        throw new Error('Oversized native bootstrap proof')
      }

      chunks.push(row.value)
    }
  } finally {
    reader.releaseLock()
  }

  return JSON.parse(Buffer.concat(chunks).toString('utf8'))
}

async function resolveServedDashboardToken(baseUrl, fallbackToken, options: any = {}) {
  // The SSH path owns its independent pinned-token/endpoint proof. No HTML scraping.
  if (options.bootstrapKey === undefined && options.identity === undefined) {
    return fallbackToken
  }
  const key = options.bootstrapKey
  const identity = options.identity

  if (
    typeof key !== 'string' ||
    !/^[\x21-\x7e]{32,256}$/.test(key) ||
    typeof identity !== 'string' ||
    !/^[A-Za-z0-9_-]{1,128}$/.test(identity) ||
    typeof fallbackToken !== 'string' ||
    !/^[\x21-\x7e]{1,256}$/.test(fallbackToken)
  ) {
    throw new Error('Native bootstrap pairing unavailable; update Desktop and local backend together.')
  }

  const endpoint = new URL('/auth/desktop-bootstrap', baseUrl).toString()
  const nonce = randomBytes(32).toString('hex')
  const issued = Math.floor(Date.now() / 1000)
  const parts = [PROTOCOL, 'request', endpoint, identity, nonce, issued]

  const response = await (options.fetch || fetch)(endpoint, {
    headers: {
      'X-Hermes-Native-Identity': identity,
      'X-Hermes-Native-Nonce': nonce,
      'X-Hermes-Native-Issued': String(issued),
      'X-Hermes-Native-Proof': mac(key, parts)
    },
    redirect: 'error',
    signal: AbortSignal.timeout(options.timeoutMs ?? DEFAULT_TOKEN_FETCH_TIMEOUT_MS)
  })

  if (response.status !== 200) {
    throw new Error(`Native bootstrap denied (${response.status}); update Desktop and local backend together.`)
  }

  const result = await boundedJson(response)
  const digest = createHash('sha256').update(fallbackToken, 'utf8').digest('hex')
  const fields = ['identity', 'issued_at', 'nonce', 'proof', 'protocol', 'session_digest']

  if (
    !result ||
    typeof result !== 'object' ||
    Array.isArray(result) ||
    Object.keys(result).sort().join(',') !== fields.join(',') ||
    result.protocol !== PROTOCOL ||
    result.identity !== identity ||
    result.nonce !== nonce ||
    result.issued_at !== issued ||
    !equalHex(result.session_digest, digest) ||
    !equalHex(result.proof, mac(key, [PROTOCOL, 'response', endpoint, identity, nonce, issued, digest]))
  ) {
    throw new Error('Native backend possession/token binding mismatch')
  }

  // Nothing from the listener is adopted as a new credential.
  return fallbackToken
}

async function adoptServedDashboardToken(baseUrl, spawnToken, { childAlive, label = 'Hermes backend', ...options }) {
  if (!childAlive()) {
    throw new Error(`${label} exited; refusing its session token.`)
  }
  const servedToken = await resolveServedDashboardToken(baseUrl, spawnToken, options)

  if (!childAlive()) {
    throw new Error(`${label} exited during native bootstrap.`)
  }

  return servedToken
}

export {
  adoptServedDashboardToken,
  DEFAULT_TOKEN_FETCH_TIMEOUT_MS,
  mac as nativeBootstrapMac,
  resolveServedDashboardToken
}
