import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'

import { test } from 'vitest'

import { adoptServedDashboardToken, nativeBootstrapMac, resolveServedDashboardToken } from './dashboard-token'

const proof = { bootstrapKey: 'synthetic-child-secret-at-least-32-bytes', identity: 'child-identity' }
const spawn = 'synthetic-spawn-token'

function signed(url, options, token = spawn) {
  const nonce = options.headers['X-Hermes-Native-Nonce']
  const issued = Number(options.headers['X-Hermes-Native-Issued'])
  const digest = createHash('sha256').update(token).digest('hex')

  return new Response(
    JSON.stringify({
      protocol: 'hermes-native-v1',
      identity: proof.identity,
      nonce,
      issued_at: issued,
      session_digest: digest,
      proof: nativeBootstrapMac(proof.bootstrapKey, [
        'hermes-native-v1',
        'response',
        url,
        proof.identity,
        nonce,
        issued,
        digest
      ])
    })
  )
}

test('admits the owned spawn token only after child-secret possession verification', async () => {
  const token = await adoptServedDashboardToken('http://127.0.0.1:9120', spawn, {
    ...proof,
    childAlive: () => true,
    fetch: async (url, options) => {
      assert.equal(url, 'http://127.0.0.1:9120/auth/desktop-bootstrap')
      assert.equal(options.redirect, 'error')
      assert.equal(options.headers['X-Hermes-Native-Bootstrap'], undefined)

      return signed(url, options)
    }
  })

  assert.equal(token, spawn)
})

test('does not scrape a public page when only a pinned SSH token is available', async () => {
  assert.equal(
    await resolveServedDashboardToken('http://127.0.0.1:9120', 'pinned-token', {
      fetch: () => {
        throw new Error('public fetch forbidden')
      }
    }),
    'pinned-token'
  )
})

test('denied, mismatched, dead and interrupted native boot never manufacture success', async () => {
  await assert.rejects(() =>
    adoptServedDashboardToken('http://127.0.0.1:9120', spawn, {
      ...proof,
      childAlive: () => true,
      fetch: async () => new Response('', { status: 403 })
    })
  )
  await assert.rejects(
    () =>
      adoptServedDashboardToken('http://127.0.0.1:9120', spawn, {
        ...proof,
        childAlive: () => true,
        fetch: async (url, options) => signed(url, options, 'different-owned-runtime-token')
      }),
    /binding mismatch/
  )
  await assert.rejects(
    () =>
      adoptServedDashboardToken('http://127.0.0.1:9120', spawn, {
        ...proof,
        childAlive: () => false,
        fetch: () => {
          throw new Error('must not fetch')
        }
      }),
    /exited/
  )
  let alive = true
  await assert.rejects(
    () =>
      adoptServedDashboardToken('http://127.0.0.1:9120', spawn, {
        ...proof,
        childAlive: () => alive,
        fetch: async (url, options) => {
          alive = false

          return signed(url, options)
        }
      }),
    /exited during/
  )
})
