import { atom } from 'nanostores'

import { keyedTimeouts } from '@/lib/keyed-timeouts'

import { $gateway } from './gateway'
import { isSessionGone, isSessionGoneError, markSessionGone } from './session-gone'

export type GoalStatus = 'active' | 'done' | 'paused' | 'waiting' | 'blocked' | 'awaiting_input'

export interface SessionGoal {
  detail?: string
  goalId?: string
  revision?: number
  nextAction?: string
  turnsUsed?: number
  maxTurns?: number
  autoEligible?: boolean
  status: GoalStatus
  title: string
  updatedAt: number
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

/** Typed backend projection is authoritative; prose is an old-runtime fallback. */
export function applyGoalProjection(sid: string, value: unknown): boolean {
  if (value === null) {
    clearSessionGoal(sid)

    return true
  }
  if (typeof value !== 'object' || !value) {
    return false
  }
  const goal = value as Record<string, unknown>
  if (
    typeof goal.goal_id !== 'string' ||
    typeof goal.title !== 'string' ||
    !['active', 'done', 'paused', 'waiting', 'blocked', 'awaiting_input'].includes(String(goal.status))
  ) {
    return false
  }
  const revision = typeof goal.checkpoint_revision === 'number' ? goal.checkpoint_revision : 0
  const previous = $goalsBySession.get()[sid]
  if (previous?.goalId === goal.goal_id && (previous.revision ?? 0) > revision) {
    return true
  }
  if (
    previous?.goalId === goal.goal_id &&
    (previous.revision ?? 0) === revision &&
    ['paused', 'blocked', 'awaiting_input', 'done'].includes(previous.status) &&
    goal.status === 'active'
  ) {
    return true
  }
  setSessionGoal(sid, {
    autoEligible: goal.auto_eligible === true,
    goalId: goal.goal_id,
    revision,
    title: goal.title,
    status: goal.status as GoalStatus,
    detail: typeof goal.reason === 'string' ? goal.reason : undefined,
    nextAction: typeof goal.next_action === 'string' ? goal.next_action : undefined,
    turnsUsed: typeof goal.turns_used === 'number' ? goal.turns_used : undefined,
    maxTurns: typeof goal.max_turns === 'number' ? goal.max_turns : undefined,
    updatedAt: Date.now()
  })

  return true
}

export async function refreshSessionGoal(sid: string): Promise<void> {
  const gateway = $gateway.get()

  if (!sid || !gateway || isSessionGone(sid)) {
    return
  }

  const previous = $goalsBySession.get()[sid]
  try {
    const result = await gateway.request<{ output?: string; goal?: unknown }>('slash.exec', {
      command: 'goal status',
      session_id: sid
    })

    if ($goalsBySession.get()[sid] !== previous) {
      return
    }
    if ((result?.goal as { status?: string } | undefined)?.status === 'done') {
      clearSessionGoal(sid)
      return
    }
    if (!applyGoalProjection(sid, result?.goal)) {
      applyGoalStatusText(sid, result?.output ?? '', { hydrate: true })
    }
  } catch (error) {
    if (isSessionGoneError(error)) {
      markSessionGone(sid)
    }

    // Best-effort: older gateways or detached sessions simply won't hydrate it.
  }
}

export async function controlSessionGoal(sid: string, action: 'pause' | 'resume' | 'clear'): Promise<void> {
  const gateway = $gateway.get()
  if (!gateway) {
    return
  }
  const result = await gateway.request<{ goal?: unknown; type?: string; goal_continuation?: unknown }>(
    'command.dispatch',
    { session_id: sid, name: 'goal', arg: action }
  )
  applyGoalProjection(sid, result.goal)
  if (result.type === 'send' && result.goal_continuation) {
    await gateway.request('prompt.submit', { session_id: sid, goal_continuation: result.goal_continuation })
  }
}
