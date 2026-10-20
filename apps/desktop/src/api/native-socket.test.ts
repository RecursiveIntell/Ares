import { afterEach, expect, test, vi } from 'vitest'

import type { NativeSocketBridge, NativeSocketEvent, NativeSocketOpen } from '../../electron/native-socket-contract'

import { HermesGateway } from './client'
import { NativeRendererSocket, nativeRouteUrl, resolveDesktopGatewayDial } from './native-socket'

function fixture() {
  let listener: ((event: NativeSocketEvent) => void) | undefined
  let request: NativeSocketOpen
  let sequence = 0

  const unsubscribe = vi.fn(() => {
    listener = undefined
  })

  const bridge: NativeSocketBridge = {
    open: vi.fn(async raw => {
      request = raw

      return { handle: 'opaque' }
    }),
    send: vi.fn(async () => {}),
    close: vi.fn(async () => {}),
    ack: vi.fn(async () => {}),
    onEvent: vi.fn((_correlation, callback) => {
      listener = callback

      return unsubscribe
    })
  }

  const emit = (event: Partial<NativeSocketEvent>) =>
    listener?.({ correlation: request.correlation, handle: 'opaque', sequence: ++sequence, type: 'open', ...event })

  return { bridge, emit, unsubscribe }
}

async function flush() {
  for (let i = 0; i < 8; i++) {
    await Promise.resolve()
  }
}

afterEach(() => {
  Reflect.deleteProperty(window, 'hermesDesktop')
  vi.useRealTimers()
})
test('native dial resolves a noncredential route without minting or reading cached ticket URL', async () => {
  const f = fixture()
  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { nativeSocket: f.bridge } })
  const mint = vi.fn()
  const route = { kind: 'registry' as const, connectionId: 'source', profile: 'research' }
  expect(
    await resolveDesktopGatewayDial(
      { getGatewayWsUrl: mint },
      { authMode: 'oauth', wsUrl: 'wss://inert.invalid/?ticket=must-not-read' },
      route
    )
  ).toBe(nativeRouteUrl(route))
  expect(mint).not.toHaveBeenCalled()
})
test('synchronous CONNECTING attaches listeners before async open, preserves text/binary FIFO, removes listeners', async () => {
  const f = fixture()
  const socket = new NativeRendererSocket(f.bridge, { kind: 'primary' }, { kind: 'tts' })
  expect(socket.readyState).toBe(0)
  expect(f.bridge.open).not.toHaveBeenCalled()
  const open = vi.fn()
  const removed = vi.fn()
  const frames: unknown[] = []
  socket.addEventListener('open', open, { once: true })
  socket.addEventListener('message', removed)
  socket.removeEventListener('message', removed)
  socket.onmessage = event => frames.push(event.data)
  socket.binaryType = 'arraybuffer'
  await flush()
  f.emit({ type: 'open' })
  f.emit({ type: 'message', data: '{"type":"start"}' })
  f.emit({ type: 'message', data: Uint8Array.from([1, 2, 3]).buffer })
  expect(socket.readyState).toBe(1)
  expect(open).toHaveBeenCalledOnce()
  expect(removed).not.toHaveBeenCalled()
  expect(frames[0]).toBe('{"type":"start"}')
  expect([...new Uint8Array(frames[1] as ArrayBuffer)]).toEqual([1, 2, 3])
  expect(f.bridge.ack).toHaveBeenCalledTimes(2)
  f.emit({ type: 'close', code: 4001, reason: 'synthetic close', wasClean: true })
  expect(socket.readyState).toBe(3)
  expect(f.unsubscribe).toHaveBeenCalledOnce()
})
test('pending close cancels by correlation; late handle/open cannot revive adapter', async () => {
  const f = fixture()
  let resolve!: (value: { handle: string }) => void
  f.bridge.open = vi.fn(async raw => {
    await f.bridge.send({ handle: 'inert', data: raw.correlation })

    return new Promise<{ handle: string }>(done => {
      resolve = done
    })
  })
  const socket = new NativeRendererSocket(f.bridge, { kind: 'primary' })
  await flush()
  socket.close()
  await flush()
  expect(f.bridge.close).toHaveBeenCalledWith(expect.objectContaining({ correlation: expect.any(String) }))
  resolve({ handle: 'late' })
  await flush()
  expect(socket.readyState).toBe(3)
  expect(f.bridge.close).toHaveBeenCalledWith(expect.objectContaining({ handle: 'late' }))
})
test('late open after a close timeout is cancelled without reviving or leaking the subscription', async () => {
  vi.useFakeTimers()
  const f = fixture()
  let resolve!: (value: { handle: string }) => void
  f.bridge.open = vi.fn(
    () =>
      new Promise<{ handle: string }>(done => {
        resolve = done
      })
  )
  f.bridge.close = vi.fn(() => new Promise<void>(() => {}))
  const socket = new NativeRendererSocket(f.bridge, { kind: 'primary' })
  const closed = vi.fn()
  socket.onclose = closed
  await flush()
  socket.close()
  await vi.advanceTimersByTimeAsync(2_501)
  expect(socket.readyState).toBe(3)
  expect(closed).toHaveBeenCalledOnce()
  expect(f.unsubscribe).toHaveBeenCalledOnce()
  resolve({ handle: 'late' })
  await flush()
  expect(f.bridge.close).toHaveBeenLastCalledWith(expect.objectContaining({ handle: 'late' }))
  expect(f.unsubscribe).toHaveBeenCalledOnce()
  expect(closed).toHaveBeenCalledOnce()
})

test('failed asynchronous native send closes RPC socket and promptly rejects pending calls', async () => {
  const f = fixture()
  f.bridge.send = vi.fn(async () => {
    throw new Error('synthetic failed send')
  })
  const gateway = new HermesGateway({ socketFactory: () => new NativeRendererSocket(f.bridge, { kind: 'primary' }) })
  const connected = gateway.connect(nativeRouteUrl({ kind: 'primary' }))
  await flush()
  f.emit({ type: 'open' })
  await connected
  await expect(gateway.request('fixture.rpc')).rejects.toThrow('gateway connection closed')
  expect(f.unsubscribe).toHaveBeenCalledOnce()
  expect(gateway.connectionState).toBe('closed')
})
test('native OAuth failure preserves reauth classification across shared client', async () => {
  const f = fixture()
  const gateway = new HermesGateway({ socketFactory: () => new NativeRendererSocket(f.bridge, { kind: 'primary' }) })
  const pending = gateway.connect(nativeRouteUrl({ kind: 'primary' }))
  const assertion = expect(pending).rejects.toMatchObject({ needsOauthLogin: true })
  await flush()
  f.emit({ type: 'error', needsOauthLogin: true })
  await assertion
})
