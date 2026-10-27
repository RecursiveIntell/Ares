import { atom } from 'nanostores'

import { keyedTimeouts } from '@/lib/keyed-timeouts'

import { $gateway } from './gateway'
import { isSessionGone, isSessionGoneError, markSessionGone } from './session-gone'

export type GoalStatus = 'active' | 'done' | 'paused' | 'waiting'

export interface SessionGoal {
  detail?: string
  status: GoalStatus
  title: string
  updatedAt: number
}

/** Structured goal state carried by `status.update.goal_state`.
 *
 * The backend owns goal state; the text line is a human-facing projection of
 * it. Preferring this object keeps the composer pill correct even when the
 * prose grammar changes (it previously missed every `⊙ Goal active · …` line
 * and left a stale "Goal waiting" chip on screen indefinitely).
 */
export interface GoalStatePayload {
  continuation_pending?: boolean
  max_turns?: number
  outcome?: string | null
  status?: string
  title?: string | null
  turns_used?: number
  waiting?: boolean
}

export const $goalsBySession = atom<Record<string, SessionGoal>>({})

const DONE_LINGER_MS = 8_000
const clearTimers = keyedTimeouts()

export function setSessionGoal(sid: string, goal: SessionGoal) {
  if (!sid) {
    return
  }

  clearTimers.cancel(sid)
  $goalsBySession.set({ ...$goalsBySession.get(), [sid]: goal })

  if (goal.status === 'done') {
    clearTimers.schedule(sid, DONE_LINGER_MS, () => clearSessionGoal(sid))
  }
}

/** Apply the authoritative structured state for one session.
 *
 * Unlike the text parser this is not a guess: `status`, `outcome`, and
 * `waiting` come straight from `GoalManager`. `WAITING_FOR_AUTHORITY` is the
 * one outcome that must read as "waiting" rather than "active" — the model
 * judged the goal done but the operator has not supplied completion evidence.
 */
export function applyGoalState(sid: string, state: GoalStatePayload | null | undefined) {
  if (!sid || !state) {
    return
  }

  const title = (state.title ?? '').trim()
  const status = (state.status ?? '').trim()

  if (status === 'cleared' || (!title && status !== 'done')) {
    clearSessionGoal(sid)

    return
  }

  const now = Date.now()

  if (status === 'done') {
    setSessionGoal(sid, { status: 'done', title: title || 'Standing goal', updatedAt: now })

    return
  }

  if (status === 'paused') {
    setSessionGoal(sid, { status: 'paused', title: title || 'Standing goal', updatedAt: now })

    return
  }

  const waiting = state.waiting === true || state.outcome === 'WAITING_FOR_AUTHORITY'

  setSessionGoal(sid, {
    ...(waiting ? { detail: 'waiting for authority' } : {}),
    status: waiting ? 'waiting' : 'active',
    title: title || 'Standing goal',
    updatedAt: now
  })
}

export function clearSessionGoal(sid: string) {
  clearTimers.cancel(sid)

  const map = $goalsBySession.get()

  if (!(sid in map)) {
    return
  }

  const { [sid]: _drop, ...rest } = map
  $goalsBySession.set(rest)
}

const clean = (value: string): string => value.replace(/\r/g, '').trim()

const firstLine = (value: string): string => clean(value).split('\n')[0]?.trim() ?? ''

function goalTitleFromLine(line: string, pattern: RegExp): string {
  return (line.match(pattern)?.[1] ?? '').trim()
}

