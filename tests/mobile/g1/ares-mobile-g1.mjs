#!/usr/bin/env node
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'

const REQUIRED_ENVIRONMENT = ['ARES_G1_HOST_ID', 'ARES_G1_SESSION_ID', 'ARES_G1_WS_URL']
const DEFAULT_TIMEOUT_MS = 10_000

function now() {
  return new Date().toISOString()
}

function parseArgs(argv) {
  const args = { receipt: '', timeoutMs: DEFAULT_TIMEOUT_MS }
  for (let index = 0; index < argv.length; index += 1) {
    const current = argv[index]
    if (current === '--receipt') {
      args.receipt = String(argv[++index] ?? '')
    } else if (current === '--timeout-ms') {
      args.timeoutMs = Number(argv[++index])
    } else {
      throw new Error(`unsupported argument: ${current}`)
    }
  }
  if (!args.receipt) {
    throw new Error('--receipt is required')
  }
  if (!Number.isFinite(args.timeoutMs) || args.timeoutMs <= 0) {
    throw new Error('--timeout-ms must be a positive number')
  }
  return args
}

function redactedEndpoint(value) {
  const url = new URL(value)
  if (url.protocol !== 'ws:' && url.protocol !== 'wss:') {
    throw new Error('ARES_G1_WS_URL must use ws:// or wss://')
  }
  return { origin: url.origin, path: url.pathname }
}

function baseReceipt(state, fields = {}) {
  return {
    schema: 'AresMobileG1ReceiptV1',
    started_at: now(),
    completed_at: now(),
    state,
    scope: 'reference_preflight_only',
    g1_phase_claim: false,
    ...fields,
  }
}

async function writeReceipt(receiptPath, receipt) {
  const target = path.resolve(receiptPath)
  await mkdir(path.dirname(target), { recursive: true })
  await writeFile(target, `${JSON.stringify(receipt, null, 2)}\n`, 'utf8')
}

function waitForSocketOpen(socket, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('socket_open_timeout')), timeoutMs)
    socket.addEventListener('open', () => {
      clearTimeout(timer)
      resolve()
    }, { once: true })
    socket.addEventListener('error', () => {
      clearTimeout(timer)
      reject(new Error('socket_open_error'))
    }, { once: true })
  })
}

function parseFrame(data) {
  try {
    return JSON.parse(typeof data === 'string' ? data : String(data))
  } catch {
    return null
  }
}

function createRpcClient(socket, timeoutMs) {
  let nextId = 0
  const pending = new Map()
  let readyResolve
  const ready = new Promise(resolve => {
    readyResolve = resolve
  })
  socket.addEventListener('message', event => {
    const frame = parseFrame(event.data)
    if (!frame) return
    if (frame.id !== undefined && frame.id !== null) {
      const entry = pending.get(String(frame.id))
      if (!entry) return
      pending.delete(String(frame.id))
      clearTimeout(entry.timer)
      if (frame.error) entry.reject(new Error(`rpc_${frame.error.code ?? 'error'}`))
      else entry.resolve(frame.result)
      return
    }
    if (frame.method === 'event' && frame.params?.type === 'gateway.ready') {
      readyResolve(frame.params.payload ?? {})
    }
  })
  return {
    ready: () => Promise.race([
      ready,
      new Promise((_, reject) => setTimeout(() => reject(new Error('gateway_ready_timeout')), timeoutMs)),
    ]),
    request(method, params) {
      const id = `g1-${++nextId}`
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          pending.delete(id)
          reject(new Error('rpc_timeout'))
        }, timeoutMs)
        pending.set(id, { resolve, reject, timer })
        socket.send(JSON.stringify({ jsonrpc: '2.0', id, method, params }))
      })
    },
  }
}

