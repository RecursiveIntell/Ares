import {
  GatewayReauthRequiredError,
  type GatewayWsConnection,
  resolveGatewayWsUrl,
  type ResolveGatewayWsUrlDeps
} from '@hermes/shared'

import type {
  NativeSocketBridge,
  NativeSocketChannel,
  NativeSocketEvent,
  NativeSocketRoute
} from '../../electron/native-socket-contract'

/** Credential-free route marker consumed ONLY by the native factory. */
export function nativeRouteUrl(route: NativeSocketRoute): string {
  return `ws://native-transport.invalid/${encodeURIComponent(JSON.stringify(route))}`
}

function parseRoute(url: string): NativeSocketRoute {
  const target = new URL(url)

  if (target.origin !== 'ws://native-transport.invalid' || target.search || target.hash) {
    throw new Error('Native transport requires a route selector')
  }

  return JSON.parse(decodeURIComponent(target.pathname.slice(1))) as NativeSocketRoute
}

export function nativeRoute(profile?: null | string, connectionId?: null | string): NativeSocketRoute {
  return connectionId
    ? { kind: 'registry', connectionId, profile: profile || 'default' }
    : profile
      ? { kind: 'profile', profile }
      : { kind: 'primary' }
}

export async function resolveDesktopGatewayDial(
  deps: ResolveGatewayWsUrlDeps,
  conn: GatewayWsConnection,
  route: NativeSocketRoute = { kind: 'primary' }
): Promise<string> {
  if (window.hermesDesktop?.nativeSocket) {
    return nativeRouteUrl(route)
  }

  return resolveGatewayWsUrl(deps, conn)
}

export interface NativeSocketCloseDiagnostic {
  channel: NativeSocketChannel['kind']
  phase: string
  sequence: number
}

export const nativeCloseDiagnostic = (event: CloseEvent): NativeSocketCloseDiagnostic | null => {
  const detail = (event as CloseEvent & { nativeDiagnostic?: unknown }).nativeDiagnostic

  if (!detail || typeof detail !== 'object' || Array.isArray(detail)) {
    return null
  }

  const value = detail as Partial<NativeSocketCloseDiagnostic>

  return typeof value.channel === 'string' && typeof value.phase === 'string' && typeof value.sequence === 'number'
    ? { channel: value.channel as NativeSocketChannel['kind'], phase: value.phase, sequence: value.sequence }
    : null
}

