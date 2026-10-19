import { randomUUID } from 'node:crypto'

import type { NativeSocketEvent, NativeSocketOpen, NativeSocketRoute } from './native-socket-contract'

// 256 MiB raw file.attach becomes ~342 MiB base64 plus JSON. Matches backend cap.
export const GATEWAY_MESSAGE_BYTES = 384 * 1024 * 1024
export const TTS_MESSAGE_BYTES = 1024 * 1024
const PLUGIN_MESSAGE_BYTES = 4 * 1024 * 1024
const MAX_DOCUMENT = 24
const MAX_PROCESS = 64
const MAX_PENDING_DOCUMENT = 8
const MAX_PENDING_PROCESS = 16
const QUEUE_COUNT = 128
const PROCESS_QUEUE_BYTES = 512 * 1024 * 1024
const HANDSHAKE_MS = 15_000
const CONGESTION_MS = 15_000
const CLOSE_MS = 2_000

function closedObject(value: unknown, keys: string[]): asserts value is Record<string, any> {
  if (
    !value ||
    typeof value !== 'object' ||
    Array.isArray(value) ||
    Object.keys(value).some(key => !keys.includes(key))
  )
    {throw new Error('Invalid transport schema')}
}

function identifier(value: unknown): asserts value is string {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(value))
    {throw new Error('Invalid transport identifier')}
}

