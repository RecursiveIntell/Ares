import { atom, computed } from 'nanostores'

import { $gateway } from './gateway'
import { $activeSessionId } from './session'
import { requestForOwnedSession } from './session-states'

export interface ClarifyQuestion {
  /** Server-generated wire id (q0..qN) — clarify.respond keys answers by it. */
  qid: string
  question: string
  choices: string[] | null
  multiSelect: boolean
}

export interface ClarifyInteraction {
  draft: string
  selectedChoices: string[]
  staged: Record<string, { choices: string[]; draft: string }>
  submitting: boolean
  sendError: string | null
  deliveryUncertain: boolean
  accepted: boolean
  acceptedAnswer: string | null
  cancelled: boolean
  attempt: number
}

export const emptyClarifyInteraction: ClarifyInteraction = {
  draft: '',
  selectedChoices: [],
  staged: {},
  submitting: false,
  sendError: null,
  deliveryUncertain: false,
  accepted: false,
  acceptedAnswer: null,
  cancelled: false,
  attempt: 0
}

export interface ClarifyScope {
  requestId: string
  sessionId: string | null
  generation?: number
}

export interface ClarifyResponseToken extends ClarifyScope {
  attempt: number
}

export interface ClarifyRequest {
  requestId: string
  /** Renderer lifecycle identity, retained on replay and renewed after cleanup. */
  generation?: number
  interaction?: ClarifyInteraction
  /** Local delivery receipt after authoritative absence of a backend blocker. */
  deliveryOnly?: boolean
  /** Terminal response state reported by the owning Gateway. */
  responseState?: 'conflict' | 'expired'
  question: string
  choices: string[] | null
  multiSelect: boolean
  /** Local receipt time (Unix seconds), used to reject stale resume cleanup. */
  receivedAt?: number
  sessionId: string | null
  /** Batch (multi-question) clarify: present instead of question/choices. */
  questions?: ClarifyQuestion[]
  /** Answers already locked server-side (reconnect replay): qid → answer. */
  lockedAnswers?: Record<string, string>
}

/**
 * The backend labels the agent's recommended option by appending this to the
 * first choice (`tools/clarify_tool.py::mark_recommended`). The renderer never
 * writes it — it only styles it, and discounts it when measuring a choice so a
 * long option isn't dropped for length the label added.
 */
export const RECOMMENDED_LABEL = '(Recommended)'

export const bareChoice = (choice: string): string =>
  choice.endsWith(RECOMMENDED_LABEL) ? choice.slice(0, -RECOMMENDED_LABEL.length).trim() : choice

/**
 * Validate and normalize a choices array.
 *
 * Keeps non-blank, newline-free strings of length ≤ 200; drops everything else
 * and returns an empty array when nothing usable survives — the caller then
 * falls back to a free-text answer instead of dead buttons.
 */
export function normalizeChoices(choices: unknown): string[] {
  if (!Array.isArray(choices)) {
    return []
  }

  return choices.filter(
    (c): c is string => typeof c === 'string' && c.trim().length > 0 && bareChoice(c).length <= 200 && !c.includes('\n')
  )
}

/**
 * Structured warning for a clarify payload that arrived with choices but had
 * them all normalized away — keeps the remaining #69122 "no selectable choices"
 * triggers diagnosable in the field without dead constant fields.
 */
export function warnDroppedChoices(source: 'gateway' | 'tool_args', question: string, rawChoices: unknown): void {
  console.warn('[clarify] choices dropped after normalization', {
    choices_count: Array.isArray(rawChoices) ? rawChoices.length : 0,
    question_length: question.length,
    source
  })
}

/**
 * Validate and normalize a batch clarify payload's `questions` array.
 *
 * Keeps entries with a non-blank string `qid` and `question`; per-question
 * choices go through `normalizeChoices` (all-blank → open-ended) and
 * multi_select is only honored alongside surviving choices. Returns an empty
 * array when nothing usable remains — the caller treats that as "not a
 * batch" instead of rendering an unanswerable form.
 */
