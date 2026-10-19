import { createHash, createHmac } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { createServer } from 'node:http'

import { expect, test } from 'vitest'

import { adoptServedDashboardToken, nativeBootstrapMac } from './dashboard-token'

const key = 'synthetic-owned-child-secret-32-bytes-minimum'
const identity = 'synthetic-child'
const token = 'synthetic-spawn-token'
const sign = (parts: unknown[]) => createHmac('sha256', key).update(JSON.stringify(parts)).digest('hex')

test('an inert listener reflecting request identity cannot impersonate the child', async () => {
  const seen: string[] = []

  const server = createServer((req, res) => {
    seen.push(
      ...Object.values(req.headers)
        .flat()
        .filter((v): v is string => typeof v === 'string')
    )
    res.setHeader('content-type', 'application/json')
    res.end(JSON.stringify({ identity: req.headers['x-hermes-native-identity'], token: 'attacker-selected' }))
  })

  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()

  if (!address || typeof address === 'string') {throw new Error('inert listener did not bind')}

  try {
    await expect(
      adoptServedDashboardToken(`http://127.0.0.1:${address.port}`, token, {
        bootstrapKey: key,
        identity,
        childAlive: () => true
      })
    ).rejects.toThrow()
    expect(seen).not.toContain(key)
    expect(seen).not.toContain(token)
  } finally {
    await new Promise<void>((resolve, reject) => server.close(error => (error ? reject(error) : resolve())))
  }
})

test('native request never discloses child secret or session bearer', async () => {
  let seen: string[] = []
  await expect(
    adoptServedDashboardToken('http://127.0.0.1:9119', token, {
      bootstrapKey: key,
      identity,
      childAlive: () => true,
      fetch: async (_url, options) => {
        seen = Object.values(options.headers)

        return new Response('', { status: 403 })
      }
    })
  ).rejects.toThrow()
  expect(seen).not.toContain(key)
  expect(seen).not.toContain(token)
})

test('fresh authenticated response returns only the already-owned spawn token', async () => {
  const endpoint = 'http://127.0.0.1:9119/auth/desktop-bootstrap'
  const digest = createHash('sha256').update(token).digest('hex')

  const accepted = await adoptServedDashboardToken('http://127.0.0.1:9119', token, {
    bootstrapKey: key,
    identity,
    childAlive: () => true,
    fetch: async (url, options) => {
      expect(url).toBe(endpoint)
      const headers = options.headers
      const nonce = headers['X-Hermes-Native-Nonce']
      const issued = Number(headers['X-Hermes-Native-Issued'])
      expect(headers['X-Hermes-Native-Proof']).toBe(
        sign(['hermes-native-v1', 'request', endpoint, identity, nonce, issued])
      )

      return new Response(
        JSON.stringify({
          protocol: 'hermes-native-v1',
          identity,
          nonce,
          issued_at: issued,
          session_digest: digest,
          proof: sign(['hermes-native-v1', 'response', endpoint, identity, nonce, issued, digest])
        }),
        { status: 200 }
      )
    }
  })

  expect(accepted).toBe(token)
})

test('legacy endpoint absence gives an actionable matched-version denial', async () => {
  await expect(
    adoptServedDashboardToken('http://127.0.0.1:9119', token, {
      bootstrapKey: key,
      identity,
      childAlive: () => true,
      fetch: async () => new Response('', { status: 404 })
    })
  ).rejects.toThrow(/update.*together/i)
})

test('cross-language fixed transcript vectors agree exactly', () => {
  const row = JSON.parse(
    readFileSync(new URL('../../../tests/fixtures/p2-native-bootstrap-v1.json', import.meta.url), 'utf8')
  )

  const fields = [row.endpoint, row.identity, row.nonce, row.issued_at]
  expect(nativeBootstrapMac(row.synthetic_key, [row.protocol, 'request', ...fields])).toBe(row.request_mac)
  expect(nativeBootstrapMac(row.synthetic_key, [row.protocol, 'response', ...fields, row.session_digest])).toBe(
    row.response_mac
  )
})

test.each(['protocol', 'nonce', 'identity', 'issued_at', 'session_digest', 'proof', 'extra', 'oversized', 'malformed'])(
  'refuses altered/replayed or malformed response field %s',
  async field => {
    await expect(
      adoptServedDashboardToken('http://127.0.0.1:9119', token, {
        bootstrapKey: key,
        identity,
        childAlive: () => true,
        fetch: async (_url, options) => {
          const nonce = options.headers['X-Hermes-Native-Nonce']
          const issued = Number(options.headers['X-Hermes-Native-Issued'])
          const digest = createHash('sha256').update(token).digest('hex')

          const result: any = {
            protocol: 'hermes-native-v1',
            identity,
            nonce,
            issued_at: issued,
            session_digest: digest,
            proof: sign([
              'hermes-native-v1',
              'response',
              'http://127.0.0.1:9119/auth/desktop-bootstrap',
              identity,
              nonce,
              issued,
              digest
            ])
          }

          if (field === 'oversized') {return new Response('x'.repeat(5000))}

          if (field === 'malformed') {return new Response('{')}
          result[field] = field === 'issued_at' ? issued - 1 : 'foreign'

          return new Response(JSON.stringify(result))
        }
      })
    ).rejects.toThrow()
  }
)
