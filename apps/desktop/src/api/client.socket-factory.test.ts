import { afterEach, expect, test, vi } from 'vitest'

import { HermesGateway } from './client'

class Socket extends EventTarget {
  readyState = 0
  send = vi.fn()
  close() {
    this.readyState = 3
    this.dispatchEvent(new Event('close'))
  }
}
afterEach(() => vi.unstubAllGlobals())

test('HermesGateway preserves its RPC defaults while accepting an explicit socket factory', async () => {
  class ForbiddenRawSocket {
    static OPEN = 1
    static CONNECTING = 0
    static CLOSED = 3
    constructor() {
      throw new Error('raw renderer socket forbidden by fixture')
    }
  }
  vi.stubGlobal('WebSocket', ForbiddenRawSocket)
  const socket = new Socket()
  const factory = vi.fn(() => socket as unknown as WebSocket)
  const gateway = new HermesGateway({ socketFactory: factory })

  try {
    const pending = gateway.connect('ws://inert.invalid/api/ws')
    void pending.catch(() => {})
    expect(factory).toHaveBeenCalledOnce()
    socket.readyState = 1
    socket.dispatchEvent(new Event('open'))
    await pending
    expect(gateway.connectionState).toBe('open')
  } finally {
    gateway.close()
  }
})
