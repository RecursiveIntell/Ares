import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  createSpecialistDispatchAdmission,
  startSpecialistRunner,
  stopSpecialistRunner
} from './specialist-dispatch-admission'

const request = (suffix = '1', profiles = ['explorer']) => ({
  requestDigest: `sha256:${suffix.repeat(64)}`,
  runId: `specialist-run-0000000${suffix}`,
  profileIds: profiles
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((yes, no) => {
    resolve = yes
    reject = no
  })

  return { promise, resolve, reject }
}

test('pending runtime cancellation fences launch before acknowledgment', async () => {
  const runtime = deferred<void>()
  const entered = deferred<void>()
  const pool = new Map()
  let launches = 0

  const admission = createSpecialistDispatchAdmission({
    maxCapacity: 4,
    pool,
    spawnRunner: (request, maySpawn) =>
      startSpecialistRunner(request, maySpawn, {
        resolveRuntime: () => {
          entered.resolve()

          return runtime.promise
        },
        spawn: () => {
          launches++

          return { pid: 17 }
        }
      }),
    stopRunner: async () => {
      throw new Error('there is no child to stop')
    }
  })

  const start = admission.admit(request())
  await entered.promise
  const cancelled = admission.cancel(request().runId)
  assert.equal(admission.hasActive(), true)
  runtime.resolve()
  await cancelled
  assert.equal(launches, 0)
  assert.equal(pool.size, 0)
  assert.equal(admission.status(request().runId), 'released')
  assert.equal((await start).reasonCode, 'RUNNER_START_CANCELLED')
  assert.deepEqual(await admission.admit(request()), await start)
})

test('a handle returned after cancel remains owned until confirmed cleanup', async () => {
  const returned = deferred<object>()
  const entered = deferred<void>()
  const stopped = deferred<void>()
  const cleanup = deferred<void>()
  const child = { pid: 18, exitCode: null as number | null, signalCode: null }
  const pool = new Map()

  const admission = createSpecialistDispatchAdmission({
    maxCapacity: 1,
    pool,
    spawnRunner: async () => {
      entered.resolve()

      return returned.promise
    },
    stopRunner: actual =>
      stopSpecialistRunner(actual, {
        stopChild: owned => {
          assert.equal(owned, child)
          stopped.resolve()
        },
        waitForExit: async () => {
          await cleanup.promise
          child.exitCode = 0
        }
      })
  })

  const start = admission.admit(request())
  await entered.promise
  const firstCancel = admission.cancel(request().runId)
  const duplicateCancel = admission.cancel(request().runId)
  returned.resolve(child)
  await stopped.promise
  assert.equal(admission.hasActive(), true)
  assert.equal(pool.get(admission.poolKey(request().runId))?.process, child)
  // An exit event during stop cannot prematurely free the reservation.
  admission.release(request().runId, 'released')
  assert.equal(admission.hasActive(), true)
  assert.equal((await admission.admit(request('2'))).reasonCode, 'POOL_CAPACITY_EXCEEDED')
  cleanup.resolve()
  await Promise.all([firstCancel, duplicateCancel])
  assert.equal(pool.size, 0)
  assert.equal(admission.status(request().runId), 'released')
  assert.equal((await start).reasonCode, 'RUNNER_START_CANCELLED')
})

test('cleanup rejection retains child and capacity and reports uncertainty', async () => {
  const child = { pid: 19, exitCode: null as number | null, signalCode: null }
  const pool = new Map()
  const admission = createSpecialistDispatchAdmission({
    maxCapacity: 1,
    pool,
    spawnRunner: async () => child,
    stopRunner: actual => stopSpecialistRunner(actual, { stopChild: () => {}, waitForExit: async () => {} })
  })
  await admission.admit(request())
  await assert.rejects(admission.cancel(request().runId), /exit was not confirmed/)
  assert.equal(admission.status(request().runId), 'cleanup_failed')
  assert.equal(admission.hasActive(), true)
  assert.equal(pool.get(admission.poolKey(request().runId))?.process, child)
  admission.release(request().runId, 'released')
  assert.equal(admission.status(request().runId), 'cleanup_failed')
  assert.equal((await admission.admit(request('2'))).reasonCode, 'POOL_CAPACITY_EXCEEDED')
  // A later observed exit can settle custody; no new model work is retried.
  child.exitCode = 0
  admission.release(request().runId, 'runner_failed')
  assert.equal(admission.status(request().runId), 'released')
  assert.equal(admission.hasActive(), false)
  assert.equal(pool.size, 0)
})

test('deadline stop preserves failure outcome after confirmed exit', async () => {
  const child = { exitCode: null as number | null, signalCode: null as string | null }
  const admission = createSpecialistDispatchAdmission({
    maxCapacity: 1,
    pool: new Map(),
    spawnRunner: async () => child,
    stopRunner: actual =>
      stopSpecialistRunner(actual, {
        stopChild: () => {},
        waitForExit: async () => {
          child.signalCode = 'SIGTERM'
        }
      })
  })
  await admission.admit(request())
  await admission.cancel(request().runId, 'runner_failed')
  assert.equal(admission.status(request().runId), 'runner_failed')
  assert.equal(admission.hasActive(), false)
})

test('ordinary completion and duplicate admission retain existing semantics', async () => {
  const pool = new Map()
  let launches = 0

  const admission = createSpecialistDispatchAdmission({
    maxCapacity: 4,
    pool,
    spawnRunner: async (_request, maySpawn) => {
      assert.equal(maySpawn(), true)
      launches++

      return { pid: 20 }
    },
    stopRunner: async () => {}
  })

  const first = admission.admit(request())
  const duplicate = admission.admit(request())
  assert.deepEqual(await first, await duplicate)
  assert.equal(launches, 1)
  assert.equal(
    (await admission.admit({ ...request(), requestDigest: `sha256:${'b'.repeat(64)}` })).reasonCode,
    'IDEMPOTENCY_CONFLICT'
  )
  admission.release(request().runId, 'released')
  assert.equal(admission.hasActive(), false)
  assert.equal((await admission.admit(request('2', ['explorer', 'public']))).reservedCapacity, 2)
})

test('failed uncommitted startup frees capacity while invalid requests have no effect', async () => {
  let launches = 0
  const pool = new Map()
  const admission = createSpecialistDispatchAdmission({
    maxCapacity: 1,
    pool,
    spawnRunner: async () => {
      launches++
      throw new Error('no child')
    },
    stopRunner: async () => {}
  })
  assert.equal((await admission.admit(request('1', []))).reasonCode, 'INVALID_REQUEST')
  assert.equal(launches, 0)
  assert.equal((await admission.admit(request())).reasonCode, 'RUNNER_START_FAILED')
  assert.equal(pool.size, 0)
  assert.equal(admission.hasActive(), false)
  assert.equal((await admission.admit(request('2'))).reasonCode, 'RUNNER_START_FAILED')
  assert.equal(launches, 2)
})