function nextGoalFromText(text: string, previous?: SessionGoal): SessionGoal | null | undefined {
  const body = clean(text)
  const line = firstLine(body)

  if (!line) {
    return undefined
  }

  if (
    /^No active goal\b/i.test(line) ||
    /^No goal (?:set|to resume)\b/i.test(line) ||
    /^✓ Goal cleared\b/i.test(line)
  ) {
    return null
  }

  const now = Date.now()
  const fromSet = goalTitleFromLine(line, /^⊙ Goal set(?:\s*\([^)]*\))?:\s*(.+)$/)
  const fromActive = goalTitleFromLine(line, /^⊙ Goal\s*\([^)]*active[^)]*\):\s*(.+)$/)
  const fromResume = goalTitleFromLine(line, /^▶ Goal resumed:\s*(.+)$/)

  if (fromSet || fromActive || fromResume) {
    return { status: 'active', title: fromSet || fromActive || fromResume, updatedAt: now }
  }

  // `⊙ Goal active · running (4/20 turns): <title>` and its siblings —
  // waiting for authority, checkpointed/continuation pending, blocked,
  // budget exhausted — plus the timed `⏳ Goal active · waiting (…)` form.
  // These are what `GoalManager.status_line()` actually emits for an active
  // goal; the parser above only matched the `⊙ Goal (…active…):` shape, so
  // every real active/waiting line fell through and the chip kept whatever it
  // last understood (typically "Goal waiting" forever).
  if (/^[⊙⏳] Goal active\b/.test(line)) {
    const title = goalTitleFromLine(line, /^[⊙⏳] Goal active\b[^:]*\([^)]*\):\s*(.+)$/)

    if (title) {
      const waiting = /waiting for authority|·\s*waiting\s*\(/i.test(line)

      return {
        ...(waiting ? { detail: 'waiting for authority' } : {}),
        status: waiting ? 'waiting' : 'active',
        title,
        updatedAt: now
      }
    }
  }

  const fromWaiting = goalTitleFromLine(line, /^⏳ Goal\s*\([^)]*(?:parked|active)[^)]*\):\s*(.+)$/)

  if (fromWaiting) {
    return { status: 'waiting', title: fromWaiting, updatedAt: now }
  }

  const fromPaused = goalTitleFromLine(line, /^⏸ Goal(?:\s*\([^)]*\)| paused)?:\s*(.+)$/)

  if (fromPaused) {
    return { status: 'paused', title: fromPaused, updatedAt: now }
  }

  const fromDone = goalTitleFromLine(line, /^✓ Goal done\s*\([^)]*\):\s*(.+)$/)

  if (fromDone) {
    return { status: 'done', title: fromDone, updatedAt: now }
  }

  if (/^↻ Continuing toward goal\b/i.test(line)) {
    return {
      detail: line.replace(/^↻\s*/, ''),
      status: 'active',
      title: previous?.title || 'Standing goal',
      updatedAt: now
    }
  }

  if (/^⏳ Goal parked\b/i.test(line)) {
    return {
      detail: line.replace(/^⏳\s*/, ''),
      status: 'waiting',
      title: previous?.title || 'Standing goal',
      updatedAt: now
    }
  }

  if (/^⏸ Goal paused\b/i.test(line)) {
    return {
      detail: line.replace(/^⏸\s*/, ''),
      status: 'paused',
      title: previous?.title || 'Standing goal',
      updatedAt: now
    }
  }

  if (/^✓ Goal achieved\b/i.test(line)) {
    return {
      detail: line.replace(/^✓\s*/, ''),
      status: 'done',
      title: previous?.title || 'Standing goal',
      updatedAt: now
    }
  }

  return undefined
}

export function applyGoalStatusText(sid: string, text: string, opts?: { hydrate?: boolean }) {
  if (!sid) {
    return
  }

  const next = nextGoalFromText(text, $goalsBySession.get()[sid])

  if (next === null) {
    clearSessionGoal(sid)
  } else if (next) {
    // A done goal is terminal state in the backend DB — it stays "done"
    // forever (only /goal clear or a new goal replaces it). The 8s linger is
    // for the LIVE completion moment; re-hydrating "✓ Goal done" on every
    // mount would resurrect the chip indefinitely. Bot Mode is the worst
    // case: one endless session means the completed layover would never go
    // away. On hydration, a terminal goal is the same as no goal.
    if (opts?.hydrate && next.status === 'done') {
      clearSessionGoal(sid)

      return
    }

    setSessionGoal(sid, next)
  }
}

export async function refreshSessionGoal(sid: string): Promise<void> {
  const gateway = $gateway.get()

  if (!sid || !gateway || isSessionGone(sid)) {
    return
  }

  try {
    const result = await gateway.request<{ output?: string }>('slash.exec', { command: 'goal status', session_id: sid })

    applyGoalStatusText(sid, result?.output ?? '', { hydrate: true })
  } catch (error) {
    if (isSessionGoneError(error)) {
      markSessionGone(sid)
    }

    // Best-effort: older gateways or detached sessions simply won't hydrate it.
  }
}
