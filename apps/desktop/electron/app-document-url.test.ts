import { describe, expect, test } from 'vitest'

import { AppDocumentUrl } from './app-document-url'

const INDEX = '/app/dist/index.html'

describe('AppDocumentUrl', () => {
  test('resolves the bundle exactly once across repeated calls', () => {
    let calls = 0

    const url = new AppDocumentUrl({
      resolveIndex: () => {
        calls += 1

        return INDEX
      },
      toUrl: path => `file://${path}`
    })

    expect(url.resolve()).toBe('file:///app/dist/index.html')
    expect(url.resolve()).toBe('file:///app/dist/index.html')
    expect(url.resolve()).toBe('file:///app/dist/index.html')
    // The whole point: admission runs this on every frame/ack/watchdog tick,
    // and each walk costs ~186 ms on the real 28.8 MB bundle.
    expect(calls).toBe(1)
  })

  test('stays cached while the index is untouched, even with a stat probe', () => {
    let calls = 0
    let stats = { mtimeMs: 1000, size: 5000 }

    const url = new AppDocumentUrl({
      resolveIndex: () => {
        calls += 1

        return INDEX
      },
      toUrl: path => `file://${path}`,
      stat: () => stats
    })

    expect(url.resolve()).toBe('file:///app/dist/index.html')
    expect(url.resolve()).toBe('file:///app/dist/index.html')
    stats = { mtimeMs: 1000, size: 5000 }
    expect(url.resolve()).toBe('file:///app/dist/index.html')
    expect(calls).toBe(1)
  })

  test('re-resolves when the index really changed (bundle repair/update)', () => {
    let calls = 0
    let indexPath = INDEX
    let stats = { mtimeMs: 1000, size: 5000 }

    const url = new AppDocumentUrl({
      resolveIndex: () => {
        calls += 1

        return indexPath
      },
      toUrl: path => `file://${path}`,
      stat: () => stats
    })

    expect(url.resolve()).toBe('file:///app/dist/index.html')
    expect(calls).toBe(1)

    // A repair rewrote the bundle: same path, new bytes.
    stats = { mtimeMs: 2000, size: 6000 }
    expect(url.resolve()).toBe('file:///app/dist/index.html')
    expect(calls).toBe(2)

    // …and the newly chosen copy is picked up too.
    indexPath = '/app/resources/app.asar.unpacked/dist/index.html'
    stats = { mtimeMs: 2000, size: 6000 }
    url.invalidate()
    expect(url.resolve()).toBe('file:///app/resources/app.asar.unpacked/dist/index.html')
    expect(calls).toBe(3)
  })

  test('an unreadable stat keeps the cached identity instead of re-walking', () => {
    let calls = 0

    const url = new AppDocumentUrl({
      resolveIndex: () => {
        calls += 1

        return INDEX
      },
      toUrl: path => `file://${path}`,
      stat: () => {
        throw new Error('ENOENT')
      }
    })

    expect(url.resolve()).toBe('file:///app/dist/index.html')
    expect(url.resolve()).toBe('file:///app/dist/index.html')
    expect(calls).toBe(1)
  })

  test('dev server short-circuits without touching the bundle', () => {
    let calls = 0

    const url = new AppDocumentUrl({
      devServer: 'http://127.0.0.1:5174/',
      resolveIndex: () => {
        calls += 1

        return INDEX
      },
      toUrl: path => `file://${path}`
    })

    expect(url.resolve()).toBe('http://127.0.0.1:5174/')
    expect(url.resolve()).toBe('http://127.0.0.1:5174/')
    expect(calls).toBe(0)
  })

  test('invalidate() re-resolves on demand', () => {
    let calls = 0

    const url = new AppDocumentUrl({
      resolveIndex: () => {
        calls += 1

        return INDEX
      },
      toUrl: path => `file://${path}`
    })

    url.resolve()
    url.invalidate()
    url.resolve()
    expect(calls).toBe(2)
  })

  test('cached identity still equality-checks against the live frame URL', async () => {
    // The security property is unaffected by caching: the cached URL is
    // compared to the frame's URL on every admission.
    const { isAppDocument } = await import('./guest-policy')

    const url = new AppDocumentUrl({
      resolveIndex: () => INDEX,
      toUrl: path => `file://${path}`
    })

    const appUrl = url.resolve()

    expect(isAppDocument('file:///app/dist/index.html#/20260101_000000_abc123', appUrl)).toBe(true)
    expect(isAppDocument('file:///app/dist/index.html?win=overlay#/', appUrl)).toBe(true)
    expect(isAppDocument('file:///evil/index.html', appUrl)).toBe(false)
    expect(isAppDocument('https://example.com/index.html', appUrl)).toBe(false)
  })
})