export function normalizeQuestions(questions: unknown): ClarifyQuestion[] {
  if (!Array.isArray(questions)) {
    return []
  }

  const normalized: ClarifyQuestion[] = []

  for (const entry of questions) {
    if (typeof entry !== 'object' || entry === null) {
      continue
    }

    const row = entry as Record<string, unknown>
    const qid = typeof row.qid === 'string' ? row.qid.trim() : ''
    const question = typeof row.question === 'string' ? row.question.trim() : ''

    if (!qid || !question) {
      continue
    }

    const choices = normalizeChoices(row.choices)

    normalized.push({
      choices: choices.length > 0 ? choices : null,
      multiSelect: row.multi_select === true && choices.length > 0,
      qid,
      question
    })
  }

  return normalized
}

// Pending clarify requests keyed by the runtime session id that raised them.
// Storing per-session (instead of one shared slot) lets a *background* session
// park its clarify request while the user is looking at a different chat, then
// resolve it once they switch over — without a second concurrent clarify
// clobbering the first. A request with no session id lands under the empty key.
const keyFor = (sessionId: string | null | undefined): string => sessionId ?? ''

export const $clarifyRequests = atom<Record<string, ClarifyRequest>>({})

// The active session's live blocker, excluding display-only delivery receipts.
export const $clarifyRequest = computed([$clarifyRequests, $activeSessionId], (requests, activeId) => {
  const request = requests[keyFor(activeId)]

  return request && !request.deliveryOnly ? request : null
})

/** Inline card state for one session, including retained delivery receipts. */
export const sessionClarifyRequest = (sessionId: string | null) =>
  computed($clarifyRequests, requests => requests[keyFor(sessionId)] ?? null)

let clarifyGeneration = 0

export function setClarifyRequest(request: ClarifyRequest): void {
  const requests = $clarifyRequests.get()
  const key = keyFor(request.sessionId)
  const current = requests[key]

  const sameRequest = current?.requestId === request.requestId

  const nextRequest = {
    ...request,
    generation: sameRequest ? current.generation : ++clarifyGeneration,
    interaction: sameRequest ? current.interaction : { ...emptyClarifyInteraction },
    deliveryOnly: false,
    responseState: sameRequest ? current.responseState : undefined
  }

  $clarifyRequests.set({ ...requests, [key]: nextRequest })
}

/** An absent blocker says nothing about an answer whose receipt was lost. */
export function retainClarifyDelivery(scope: ClarifyScope): boolean {
  const requests = $clarifyRequests.get()
  const key = keyFor(scope.sessionId)
  const current = requests[key]

  if (!current || current.requestId !== scope.requestId || current.generation !== scope.generation) {
    return false
  }

  const interaction = current.interaction

  if (!interaction) {
    return false
  }

  const submitted = interaction.submitting || interaction.deliveryUncertain || interaction.accepted

  const drafted =
    interaction.draft.trim() ||
    interaction.selectedChoices.length ||
    Object.values(interaction.staged).some(stage => stage.draft.trim() || stage.choices.length)

  if (!submitted && !drafted) {
    return false
  }

  $clarifyRequests.set({
    ...requests,
    [key]: {
      ...current,
      deliveryOnly: true,
      responseState: current.responseState ?? (submitted ? undefined : 'expired')
    }
  })

  return true
}

/** Update only this lifecycle; a late promise cannot mutate a replacement. */
export function updateClarifyInteraction(
  scope: ClarifyScope,
  patch: Partial<ClarifyInteraction> | ((current: ClarifyInteraction) => Partial<ClarifyInteraction>)
): boolean {
  const requests = $clarifyRequests.get()
  const key = keyFor(scope.sessionId)
  const current = requests[key]

  if (!current || current.requestId !== scope.requestId || current.generation !== scope.generation) {
    return false
  }

  const interaction = current.interaction ?? emptyClarifyInteraction
  const delta = typeof patch === 'function' ? patch(interaction) : patch
  $clarifyRequests.set({ ...requests, [key]: { ...current, interaction: { ...interaction, ...delta } } })

  return true
}

/** Synchronous admission survives remounts and blocks duplicate clicks/windows. */
export function beginClarifyResponse(scope: ClarifyScope): ClarifyResponseToken | null {
  const current = $clarifyRequests.get()[keyFor(scope.sessionId)]

  if (
    !current ||
    current.requestId !== scope.requestId ||
    current.generation !== scope.generation ||
    current.responseState ||
    current.interaction?.accepted ||
    current.interaction?.submitting
  ) {
    return null
  }

  const attempt = (current.interaction?.attempt ?? 0) + 1
  updateClarifyInteraction(scope, {
    attempt,
    submitting: true,
    sendError: current.interaction?.deliveryUncertain ? current.interaction.sendError : null
  })

  return { requestId: scope.requestId, sessionId: scope.sessionId, generation: scope.generation, attempt }
}

