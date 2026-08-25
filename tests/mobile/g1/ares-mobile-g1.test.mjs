import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const harness = path.join(here, 'ares-mobile-g1.mjs')

test('G1 harness records missing environment as a blocked receipt', async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'ares-mobile-g1-'))
  const receiptPath = path.join(dir, 'receipt.json')
  try {
    const result = spawnSync(process.execPath, [harness, '--receipt', receiptPath], {
      encoding: 'utf8',
      env: {
        HOME: process.env.HOME ?? '',
        PATH: process.env.PATH ?? '',
        TMPDIR: process.env.TMPDIR ?? os.tmpdir(),
      },
    })

    assert.equal(result.status, 2, result.stderr)
    const receipt = JSON.parse(await readFile(receiptPath, 'utf8'))
    assert.equal(receipt.schema, 'AresMobileG1ReceiptV1')
    assert.equal(receipt.state, 'blocked_environment')
    assert.deepEqual(receipt.missing_environment, [
      'ARES_G1_HOST_ID',
      'ARES_G1_SESSION_ID',
      'ARES_G1_WS_URL',
    ])
    assert.equal(receipt.scope, 'reference_preflight_only')
    assert.equal(receipt.g1_phase_claim, false)
  } finally {
    await rm(dir, { force: true, recursive: true })
  }
})
