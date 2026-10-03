import type { SessionInfo } from '../types.js'

/** Only the backend can attest that this exact live session/model has run. */
export const modelIsReady = (info: null | SessionInfo, sid: null | string): boolean =>
  Boolean(sid && info?.session_id === sid && info.model_ready === true && info.provider && info.model)

export const idleModelStatus = (info: null | SessionInfo, sid: null | string): string =>
  modelIsReady(info, sid) ? 'ready' : 'model unverified'
