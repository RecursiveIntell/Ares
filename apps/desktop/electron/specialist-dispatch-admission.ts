import { canAdmitLocalBackend, type PoolEvictionEntry } from './pool-eviction'

export const SPECIALIST_POOL_PREFIX = 'specialist-run:'

export interface SpecialistDispatchAdmissionRequest {
  requestDigest: string
  runId: string
  profileIds: string[]
  /** Canonical runner input; never interpreted as a command by Electron. */
  runnerInput?: string
}

export interface SpecialistDispatchAdmissionResult {
  outcome: 'admitted' | 'rejected'
  reasonCode: 'ADMITTED' | 'IDEMPOTENCY_CONFLICT' | 'INVALID_REQUEST' | 'POOL_CAPACITY_EXCEEDED' | 'RUNNER_START_FAILED' | 'RUNNER_START_CANCELLED'
  maxCapacity?: number
  reservedCapacity?: number
  runId: string
}

export interface SpecialistDispatchAdmissionDeps {
  maxCapacity: number
  pool: Map<string, PoolEvictionEntry>
  /** Check maySpawn immediately before the fixed synchronous spawn. */
  spawnRunner: (request: SpecialistDispatchAdmissionRequest, maySpawn: () => boolean) => Promise<unknown>
  /** Retain the exact child until exit is confirmed; reject uncertain cleanup. */
  stopRunner: (child: unknown) => Promise<void>
}

const DIGEST = /^sha256:[0-9a-f]{64}$/
const PROFILE = /^[a-z0-9][a-z0-9_-]{0,63}$/
const RUN_ID = /^specialist-run-[a-z0-9][a-z0-9-]{7,63}$/

/** Runtime resolution may await recovery; the final launch fence may not. */
export async function startSpecialistRunner<Runtime>(
  request: SpecialistDispatchAdmissionRequest,
  maySpawn: () => boolean,
  deps: { resolveRuntime: () => Promise<Runtime>; spawn: (runtime: Runtime, request: SpecialistDispatchAdmissionRequest) => unknown }
): Promise<unknown> {
  const runtime = await deps.resolveRuntime()
  if (!maySpawn()) {
    throw new Error('specialist runner startup was revoked')
  }
  return deps.spawn(runtime, request)
}

function specialistRunnerExited(child: unknown): boolean {
  const stopped = child as { exitCode?: number | null; signalCode?: string | null } | null
  return Boolean(stopped && (Number.isInteger(stopped.exitCode) ||
    typeof stopped.signalCode === 'string' && stopped.signalCode.length > 0))
}

/** A bounded best-effort wait is insufficient evidence of child exit. */
export async function stopSpecialistRunner(
  child: unknown,
  deps: { stopChild: (child: unknown) => void; waitForExit: (child: unknown) => Promise<void> }
): Promise<void> {
  deps.stopChild(child)
  await deps.waitForExit(child)
  if (!specialistRunnerExited(child)) {
    throw new Error('specialist runner exit was not confirmed')
  }
}

function valid(request: SpecialistDispatchAdmissionRequest): boolean {
  return Boolean(
    request &&
      typeof request.runId === 'string' && RUN_ID.test(request.runId) &&
      typeof request.requestDigest === 'string' && DIGEST.test(request.requestDigest) &&
      Array.isArray(request.profileIds) &&
      request.profileIds.length >= 1 &&
      request.profileIds.length <= 4 &&
      request.profileIds.every(profile => typeof profile === 'string' && PROFILE.test(profile)) &&
      request.profileIds.every((profile, index, profiles) => index === 0 || profiles[index - 1] < profile)
  )
}

/**
 * Electron-only weighted pool admission. The caller supplies no command,
 * capability assertion, token, connection URL, or capacity value; the fixed
 * Electron spawn path receives only an admitted request identity.
 */