/** DOM-shaped transport; RPC IDs, heartbeat and replay remain in the shared client. */
export class NativeRendererSocket extends EventTarget {
  readonly CONNECTING = 0
  readonly OPEN = 1
  readonly CLOSING = 2
  readonly CLOSED = 3
  readyState = 0
  binaryType: BinaryType = 'arraybuffer'
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  addEventListener<K extends keyof WebSocketEventMap>(
    type: K,
    listener: (event: WebSocketEventMap[K]) => void,
    options?: boolean | AddEventListenerOptions
  ): void
  addEventListener(
    type: string,
    listener: EventListenerOrEventListenerObject | null,
    options?: boolean | AddEventListenerOptions
  ): void
  addEventListener(
    type: string,
    listener: EventListenerOrEventListenerObject | ((event: never) => void) | null,
    options?: boolean | AddEventListenerOptions
  ): void {
    super.addEventListener(type, listener as EventListenerOrEventListenerObject | null, options)
  }
  removeEventListener<K extends keyof WebSocketEventMap>(type: K, listener: (event: WebSocketEventMap[K]) => void): void
  removeEventListener(type: string, listener: EventListenerOrEventListenerObject | null): void
  removeEventListener(
    type: string,
    listener: EventListenerOrEventListenerObject | ((event: never) => void) | null
  ): void {
    super.removeEventListener(type, listener as EventListenerOrEventListenerObject | null)
  }
  private handle?: string
  private sequence = 0
  private readonly correlation = crypto.randomUUID()
  private unsubscribe: () => void
  private timer: ReturnType<typeof setTimeout>
  private sends = 0
  private sendBytes = 0
  constructor(
    private bridge: NativeSocketBridge,
    route: NativeSocketRoute,
    private channel: NativeSocketChannel = { kind: 'gateway' }
  ) {
    super()
    this.unsubscribe = bridge.onEvent(this.correlation, event => this.receive(event))
    this.timer = setTimeout(() => this.fail('open_timeout'), 15_000)
    // Listener attachment always precedes the asynchronous bridge open.
    queueMicrotask(() => {
      if (this.readyState !== 0) {
        return
      }

      void bridge
        .open({ correlation: this.correlation, route, channel })
        .then(result => {
          if (this.handle && this.handle !== result.handle) {
            this.fail('handle_mismatch')

            return
          }

          this.handle = result.handle

          if (this.readyState >= 2) {
            void this.cancel().catch(() => {})
          }
        })
        .catch(() => this.fail('open_rejected'))
    })
  }
  private receive(event: NativeSocketEvent) {
    if (event.correlation !== this.correlation || this.readyState === 3) {
      return
    }

    if ((this.handle && event.handle !== this.handle) || event.sequence !== this.sequence + 1) {
      this.fail('sequence_gap')

      return
    }

    this.handle = event.handle
    this.sequence = event.sequence

    if (event.type === 'open') {
      if (this.readyState !== 0) {
        void this.cancel().catch(() => {})

        return
      }

      clearTimeout(this.timer)
      this.readyState = 1
      const opened = new Event('open')
      this.dispatchEvent(opened)
      this.onopen?.(opened)
    } else if (event.type === 'message') {
      if (this.readyState !== 1) {
        return
      }

      const message = new MessageEvent('message', { data: event.data })

      try {
        this.dispatchEvent(message)
        this.onmessage?.(message)
      } finally {
        void this.bridge.ack({ handle: event.handle, sequence: event.sequence }).catch(() => this.fail('ack_rejected'))
      }
    } else if (event.type === 'error') {
      this.fail('native_error', event.needsOauthLogin)
    } else {
      this.finish(event.code ?? 1006, event.reason ?? '', event.wasClean === true, 'native_close')
    }
  }
  private cancel() {
    return this.bridge.close({ correlation: this.correlation, ...(this.handle ? { handle: this.handle } : {}) })
  }
  private fail(phase: string, oauth = false) {
    if (this.readyState === 3) {
      return
    }

    const event = new Event('error')

    if (oauth) {
      Object.assign(event, { error: new GatewayReauthRequiredError('Gateway sign-in required'), needsOauthLogin: true })
    }

    this.dispatchEvent(event)
    this.onerror?.(event)
    void this.cancel().catch(() => {})
    this.finish(1006, 'Native transport failed', false, phase)
  }
  private finish(code: number, reason: string, wasClean: boolean, phase: string) {
    if (this.readyState === 3) {
      return
    }

    clearTimeout(this.timer)
    this.readyState = 3
    this.unsubscribe()

    const event = Object.assign(new CloseEvent('close', { code, reason, wasClean }), {
      nativeDiagnostic: { channel: this.channel.kind, phase, sequence: this.sequence }
    })

    this.dispatchEvent(event)
    this.onclose?.(event)
  }
  send(data: string) {
    if (this.readyState !== 1 || !this.handle || typeof data !== 'string') {
      throw new Error('Native socket is not open')
    }

    // Bound in-flight IPC copies as well as main's queue. Exact byte accounting
    // is enforced in main; UTF-16 upper bound avoids another huge allocation.
    const bytes = data.length * 2

    if (this.sends >= 128 || this.sendBytes + bytes > 768 * 1024 * 1024) {
      this.fail('send_overflow')
      throw new Error('Native send queue overflow')
    }

    this.sends++
    this.sendBytes += bytes
    void this.bridge
      .send({ handle: this.handle, data })
      .catch(() => this.fail('send_rejected'))
      .finally(() => {
        this.sends--
        this.sendBytes -= bytes
      })
  }
  close(code?: number, reason?: string) {
    if (this.readyState >= 2) {
      return
    }

    if (code !== undefined && code !== 1000 && !(Number.isInteger(code) && code >= 3000 && code <= 4999)) {
      throw new Error('Invalid close code')
    }

    if (reason && new TextEncoder().encode(reason).byteLength > 123) {
      throw new Error('Invalid close reason')
    }

    this.readyState = 2
    clearTimeout(this.timer)
    this.timer = setTimeout(() => this.finish(1006, 'Close timed out', false, 'close_timeout'), 2_500)
    void this.bridge
      .close({
        correlation: this.correlation,
        ...(this.handle ? { handle: this.handle } : {}),
        ...(code === undefined ? {} : { code }),
        ...(reason === undefined ? {} : { reason })
      })
      .then(() => {
        if (!this.handle) {
          this.finish(1000, '', true, 'close_ack')
        }
      })
      .catch(() => this.fail('close_rejected'))
  }
}

export function desktopSocketFactory(url: string) {
  const bridge = window.hermesDesktop?.nativeSocket

  if (!bridge) {
    throw new Error('Native transport unavailable')
  }

  return new NativeRendererSocket(bridge, parseRoute(url))
}