export function validateNativeOpen(raw: unknown): NativeSocketOpen {
  closedObject(raw, ['correlation', 'route', 'channel'])
  identifier(raw.correlation)
  closedObject(raw.route, ['kind', 'profile', 'connectionId'])
  const route = raw.route

  if (route.kind === 'primary') {closedObject(route, ['kind'])}
  else if (route.kind === 'profile') {
    closedObject(route, ['kind', 'profile'])
    identifier(route.profile)
  } else if (route.kind === 'registry') {
    identifier(route.connectionId)
    identifier(route.profile)
  } else {throw new Error('Invalid transport route')}

  closedObject(raw.channel, ['kind', 'pluginId', 'suffix'])

  if (raw.channel.kind === 'gateway' || raw.channel.kind === 'tts') {closedObject(raw.channel, ['kind'])}
  else if (raw.channel.kind === 'plugin') {
    identifier(raw.channel.pluginId)
    // No URL, query credentials, encoded traversal, backslash, or namespace escape.
    const suffix = raw.channel.suffix

    if (typeof suffix !== 'string' || suffix.length > 512)
      {throw new Error('Invalid plugin suffix')}
    // Kanban's selected-board stream selects its board via `/events?board=<slug>`;
    // preserve exactly that one non-credential parameter and nothing else.
    const queryAt = suffix.indexOf('?')
    const suffixPath = queryAt === -1 ? suffix : suffix.slice(0, queryAt)
    const suffixQuery = queryAt === -1 ? '' : suffix.slice(queryAt)

    if (!/^\/(?:[A-Za-z0-9_-]+\/?)*$/.test(suffixPath))
      {throw new Error('Invalid plugin suffix')}
    if (suffixQuery && !/^\?board=[A-Za-z0-9-_.~!*'()%]{1,256}$/.test(suffixQuery))
      {throw new Error('Invalid plugin suffix')}
      {throw new Error('Invalid plugin suffix')}
  } else {throw new Error('Invalid transport channel')}

  return raw as NativeSocketOpen
}

interface Owner {
  sender: any
  senderFrame: any
}
export interface ResolvedNativeBackend {
  scope: string
  oauth?: boolean
  alive: () => boolean
  prepare: (
    channel: NativeSocketOpen['channel'],
    current: () => void
  ) => Promise<{ url: string; headers: Record<string, string> }>
}
export interface NativeWire {
  readyState: number
  bufferedAmount: number
  on(name: string, listener: (...args: any[]) => void): unknown
  send(data: string, callback: (error?: Error) => void): void
  close(code?: number, reason?: string): void
  terminate(): void
}
interface Entry {
  owner: Owner
  generation: number
  request: NativeSocketOpen
  handle: string
  sequence: number
  pending: boolean
  dead: boolean
  closing?: boolean
  backend?: ResolvedNativeBackend
  wire?: NativeWire
  timer: ReturnType<typeof setTimeout>
  watchdog?: ReturnType<typeof setInterval>
  queued: Map<number, number>
  bytes: number
  sends: number
  sendBytes: number
  progress: number
}
/** Transport only: deliberately never parses JSON-RPC. */
export class NativeSocketTransport {
  private entries = new Map<string, Entry>()
  private resolving = new Set<Entry>()
  private documents = new WeakMap<object, number>()
  private blocked = new WeakSet<object>()
  private watched = new WeakSet<object>()
  private queueBytes = 0
  constructor(
    private deps: {
      trusted: (contents: any) => boolean
      approvedFrame: (frame: any) => boolean
      resolve: (route: NativeSocketRoute, current: () => void) => Promise<ResolvedNativeBackend>
      dial: (url: string, headers: Record<string, string>, maxPayload: number) => NativeWire
      emit: (owner: Owner, event: NativeSocketEvent) => void
    }
  ) {}
  private admit(owner: Owner): number {
    if (
      !owner.sender ||
      !this.deps.trusted(owner.sender) ||
      this.blocked.has(owner.sender) ||
      !owner.senderFrame ||
      owner.senderFrame.detached ||
      owner.senderFrame !== owner.sender.mainFrame ||
      !this.deps.approvedFrame(owner.senderFrame)
    )
      {throw new Error('Transport sender denied')}

    this.watch(owner.sender)

    return this.documents.get(owner.sender) ?? 0
  }
  register(contents: any) {
    this.watch(contents)
  }
  private watch(contents: any) {
    if (this.watched.has(contents)) {return}
    this.watched.add(contents)
    contents.on('did-start-navigation', (_event: unknown, _url: string, inPlace: boolean, main: boolean) => {
      if (main && !inPlace) {
        this.blocked.add(contents)
        this.invalidate(contents)
      }
    })
    contents.on('did-finish-load', () => this.blocked.delete(contents))
    contents.on('render-process-gone', () => {
      this.blocked.add(contents)
      this.invalidate(contents)
    })
    contents.once('destroyed', () => {
      this.blocked.add(contents)
      this.invalidate(contents)
    })
  }
  invalidate(contents: any) {
    this.documents.set(contents, (this.documents.get(contents) ?? 0) + 1)

    for (const entry of this.entries.values())
      {if (entry.owner.sender === contents) {this.finish(entry, 1001, 'Document retired', false, false)}}
  }
  private documentCurrent(entry: Entry): boolean {
    try {
      return !entry.dead && this.admit(entry.owner) === entry.generation
    } catch {
      return false
    }
  }
  private current(entry: Entry): boolean {
    try {
      return this.documentCurrent(entry) && (!entry.backend || entry.backend.alive())
    } catch {
      return false
    }
  }
  private check(entry: Entry) {
    if (!this.current(entry)) {
      this.finish(entry, 1001, 'Transport retired')
      throw new Error('Transport retired')
    }
  }
  private limit(entry: Entry) {
    return entry.request.channel.kind === 'gateway'
      ? GATEWAY_MESSAGE_BYTES
      : entry.request.channel.kind === 'tts'
        ? TTS_MESSAGE_BYTES
        : PLUGIN_MESSAGE_BYTES
  }
  private documentQueue(entry: Entry) {
    const own = [...this.entries.values()].filter(row => row.owner.sender === entry.owner.sender)

    return {
      bytes: own.reduce((sum, row) => sum + row.bytes + row.sendBytes, 0),
      count: own.reduce((sum, row) => sum + row.queued.size + row.sends, 0)
    }
  }
  private emit(entry: Entry, event: Omit<NativeSocketEvent, 'correlation' | 'handle' | 'sequence'>) {
    if (!this.documentCurrent(entry)) {return}

    try {
      this.deps.emit(entry.owner, {
        ...event,
        correlation: entry.request.correlation,
        handle: entry.handle,
        sequence: ++entry.sequence
      })
    } catch {
      this.finish(entry, 1006, 'Renderer unavailable', false, false)
    }
  }
  private finish(entry: Entry, code = 1006, reason = 'Native transport failed', clean = false, publish = true) {
    if (entry.dead) {return}

    if (publish) {this.emit(entry, { type: 'close', code, reason, wasClean: clean })}

    if (entry.dead) {return}
    entry.dead = true
    clearTimeout(entry.timer)
    clearInterval(entry.watchdog)
    this.queueBytes -= entry.bytes + entry.sendBytes
    entry.bytes = entry.sendBytes = 0
    entry.queued.clear()
    this.entries.delete(entry.handle)
    // Hard teardown bounds failed upgrades and backpressured close handshakes.
    entry.wire?.terminate()
  }
  private failure(entry: Entry, oauth = false) {
    this.emit(entry, { type: 'error', needsOauthLogin: oauth })
    this.finish(entry)
  }
  async open(owner: Owner, raw: unknown): Promise<{ handle: string }> {
    const generation = this.admit(owner) // Before schema/route/credentials.
    const request = validateNativeOpen(raw)
    const reserved = [...new Set([...this.entries.values(), ...this.resolving])]
    const own = reserved.filter(row => row.owner.sender === owner.sender)

    if (
      own.some(row => row.request.correlation === request.correlation) ||
      own.length >= MAX_DOCUMENT ||
      reserved.length >= MAX_PROCESS ||
      own.filter(row => row.pending).length >= MAX_PENDING_DOCUMENT ||
      reserved.filter(row => row.pending).length >= MAX_PENDING_PROCESS
    )
      {throw new Error('Native transport capacity exceeded')}

    const entry: Entry = {
      owner: { sender: owner.sender, senderFrame: owner.senderFrame },
      generation,
      request,
      handle: randomUUID(),
      sequence: 0,
      pending: true,
      dead: false,
      queued: new Map(),
      bytes: 0,
      sends: 0,
      sendBytes: 0,
      progress: Date.now(),
      timer: undefined!
    }

    this.entries.set(entry.handle, entry)
    this.resolving.add(entry)
    entry.timer = setTimeout(() => this.failure(entry), HANDSHAKE_MS)
    // Returning the handle does not publish open. The subscribed event stream does.
    void this.start(entry)

    return { handle: entry.handle }
  }
  private async start(entry: Entry) {
    try {
      entry.backend = await this.deps.resolve(entry.request.route, () => this.check(entry))
      this.check(entry)
      const target = await entry.backend.prepare(entry.request.channel, () => this.check(entry))
      this.check(entry)
      const wire = this.deps.dial(target.url, target.headers, this.limit(entry))
      entry.wire = wire
      this.check(entry)
      wire.on('open', () => {
        try {
          this.check(entry)
          entry.pending = false
          clearTimeout(entry.timer)
          this.emit(entry, { type: 'open' })
        } catch {
          /* retired */
        }
      })
      wire.on('error', () => this.failure(entry))
      wire.on('unexpected-response', (_request, response) => {
        const oauth = entry.backend?.oauth && [401, 403].includes(response.statusCode)
        response.destroy?.()
        this.failure(entry, oauth)
      })
      wire.on('close', (code: number, reason: Buffer) =>
        this.finish(entry, code, reason.toString().slice(0, 123), code !== 1006)
      )
      wire.on('message', (bytes: Buffer, binary: boolean) => {
        try {
          this.check(entry)
          const size = bytes.byteLength

          if (
            (binary && entry.request.channel.kind !== 'tts') ||
            size > this.limit(entry) ||
            entry.queued.size >= QUEUE_COUNT ||
            entry.bytes + size > this.limit(entry) ||
            this.documentQueue(entry).bytes + size > GATEWAY_MESSAGE_BYTES ||
            this.documentQueue(entry).count >= 256 ||
            this.queueBytes + size > PROCESS_QUEUE_BYTES
          )
            {throw new Error('Native event queue overflow')}

          const data = binary ? Uint8Array.from(bytes).buffer : bytes.toString('utf8')
          entry.bytes += size
          this.queueBytes += size
          const sequence = entry.sequence + 1
          entry.queued.set(sequence, size)
          this.emit(entry, { type: 'message', data })
        } catch {
          this.failure(entry)
        }
      })
      let congestedSince = 0
      entry.watchdog = setInterval(() => {
        if (!this.current(entry)) {
          this.finish(entry, 1001, 'Backend retired')

          return
        }

        if (wire.bufferedAmount || entry.queued.size || entry.sends) {
          congestedSince ||= Date.now()

          if (Date.now() - Math.max(congestedSince, entry.progress) > CONGESTION_MS) {this.failure(entry)}
        } else {congestedSince = 0}
      }, 250)
    } catch (error: any) {
      if (error?.code === 'PLUGIN_OAUTH_POLLING')
        {this.finish(entry, 1008, 'Plugin OAuth stream unavailable; use polling')}
      else {this.failure(entry, error?.needsOauthLogin === true)}
    } finally {
      this.resolving.delete(entry)
    }
  }
  private owned(owner: Owner, handle: unknown): Entry {
    const generation = this.admit(owner)
    const entry = typeof handle === 'string' ? this.entries.get(handle) : undefined

    if (
      !entry ||
      entry.owner.sender !== owner.sender ||
      entry.owner.senderFrame !== owner.senderFrame ||
      entry.generation !== generation
    )
      {throw new Error('Native socket ownership denied')}

    this.check(entry)

    return entry
  }
  send(owner: Owner, raw: unknown): Promise<void> {
    closedObject(raw, ['handle', 'data'])
    const entry = this.owned(owner, raw.handle)
    const bytes = typeof raw.data === 'string' ? Buffer.byteLength(raw.data, 'utf8') : Infinity

    if (
      entry.pending ||
      entry.closing ||
      !entry.wire ||
      entry.wire.readyState !== 1 ||
      bytes > this.limit(entry) ||
      entry.sends >= QUEUE_COUNT ||
      entry.sendBytes + bytes > this.limit(entry) ||
      this.documentQueue(entry).bytes + bytes > GATEWAY_MESSAGE_BYTES ||
      this.documentQueue(entry).count >= 256 ||
      this.queueBytes + bytes > PROCESS_QUEUE_BYTES
    ) {
      this.failure(entry)
      throw new Error('Native send congestion or payload limit')
    }

    entry.sends++
    entry.sendBytes += bytes
    this.queueBytes += bytes

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.failure(entry)
        reject(new Error('Native send timed out'))
      }, CONGESTION_MS)

      const complete = (error?: Error) => {
        clearTimeout(timer)

        if (!entry.dead) {
          entry.sends--
          entry.sendBytes -= bytes
          this.queueBytes -= bytes
          entry.progress = Date.now()
        }

        if (error || !this.current(entry)) {
          this.failure(entry)
          reject(new Error('Native send failed'))
        } else {resolve()}
      }

      try {
        entry.wire!.send(raw.data, complete)
      } catch {
        complete(new Error('Native send failed'))
      }
    })
  }
  ack(owner: Owner, raw: unknown) {
    closedObject(raw, ['handle', 'sequence'])
    const entry = this.owned(owner, raw.handle)

    if (!Number.isSafeInteger(raw.sequence) || !entry.queued.has(raw.sequence))
      {throw new Error('Invalid transport acknowledgement')}

    const size = entry.queued.get(raw.sequence)!
    entry.queued.delete(raw.sequence)
    entry.bytes -= size
    this.queueBytes -= size
    entry.progress = Date.now()
  }
  close(owner: Owner, raw: unknown) {
    closedObject(raw, ['correlation', 'handle', 'code', 'reason'])
    const generation = this.admit(owner)
    identifier(raw.correlation)

    const entry = raw.handle
      ? this.owned(owner, raw.handle)
      : [...this.entries.values()].find(
          row =>
            row.owner.sender === owner.sender &&
            row.owner.senderFrame === owner.senderFrame &&
            row.generation === generation &&
            row.request.correlation === raw.correlation
        )

    if (!entry) {return}

    if (
      entry.request.correlation !== raw.correlation ||
      (raw.code !== undefined &&
        raw.code !== 1000 &&
        !(Number.isInteger(raw.code) && raw.code >= 3000 && raw.code <= 4999)) ||
      (raw.reason !== undefined && (typeof raw.reason !== 'string' || Buffer.byteLength(raw.reason) > 123))
    )
      {throw new Error('Invalid transport close')}

    if (entry.pending) {
      this.finish(entry, 1000, 'Open cancelled', true)

      return
    }

    if (entry.closing) {return}
    entry.closing = true
    clearTimeout(entry.timer)
    entry.timer = setTimeout(() => this.finish(entry), CLOSE_MS)

    try {
      entry.wire!.close(raw.code ?? 1000, raw.reason ?? '')
    } catch {
      this.failure(entry)
    }
  }
}
