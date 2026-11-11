import { atom } from 'nanostores'

import { keyedTimeouts } from '@/lib/keyed-timeouts'

import { $gateway } from './gateway'
import { isSessionGone, isSessionGoneError, markSessionGone } from './session-gone'

export type GoalStatus = 'active' | 'done' | 'paused' | 'waiting'

export interface SessionGoal {
  detail?: string
  /** Provenance of the last write (F1.3): 'notice' (backend send-directive),
   * 'structured' (goal_state twin), or 'hydrate' (refreshSessionGoal read).
   * The hydration gate reads this to distinguish authority from race.
   */
  writtenBy?: GoalWrittenBy
  /** Runtime id that produced the last write (F1.3 second-pass minor 1). */
  runtimeId?: string
  status: GoalStatus
  title: string
  updatedAt: number
}

export type GoalWrittenBy = 'notice' | 'structured' | 'hydrate'

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

// Bumped on every goal write/clear. Verification responses capture it when
// scheduled and discard themselves if it moved — a stale "no active goal"
// reply must not clear a chip a newer notice just wrote.
let goalWriteGeneration = 0

export function setSessionGoal(sid: string, goal: SessionGoal) {
  if (!sid) {
    return
  }

  clearTimers.cancel(sid)
  goalWriteGeneration += 1
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
export function applyGoalState(
  sid: string,
  state: GoalStatePayload | null | undefined,
  opts?: { runtimeId?: string; mapKey?: string }
) {
  if (!sid || !state) {
    return
  }

  const title = (state.title ?? '').trim()
  const status = (state.status ?? '').trim()
  const mapKey = opts?.mapKey ?? sid

  if (status === 'cleared' || (!title && status !== 'done')) {
    clearSessionGoal(mapKey)

    return
  }

  const now = Date.now()

  if (status === 'done') {
    setSessionGoal(mapKey, { status: 'done', title: title || 'Standing goal', updatedAt: now, writtenBy: 'structured', runtimeId: opts?.runtimeId })

    return
  }

  if (status === 'paused') {
    setSessionGoal(mapKey, { status: 'paused', title: title || 'Standing goal', updatedAt: now, writtenBy: 'structured', runtimeId: opts?.runtimeId })

    return
  }

  const waiting = state.waiting === true || state.outcome === 'WAITING_FOR_AUTHORITY'

  setSessionGoal(mapKey, {
    ...(waiting ? { detail: 'waiting for authority' } : {}),
    status: waiting ? 'waiting' : 'active',
    title: title || 'Standing goal',
    updatedAt: now,
    writtenBy: 'structured',
    runtimeId: opts?.runtimeId
  })
}

export function clearSessionGoal(sid: string) {
  clearTimers.cancel(sid)

  const map = $goalsBySession.get()

  if (!(sid in map)) {
    return
  }

  const { [sid]: _drop, ...rest } = map
  goalWriteGeneration += 1
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

export function applyGoalStatusText(
  sid: string,
  text: string,
  opts?: { hydrate?: boolean; runtimeId?: string; mapKey?: string }
) {
  if (!sid) {
    return
  }

  const mapKey = opts?.mapKey ?? sid
  const previous = $goalsBySession.get()[mapKey]
  const next = nextGoalFromText(text, previous)

  if (next === null) {
    // F1.3 hydration gate with provenance: a chip freshly written from an
    // authoritative source (backend notice or structured goal_state) is not
    // cleared by a single hydration read saying "No active goal" — that
    // hydration may be racing the very turn that set the goal (and, in the
    // deferred-write case, the DB may legitimately be empty while the
    // in-memory manager holds the goal). One verification re-read settles it:
    // two consecutive "no goal" hydrations agree, so clear then.
    if (
      opts?.hydrate &&
      previous?.writtenBy &&
      previous.writtenBy !== 'hydrate' &&
      canPerformVerificationRecheck(previous)
    ) {
      scheduleHydrationVerification(sid, mapKey, opts.runtimeId)

      return
    }

    clearSessionGoal(mapKey)
  } else if (next) {
    // A done goal is terminal state in the backend DB — it stays "done"
    // forever (only /goal clear or a new goal replaces it). The 8s linger is
    // for the LIVE completion moment; re-hydrating "✓ Goal done" on every
    // mount would resurrect the chip indefinitely. Bot Mode is the worst
    // case: one endless session means the completed layover would never go
    // away. On hydration, a terminal goal is the same as no goal.
    if (opts?.hydrate && next.status === 'done') {
      clearSessionGoal(mapKey)

      return
    }

    setSessionGoal(mapKey, {
      ...next,
      writtenBy: opts?.hydrate ? 'hydrate' : 'notice',
      runtimeId: opts?.runtimeId
    })
  }
}

// One verification re-check per clear-suppression, keyed by map key. A second
// consecutive "No active goal" hydration result clears the chip — agreement
// of two reads is authority enough without re-plumbing the backend.
const HYDRATION_VERIFY_DELAY_MS = 1_500
const hydrationVerifyTimers = keyedTimeouts()
const hydrationVerifyCounts = new Map<string, number>()

function canPerformVerificationRecheck(previous: SessionGoal): boolean {
  // Bounded: after two suppressed clears, the next one wins regardless of
  // provenance (a stuck non-goal state must still be clearable).
  return (hydrationVerifyCounts.get(keyOfInterest(previous)) ?? 0) < 2
}

function keyOfInterest(previous: SessionGoal): string {
  return `${previous.status}:${previous.title}`
}

function scheduleHydrationVerification(sid: string, mapKey: string, runtimeId?: string) {
  const countKey = mapKey
  const nextCount = (hydrationVerifyCounts.get(countKey) ?? 0) + 1
  hydrationVerifyCounts.set(countKey, nextCount)
  hydrationVerifyTimers.cancel(`${mapKey}:verify`)
  const generation = goalWriteGeneration
  hydrationVerifyTimers.schedule(`${mapKey}:verify`, HYDRATION_VERIFY_DELAY_MS, () => {
    void performVerificationHydration(sid, mapKey, runtimeId, nextCount, generation)
  })
}

async function performVerificationHydration(
  sid: string,
  mapKey: string,
  runtimeId: string | undefined,
  attempt: number,
  generation: number
) {
  const gateway = $gateway.get()

  if (!gateway || isSessionGone(sid)) {
    return
  }

  if (generation !== goalWriteGeneration) {
    // A newer goal notice or structured update landed while this verification
    // was in flight — the response is stale and must not clear/overwrite the
    // freshly written chip.
    return
  }

  try {
    const result = await gateway.request<{ output?: string }>('slash.exec', { command: 'goal status', session_id: sid })
    const text = result?.output ?? ''
    const next = nextGoalFromText(text, $goalsBySession.get()[mapKey])

    if (next === null) {
      // Second consecutive "no goal" — clear. Reset the counter.
      hydrationVerifyCounts.delete(mapKey)
      clearSessionGoal(mapKey)
    } else if (next) {
      // Goal confirmed live — restore/keep and reset the counter.
      hydrationVerifyCounts.delete(mapKey)
      if (next.status === 'done') {
        clearSessionGoal(mapKey)

        return
      }

      setSessionGoal(mapKey, { ...next, writtenBy: 'hydrate', runtimeId })
    } else {
      // Unparseable — leave whatever is on screen; one more verification pass
      // is allowed before the bounded counter gives up.
      if (attempt >= 2) {
        hydrationVerifyCounts.delete(mapKey)
        clearSessionGoal(mapKey)
      }
    }
  } catch (error) {
    if (isSessionGoneError(error)) {
      markSessionGone(sid)
    }
  }
}

/** Re-house a goal chip from a runtime-keyed entry to its stored-session key
 * (F1.2 second-pass minor 1). Called once the runtime→stored binding becomes
 * known. Composite: read + clear + set is not atomic across nanostores
 * subscribers, so this runs synchronously as one function — no observable
 * intermediate state (the chip disappears from the old key and appears under
 * the new one in the same task; nanostore computes fire once).
 */
export function rehouseGoalChip(fromKey: string, toKey: string) {
  if (!fromKey || !toKey || fromKey === toKey) {
    return
  }

  const map = $goalsBySession.get()
  const chip = map[fromKey]

  if (!chip) {
    return
  }

  const { [fromKey]: _drop, ...rest } = map
  $goalsBySession.set({ ...rest, [toKey]: chip })
}

export async function refreshSessionGoal(
  sid: string,
  opts?: { mapKey?: string }
): Promise<void> {
  const gateway = $gateway.get()

  if (!sid || !gateway || isSessionGone(sid)) {
    return
  }

  try {
    const result = await gateway.request<{ output?: string }>('slash.exec', { command: 'goal status', session_id: sid })

    applyGoalStatusText(sid, result?.output ?? '', {
      hydrate: true,
      mapKey: opts?.mapKey,
      runtimeId: sid
    })
  } catch (error) {
    if (isSessionGoneError(error)) {
      markSessionGone(sid)
    }

    // Best-effort: older gateways or detached sessions simply won't hydrate it.
  }
}