export function isClarifyResponseCurrent(token: ClarifyResponseToken): boolean {
  const current = $clarifyRequests.get()[keyFor(token.sessionId)]

  return (
    current?.requestId === token.requestId &&
    current.generation === token.generation &&
    current.interaction?.attempt === token.attempt
  )
}

export function finishClarifyResponse(token: ClarifyResponseToken): void {
  const current = $clarifyRequests.get()[keyFor(token.sessionId)]

  if (current?.interaction?.attempt === token.attempt) {
    updateClarifyInteraction(token, { submitting: false })
  }
}

/** Mark only the exact request which received a terminal Gateway response. */
export function markClarifyResponseState(
  requestId: string,
  sessionId: string | null | undefined,
  responseState: ClarifyRequest['responseState'],
  generation?: number
): void {
  const requests = $clarifyRequests.get()
  const key = keyFor(sessionId)
  const current = requests[key]

  if (
    !current ||
    current.requestId !== requestId ||
    !responseState ||
    (generation !== undefined && current.generation !== generation)
  ) {
    return
  }

  $clarifyRequests.set({ ...requests, [key]: { ...current, responseState } })
}

export function clearClarifyRequest(requestId?: string, sessionId?: string | null): void {
  const requests = $clarifyRequests.get()

  // Targeted clear when the caller knows the session (the common path from the
  // inline ClarifyTool answering its own request).
  if (sessionId !== undefined) {
    const key = keyFor(sessionId)
    const current = requests[key]

    if (!current || (requestId && current.requestId !== requestId)) {
      return
    }

    const next = { ...requests }
    delete next[key]
    $clarifyRequests.set(next)

    return
  }

  // Fallback with no session hint: drop every entry matching the request id
  // (or clear all when none is given).
  const next: Record<string, ClarifyRequest> = {}
  let changed = false

  for (const [key, value] of Object.entries(requests)) {
    if (requestId && value.requestId !== requestId) {
      next[key] = value
    } else {
      changed = true
    }
  }

  if (changed) {
    $clarifyRequests.set(next)
  }
}

/** Whether `sessionId` has a clarify parked on it right now (imperative read —
 *  the composer checks this on Enter, not on every render). */
export const hasClarifyRequest = (sessionId: string | null | undefined): boolean =>
  Boolean($clarifyRequests.get()[keyFor(sessionId)] && !$clarifyRequests.get()[keyFor(sessionId)].deliveryOnly)

/**
 * Answer `sessionId`'s pending clarify with an empty answer (a skip) and drop it
 * locally, resolving to whether there was one to skip.
 *
 * The composer uses this when the user types a real message instead of picking
 * an option: a clarify blocks the agent inside its tool batch, so leaving it
 * unanswered would park the follow-up until the server-side clarify timeout
 * (default 5 min) — the message looks sent and nothing happens. Skipping lets
 * the tool return and the turn carry on with the user's actual words.
 *
 * An empty answer is the same thing the card's own Skip button sends, and
 * `clarify.respond` is `allow_expired`, so racing the timeout is harmless.
 */
export async function skipClarifyRequest(sessionId: string | null | undefined): Promise<boolean> {
  const request = $clarifyRequests.get()[keyFor(sessionId)]

  if (!request || request.deliveryOnly) {
    return false
  }

  // Clear first: the answer is already decided, and an in-flight RPC must not
  // leave a live card the user can answer a second time.
  clearClarifyRequest(request.requestId, request.sessionId)

  try {
    const gateway = $gateway.get()

    if (gateway) {
      // The composer may now be focused on a different profile. The pending
      // request's session, not the ambient socket, owns this skip response.
      await requestForOwnedSession(
        request.sessionId,
        gateway.request.bind(gateway) as typeof gateway.request,
        'clarify.respond',
        { request_id: request.requestId, session_id: request.sessionId, answer: '' }
      )
    }
  } catch {
    // The tool times out on its own; a failed skip must never swallow the
    // message the user is actually sending.
  }

  return true
}