export async function runReferencePreflight({ environment = process.env, timeoutMs = DEFAULT_TIMEOUT_MS }) {
  const missing = REQUIRED_ENVIRONMENT.filter(name => !String(environment[name] ?? '').trim())
  if (missing.length > 0) {
    return { exitCode: 2, receipt: baseReceipt('blocked_environment', { missing_environment: missing }) }
  }

  let endpoint
  try {
    endpoint = redactedEndpoint(String(environment.ARES_G1_WS_URL))
  } catch {
    return {
      exitCode: 2,
      receipt: baseReceipt('blocked_environment', { missing_environment: ['ARES_G1_WS_URL_VALID'] }),
    }
  }

  const profile = String(environment.ARES_G1_PROFILE ?? '').trim() || 'default'
  const hostId = String(environment.ARES_G1_HOST_ID)
  const sessionId = String(environment.ARES_G1_SESSION_ID)
  let socket
  try {
    socket = new WebSocket(String(environment.ARES_G1_WS_URL))
    await waitForSocketOpen(socket, timeoutMs)
    const rpc = createRpcClient(socket, timeoutMs)
    const ready = await rpc.ready()
    const capabilities = ready.capabilities ?? {}
    if (ready.protocol_revision !== 2 || capabilities.session_snapshot_v1 !== true || capabilities.session_observe_v1 !== true) {
      return {
        exitCode: 3,
        receipt: baseReceipt('blocked_protocol', {
          endpoint,
          protocol_revision: ready.protocol_revision ?? null,
          advertised_capabilities: {
            session_observe_v1: capabilities.session_observe_v1 === true,
            session_snapshot_v1: capabilities.session_snapshot_v1 === true,
          },
          reason: 'required_read_capabilities_unavailable',
        }),
      }
    }
    const snapshotResult = await rpc.request('session.snapshot', {
      host_id: hostId,
      profile,
      session_id: sessionId,
    })
    const snapshot = snapshotResult?.snapshot
    if (!snapshot || typeof snapshot.revision !== 'number' || snapshot.scope?.runtime_id !== ready.runtime_id) {
      return {
        exitCode: 3,
        receipt: baseReceipt('blocked_protocol', {
          endpoint,
          reason: 'snapshot_contract_mismatch',
        }),
      }
    }
    const observeResult = await rpc.request('session.observe', {
      host_id: hostId,
      profile,
      session_id: sessionId,
      runtime_id: ready.runtime_id,
      last_seen_revision: snapshot.revision,
    })
    if (observeResult?.observing !== true || observeResult?.snapshot_required === true) {
      return {
        exitCode: 3,
        receipt: baseReceipt('blocked_protocol', {
          endpoint,
          reason: 'observe_contract_not_admitted',
        }),
      }
    }
    return {
      exitCode: 0,
      receipt: baseReceipt('passed', {
        endpoint,
        host_id: hostId,
        profile,
        session_id: sessionId,
        protocol_revision: ready.protocol_revision,
        runtime_id: ready.runtime_id,
        snapshot_revision: snapshot.revision,
        observe_revision: observeResult.current_revision,
        advertised_capabilities: {
          session_observe_v1: true,
          session_snapshot_v1: true,
        },
      }),
    }
  } catch (error) {
    return {
      exitCode: 1,
      receipt: baseReceipt('failed', {
        endpoint,
        error_kind: error instanceof Error ? error.message : 'unknown_error',
      }),
    }
  } finally {
    try {
      socket?.close()
    } catch {
      // Best-effort close only; receipt state comes from the completed preflight.
    }
  }
}

async function main() {
  let args
  try {
    args = parseArgs(process.argv.slice(2))
  } catch (error) {
    process.stderr.write(`${error instanceof Error ? error.message : 'invalid arguments'}\n`)
    return 64
  }
  const { exitCode, receipt } = await runReferencePreflight({ timeoutMs: args.timeoutMs })
  await writeReceipt(args.receipt, receipt)
  process.stdout.write(`${JSON.stringify({ state: receipt.state, receipt: path.resolve(args.receipt) })}\n`)
  return exitCode
}

if (import.meta.url === new URL(process.argv[1], 'file:').href) {
  process.exitCode = await main()
}
