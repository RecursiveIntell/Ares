import assert from 'node:assert/strict'

import { test } from 'vitest'

import { checkDevelopmentInstall, REQUIRED_DEVELOPMENT_PACKAGES } from './assert-root-install.mjs'

test('development admission accepts a resolver that resolves every direct package', () => {
  const seen = []
  const result = checkDevelopmentInstall({
    resolve(packageName) {
      seen.push(packageName)
      return `/node_modules/${packageName}/entry.js`
    }
  })

  assert.deepEqual(result, { missing: [], ok: true })
  assert.deepEqual(seen, REQUIRED_DEVELOPMENT_PACKAGES)
})

test('development admission reports empty or missing direct packages by name', () => {
  const result = checkDevelopmentInstall({
    resolve(packageName) {
      if (packageName === 'vitest' || packageName === '@assistant-ui/react-streamdown') {
        throw new Error('module unavailable')
      }

      return `/node_modules/${packageName}/entry.js`
    }
  })

  assert.equal(result.ok, false)
  assert.deepEqual(result.missing, ['@assistant-ui/react-streamdown', 'vitest'])
})
