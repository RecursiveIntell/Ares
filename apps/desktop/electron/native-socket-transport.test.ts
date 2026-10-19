import { EventEmitter } from 'node:events'

import { afterEach, expect, test, vi } from 'vitest'

import { GATEWAY_MESSAGE_BYTES, NativeSocketTransport, validateNativeOpen } from './native-socket-transport'

class Contents extends EventEmitter {
  mainFrame = { url: 'file:///fixture/app.html', detached: false }
}

class Wire extends EventEmitter {
  readyState = 0
  bufferedAmount = 0
  terminate = vi.fn(() => {
    this.readyState = 3
  })
  send = vi.fn((_data: string, callback: (error?: Error) => void) => callback())
  close = vi.fn()
}

function fixture() {
  vi.useFakeTimers()
  const contents = new Contents()
  const owner = { sender: contents, senderFrame: contents.mainFrame }
  const wire = new Wire()
  const events: any[] = []
  const prepare = vi.fn(async () => ({ url: 'ws://inert.invalid/api/ws?token=synthetic', headers: {} }))
  let alive = true
  const resolve = vi.fn(async () => ({ scope: 'primary', alive: () => alive, prepare }))
  const dial = vi.fn(() => wire)

  const transport = new NativeSocketTransport({
    trusted: sender => sender === contents,
    approvedFrame: frame => frame.url === 'file:///fixture/app.html',
    resolve,
    dial,
    emit: (_owner, event) => events.push(event)
  })

  return {
    owner,
    contents,
    wire,
    events,
    prepare,
    resolve,
    dial,
    transport,
    retire: () => {
      alive = false
    }
  }
}

const request = (correlation = 'request_1') => ({
  correlation,
  route: { kind: 'primary' },
  channel: { kind: 'gateway' }
})

async function flush() {
  for (let i = 0; i < 8; i++) {await Promise.resolve()}
}

afterEach(() => vi.useRealTimers())

