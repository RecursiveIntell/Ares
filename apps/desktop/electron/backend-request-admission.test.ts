import { EventEmitter } from 'node:events'

import { expect, test } from 'vitest'

import { privilegedRequestHeaders, registerOwnedBackendAdmission } from './backend-request-admission'

class Child extends EventEmitter {
  exitCode: number | null = null
  killed = false
}
const frame = {}
const app = { mainFrame: frame }
const url = 'ws://127.0.0.1:9119/api/ws?token=synthetic'

function fixture() {
  const admissions = new Map()
  const child = new Child()
  registerOwnedBackendAdmission(admissions, 'http://127.0.0.1:9119', child, 'owned')

  return {
    admissions,
    child,
    deps: {
      admissions,
      trusted: contents => contents === app,
      remembered: (_url: string) => false,
      headers: () => ({ 'X-Synthetic-Proxy': 'approved' })
    }
  }
}

test('old exit cannot revoke replacement generation at the same origin', () => {
  const { admissions, child } = fixture()
  const newer = new Child()
  const replacement = registerOwnedBackendAdmission(admissions, 'http://127.0.0.1:9119', newer, 'replacement')
  child.exitCode = 0
  child.emit('exit', 0)
  expect(admissions.get('http://127.0.0.1:9119')).toBe(replacement)
  newer.exitCode = 0
  newer.emit('exit', 0)
  expect(admissions.size).toBe(0)
})

test('alive closure belongs to its captured child, not a mutable current pointer', () => {
  const { admissions, child } = fixture()
  const entry = admissions.get('http://127.0.0.1:9119')
  child.killed = true
  expect(entry.alive()).toBe(false)
  expect(() => registerOwnedBackendAdmission(admissions, 'http://127.0.0.1:9119', child, 'dead')).toThrow()
})

test('renderer main-frame sockets receive no native Origin or headers', () => {
  const { deps } = fixture()

  const result = privilegedRequestHeaders(
    { url, frame, resourceType: 'webSocket', requestHeaders: { Origin: 'null' } },
    app,
    deps
  )

  expect(result).toEqual({})
  expect(result.requestHeaders?.['X-Hermes-Native-Bootstrap']).toBeUndefined()
})

test.each(['guest', 'subframe', 'unknown-frame', 'dead', 'unadmitted', 'malformed'])(
  'no native Origin or proxy credential for %s',
  kind => {
    const { deps, child } = fixture()

    if (kind === 'dead') {
      child.exitCode = 0
    }

    const request = {
      url: kind === 'unadmitted' ? 'ws://127.0.0.1:9220/api/ws' : kind === 'malformed' ? 'invalid' : url,
      frame: kind === 'subframe' ? {} : kind === 'unknown-frame' ? null : frame,
      resourceType: 'webSocket',
      requestHeaders: { Origin: 'http://hostile.test' }
    }

    const contents = kind === 'guest' ? { mainFrame: frame } : app
    expect(privilegedRequestHeaders(request, contents, deps)).toEqual({})
  }
)

test('remembered remote/SSH URL never authorizes renderer native Origin or headers', () => {
  const { deps } = fixture()
  const remote = 'wss://gateway.example/api/ws?ticket=synthetic'
  deps.remembered = candidate => candidate === remote
  expect(privilegedRequestHeaders({ url: remote, frame, resourceType: 'webSocket' }, app, deps)).toEqual({})
  expect(privilegedRequestHeaders({ url: remote + '-foreign', frame, resourceType: 'webSocket' }, app, deps)).toEqual(
    {}
  )
})
