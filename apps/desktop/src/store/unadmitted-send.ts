import { atom } from 'nanostores'

import type { ComposerAttachment } from './composer'

/** A send that was dispatched but not yet admitted by the backend.
 *
 * Recorded at `prompt.submit` and cleared when the turn shows signs of life
 * (`message.start`). If the host refuses admission, the send is re-queued from
 * this record and its optimistic bubble is dropped — otherwise the transcript
 * keeps a user message that never ran, and the text is unrecoverable.
 */
export interface UnadmittedSend {
  optimisticId: string
  /** Composer queue key (lineage root), not the runtime id. Null when the
   *  stored-session resolution failed; the refusal handler falls back to an
   *  honest "send it again" notice rather than guessing a queue. */
  queueKey: null | string
  text: string
  attachments: ComposerAttachment[]
  recordedAt: number
}

export const $unadmittedSendBySession = atom<Record<string, UnadmittedSend>>({})

export function recordUnadmittedSend(sessionId: null | string, send: Omit<UnadmittedSend, 'recordedAt'>) {
  if (!sessionId) {
    return
  }

  $unadmittedSendBySession.set({
    ...$unadmittedSendBySession.get(),
    [sessionId]: { ...send, recordedAt: Date.now() }
  })
}

export function takeUnadmittedSend(sessionId: null | string): null | UnadmittedSend {
  if (!sessionId) {
    return null
  }

  const map = $unadmittedSendBySession.get()
  const send = map[sessionId]

  if (!send) {
    return null
  }

  const { [sessionId]: _drop, ...rest } = map
  $unadmittedSendBySession.set(rest)

  return send
}

export function clearUnadmittedSend(sessionId: null | string): void {
  if (!sessionId || !(sessionId in $unadmittedSendBySession.get())) {
    return
  }

  const { [sessionId]: _drop, ...rest } = $unadmittedSendBySession.get()
  $unadmittedSendBySession.set(rest)
}
