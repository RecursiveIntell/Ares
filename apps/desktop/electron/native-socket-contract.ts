/** Closed transport capability. No endpoint or credential crosses this seam. */
export type NativeSocketRoute =
  | { kind: 'primary' }
  | { kind: 'profile'; profile: string }
  | { kind: 'registry'; connectionId: string; profile: string }
export type NativeSocketChannel =
  { kind: 'gateway' } | { kind: 'tts' } | { kind: 'plugin'; pluginId: string; suffix: string }
export interface NativeSocketOpen {
  correlation: string
  route: NativeSocketRoute
  channel: NativeSocketChannel
}
export interface NativeSocketEvent {
  correlation: string
  handle: string
  sequence: number
  type: 'open' | 'message' | 'error' | 'close'
  data?: string | ArrayBuffer
  code?: number
  reason?: string
  wasClean?: boolean
  needsOauthLogin?: boolean
}
export interface NativeSocketBridge {
  open(request: NativeSocketOpen): Promise<{ handle: string }>
  send(request: { handle: string; data: string }): Promise<void>
  close(request: { correlation: string; handle?: string; code?: number; reason?: string }): Promise<void>
  ack(request: { handle: string; sequence: number }): Promise<void>
  onEvent(correlation: string, listener: (event: NativeSocketEvent) => void): () => void
}