test.each([
  { url: 'ws://hostile.invalid' },
  { token: 'synthetic' },
  { headers: {} },
  { origin: 'null' },
  { sender: 1 }
])('closed open schema rejects caller authority %j', extra => {
  expect(() => validateNativeOpen({ ...request(), ...extra })).toThrow()
})
test.each(['/../events', '/%2e%2e/events', '//hostile.invalid', '/events?token=x', '/a\\b', '/./events'])(
  'reject plugin namespace escape %s',
  suffix => {
    expect(() =>
      validateNativeOpen({ ...request(), channel: { kind: 'plugin', pluginId: 'kanban', suffix } })
    ).toThrow()
  }
)
test('attachment upper bound covers raw 256 MiB base64 plus JSON without allocating it', () => {
  expect(GATEWAY_MESSAGE_BYTES).toBeGreaterThan(Math.ceil((256 * 1024 * 1024) / 3) * 4 + 4096)
})
test('actual registered current main frame is required before any claim', async () => {
  const f = fixture()

  for (const owner of [
    { sender: new Contents(), senderFrame: f.owner.senderFrame },
    { sender: f.contents, senderFrame: {} },
    { sender: f.contents, senderFrame: null }
  ]) {
    await expect(f.transport.open(owner, request())).rejects.toThrow('sender denied')
  }

  f.contents.mainFrame.detached = true
  await expect(f.transport.open(f.owner, request())).rejects.toThrow('sender denied')
  expect(f.resolve).not.toHaveBeenCalled()
})
test('opaque handles are owner-bound; sends do not interpret RPC', async () => {
  const f = fixture()
  const { handle } = await f.transport.open(f.owner, request())
  await flush()
  f.wire.readyState = 1
  f.wire.emit('open')
  expect(() =>
    f.transport.send({ sender: new Contents(), senderFrame: f.owner.senderFrame }, { handle, data: '{}' })
  ).toThrow()
  await f.transport.send(f.owner, { handle, data: '{"method":"arbitrary.rpc"}' })
  expect(f.wire.send.mock.calls[0][0]).toBe('{"method":"arbitrary.rpc"}')
  f.transport.invalidate(f.contents)
})
test('navigation during claim cancels late completion without credential preparation or stale events', async () => {
  const f = fixture()
  let complete!: (value: any) => void
  f.resolve.mockImplementation(
    () =>
      new Promise(resolve => {
        complete = resolve
      })
  )
  const { handle } = await f.transport.open(f.owner, request())
  f.contents.emit('did-start-navigation', {}, 'file:///fixture/app.html', false, true)
  complete({ scope: 'old', alive: () => true, prepare: f.prepare })
  await flush()
  expect(f.prepare).not.toHaveBeenCalled()
  expect(f.dial).not.toHaveBeenCalled()
  expect(f.events).toEqual([])
  f.contents.emit('did-finish-load')
  expect(() => f.transport.send(f.owner, { handle, data: '{}' })).toThrow()
})
test('navigation during fresh ticket preparation closes the generation and refuses late upgrade', async () => {
  const f = fixture()
  let complete!: (value: any) => void
  f.prepare.mockImplementation(
    () =>
      new Promise(resolve => {
        complete = resolve
      })
  )
  await f.transport.open(f.owner, request())
  await flush()
  f.contents.emit('did-start-navigation', {}, 'file:///fixture/app.html', false, true)
  complete({ url: 'ws://inert.invalid/api/ws?ticket=synthetic', headers: {} })
  await flush()
  expect(f.dial).not.toHaveBeenCalled()
  expect(f.events).toEqual([])
})
test('cancelled unresolved opens still reserve bounded capacity until claims settle', async () => {
  const f = fixture()
  f.resolve.mockImplementation(() => new Promise(() => {}))

  for (let i = 0; i < 8; i++) {
    const correlation = `pending_${i}`
    await f.transport.open(f.owner, request(correlation))
    f.transport.close(f.owner, { correlation })
  }

  await expect(f.transport.open(f.owner, request('overflow'))).rejects.toThrow('capacity')
  expect(f.resolve).toHaveBeenCalledTimes(8)
})
test('handshake timeout and renderer loss release sockets; late open never publishes', async () => {
  const f = fixture()
  await f.transport.open(f.owner, request())
  await flush()
  await vi.advanceTimersByTimeAsync(15_001)
  expect(f.wire.terminate).toHaveBeenCalledOnce()
  f.wire.emit('open')
  expect(f.events.filter(event => event.type === 'open')).toHaveLength(0)
  const replacement = await f.transport.open(f.owner, request('replacement'))
  await flush()
  f.contents.emit('render-process-gone')
  expect(() => f.transport.close(f.owner, { correlation: 'replacement', handle: replacement.handle })).toThrow()
})
test('ordered PCM/text events remain bounded until acknowledged; overflow is terminal', async () => {
  const f = fixture()
  const { handle } = await f.transport.open(f.owner, { ...request(), channel: { kind: 'tts' } })
  await flush()
  f.wire.readyState = 1
  f.wire.emit('open')
  f.wire.emit('message', Buffer.from('{"type":"start"}'), false)
  f.wire.emit('message', Buffer.from([0, 1, 2]), true)
  expect(f.events.map(event => event.type)).toEqual(['open', 'message', 'message'])
  expect(f.events[1].data).toBe('{"type":"start"}')
  expect([...new Uint8Array(f.events[2].data)]).toEqual([0, 1, 2])
  f.transport.ack(f.owner, { handle, sequence: 2 })
  f.transport.ack(f.owner, { handle, sequence: 3 })

  for (let i = 0; i < 129; i++) {f.wire.emit('message', Buffer.from('x'), false)}
  expect(f.events.at(-1).type).toBe('close')
  expect(f.wire.terminate).toHaveBeenCalledOnce()
})
test('backend retirement closes its pinned socket without admitting stale sends', async () => {
  const f = fixture()
  const { handle } = await f.transport.open(f.owner, request())
  await flush()
  f.wire.readyState = 1
  f.wire.emit('open')
  f.retire()
  await vi.advanceTimersByTimeAsync(250)
  expect(f.events.at(-1)).toMatchObject({ type: 'close', code: 1001 })
  expect(() => f.transport.send(f.owner, { handle, data: '{}' })).toThrow()
})
test('failed send is terminal and bounded graceful close escalates', async () => {
  const f = fixture()
  const { handle } = await f.transport.open(f.owner, request())
  await flush()
  f.wire.readyState = 1
  f.wire.emit('open')
  f.wire.send.mockImplementation((_data, callback) => callback(new Error('synthetic failure')))
  await expect(f.transport.send(f.owner, { handle, data: '{}' })).rejects.toThrow('send failed')
  expect(f.events.at(-1).type).toBe('close')
  const next = await f.transport.open(f.owner, request('next'))
  await flush()
  f.wire.readyState = 1
  f.wire.emit('open')
  f.transport.close(f.owner, { handle: next.handle, correlation: 'next' })
  await vi.advanceTimersByTimeAsync(2_001)
  expect(f.wire.terminate).toHaveBeenCalled()
})
