import assert from 'node:assert/strict'

import { test } from 'vitest'

import { admitPreviewGuest, installSessionPermissions, isAppDocument } from './guest-policy'

test('request/check policies bind exact privileged document, main frame and audio scope', () => {
  let request, check
  const appUrl = 'http://127.0.0.1:5174'
  const app = { getURL: () => appUrl + '/?chat=1' }
  installSessionPermissions(
    {
      setPermissionRequestHandler: handler => {
        request = handler
      },
      setPermissionCheckHandler: handler => {
        check = handler
      }
    },
    contents => contents === app,
    () => appUrl
  )
  const details = { isMainFrame: true, requestingUrl: appUrl + '/', mediaTypes: ['audio'], mediaType: 'audio' }

  for (const permission of [
    'geolocation',
    'notifications',
    'clipboard-read',
    'clipboard-sanitized-write',
    'display-capture',
    'fileSystem'
  ]) {
    request(app, permission, result => assert.equal(result, false), details)
    assert.equal(check(app, permission, appUrl, details), false)
  }

  request(app, 'media', result => assert.equal(result, true), details)
  assert.equal(check(app, 'media', appUrl, details), true)

  for (const modified of [
    { ...details, isMainFrame: false },
    { ...details, requestingUrl: 'http://127.0.0.1:5175/' },
    { ...details, requestingUrl: 'http://localhost:5174/' },
    { ...details, mediaTypes: ['video'], mediaType: 'video' },
    { ...details, mediaTypes: undefined, mediaType: 'unknown' }
  ]) {
    request(app, 'media', result => assert.equal(result, false), modified)
    assert.equal(check(app, 'media', appUrl, modified), false)
  }

  request(null, 'media', result => assert.equal(result, false), details)
  assert.equal(check({ getURL: () => appUrl }, 'media', appUrl, details), false)
})

test('guest attach canonical partition is sandboxed; unknown partitions and preloads deny', () => {
  let prevented = false

  const event = {
    preventDefault: () => {
      prevented = true
    }
  }

  const prefs: any = { nodeIntegration: true, sandbox: false, contextIsolation: false }
  const sess = {}
  admitPreviewGuest(event, prefs, { partition: 'persist:hermes-preview', src: 'https://example.test/' }, () => sess)
  assert.equal(prevented, false)
  assert.equal(prefs.session, sess)
  assert.equal(prefs.sandbox, true)
  assert.equal(prefs.contextIsolation, true)
  assert.equal(prefs.nodeIntegration, false)

  for (const params of [
    { partition: 'persist:unknown', src: 'https://example.test/' },
    { partition: 'persist:hermes-preview', src: 'javascript:void(0)' },
    { partition: 'persist:hermes-preview', src: 'https://example.test/', preload: 'file:///tmp/evil.js' }
  ]) {
    prevented = false
    admitPreviewGuest(event, {}, params, () => {
      throw new Error('no session effects on denial')
    })
    assert.equal(prevented, true)
  }

  assert.equal(isAppDocument('file:///other/index.html', 'file:///app/index.html'), false)
})