export function createSpecialistDispatchAdmission(deps: SpecialistDispatchAdmissionDeps) {
  const starts = new Map<string, {
    digest: string
    result: Promise<SpecialistDispatchAdmissionResult>
    entry?: PoolEvictionEntry
    cancelRequested?: boolean
    cancelTerminal?: 'released' | 'runner_failed'
    cancellation?: Promise<void>
  }>()
  const terminals = new Map<string, 'released' | 'runner_failed' | 'cleanup_failed'>()

  function poolKey(runId: string): string {
    return `${SPECIALIST_POOL_PREFIX}${runId}`
  }

  function release(runId: string, terminal: 'released' | 'runner_failed' | 'cleanup_failed'): void {
    const record = starts.get(runId)
    if (!record) {
      return
    }
    // Cancellation owns reconciliation until its exact handle has exited.
    // An exit/error callback cannot free its capacity or erase uncertainty.
    if (record.cancelRequested) {
      if (terminals.get(runId) !== 'cleanup_failed' || !specialistRunnerExited(record.entry?.process)) {
        return
      }
      // A later real exit resolves cleanup uncertainty without retrying work.
      terminal = record.cancelTerminal!
    }
    const key = poolKey(runId)
    if (record.entry && deps.pool.get(key) === record.entry) {
      deps.pool.delete(key)
    }
    terminals.set(runId, terminal)
  }

  function cancel(runId: string, terminal: 'released' | 'runner_failed' = 'released'): Promise<void> {
    const record = starts.get(runId)
    if (!record || terminals.has(runId)) {
      return Promise.reject(new Error('specialist run is not active'))
    }
    if (record.cancellation) {
      return record.cancellation
    }
    // Revoke synchronously, before any awaited runtime lookup can resume.
    record.cancelRequested = true
    record.cancelTerminal = terminal
    record.cancellation = (async () => {
      await record.result
      try {
        if (record.entry?.process) {
          await deps.stopRunner(record.entry.process)
        }
        const key = poolKey(runId)
        if (record.entry && deps.pool.get(key) === record.entry) {
          deps.pool.delete(key)
        }
        terminals.set(runId, terminal)
      } catch (error) {
        // Keep the handle and weighted reservation: no released acknowledgment
        // and no new work may overlap an uncertain live child.
        terminals.set(runId, 'cleanup_failed')
        throw error
      }
    })()
    return record.cancellation
  }

  return {
    async admit(request: SpecialistDispatchAdmissionRequest): Promise<SpecialistDispatchAdmissionResult> {
      if (!valid(request)) {
        return { outcome: 'rejected', reasonCode: 'INVALID_REQUEST', runId: String(request?.runId || '') }
      }
      const prior = starts.get(request.runId)
      if (prior) {
        if (prior.digest !== request.requestDigest) {
          return { outcome: 'rejected', reasonCode: 'IDEMPOTENCY_CONFLICT', runId: request.runId }
        }
        return prior.result
      }
      const reservedCapacity = request.profileIds.length
      const result = Promise.resolve().then(async (): Promise<SpecialistDispatchAdmissionResult> => {
        const record = starts.get(request.runId)!
        if (record.cancelRequested) {
          return { outcome: 'rejected', reasonCode: 'RUNNER_START_CANCELLED', runId: request.runId }
        }
        if (!canAdmitLocalBackend(deps.pool.entries(), deps.maxCapacity, reservedCapacity)) {
          return {
            outcome: 'rejected',
            reasonCode: 'POOL_CAPACITY_EXCEEDED',
            maxCapacity: deps.maxCapacity,
            runId: request.runId
          }
        }
        const entry: PoolEvictionEntry = {
          process: null,
          countsTowardPoolCap: true,
          capacityUnits: reservedCapacity,
          lastActiveAt: Date.now()
        }
        record.entry = entry
        deps.pool.set(poolKey(request.runId), entry)
        try {
          entry.process = await deps.spawnRunner(request, () =>
            !record.cancelRequested && !terminals.has(request.runId) && deps.pool.get(poolKey(request.runId)) === entry
          )
          if (record.cancelRequested) {
            return { outcome: 'rejected', reasonCode: 'RUNNER_START_CANCELLED', runId: request.runId }
          }
          if (terminals.has(request.runId)) {
            return { outcome: 'rejected', reasonCode: 'RUNNER_START_FAILED', runId: request.runId }
          }
          return { outcome: 'admitted', reasonCode: 'ADMITTED', reservedCapacity, runId: request.runId }
        } catch {
          if (record.cancelRequested) {
            return { outcome: 'rejected', reasonCode: 'RUNNER_START_CANCELLED', runId: request.runId }
          }
          if (deps.pool.get(poolKey(request.runId)) === entry) {
            deps.pool.delete(poolKey(request.runId))
          }
          starts.delete(request.runId)
          return { outcome: 'rejected', reasonCode: 'RUNNER_START_FAILED', runId: request.runId }
        }
      })
      starts.set(request.runId, { digest: request.requestDigest, result })
      return result
    },
    hasActive(): boolean {
      return [...starts.entries()].some(([runId, record]) =>
        record.cancelRequested && !terminals.has(runId) ||
        record.entry !== undefined && (!terminals.has(runId) || terminals.get(runId) === 'cleanup_failed')
      )
    },
    cancel,
    poolKey,
    release,
    status(runId: string): 'running' | 'released' | 'runner_failed' | 'cleanup_failed' | 'unknown' {
      return terminals.get(runId) || (starts.has(runId) ? 'running' : 'unknown')
    }
  }
}
