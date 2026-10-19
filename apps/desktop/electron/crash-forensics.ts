/**
 * Last-chance forensics for the Electron main process.
 *
 * Electron installs its own `uncaughtException` listener and only warns on
 * unhandled rejections, so the app usually survives — but the reason lands on
 * stderr alone, which is discarded entirely when the app is launched from
 * Finder or the Start menu. Without a record in desktop.log, a main-process
 * fault is invisible in a `hermes debug share` bundle and the user is left
 * describing symptoms instead of showing a stack.
 */

export interface CrashForensicsTarget {
  on: (event: 'uncaughtException' | 'unhandledRejection', listener: (value: unknown) => void) => unknown
}

export interface CrashForensicsOptions {
  flush: () => void
  log: (message: string) => void
  target?: CrashForensicsTarget
}

/** Render a thrown value for the log, preferring a stack over a bare message. */
export function describeCrashReason(reason: unknown): string {
  if (reason instanceof Error) {
    return reason.stack || reason.message || reason.name || 'Error'
  }

  if (typeof reason === 'string') {
    return reason
  }

  try {
    return JSON.stringify(reason) ?? String(reason)
  } catch {
    return String(reason)
  }
}

/**
 * Record main-process faults to desktop.log and flush synchronously, since a
 * fault that does prove fatal leaves no chance for the batched async flush.
 *
 * EPIPE on stdout/stdin is special: it means the pipe reader (usually the
 * process that launched the app) is gone. Both `log` and `flush` themselves
 * write through that dead pipe, so recording the fault throws inside the
 * uncaughtException handler — one logged fault becomes 353 suppressed
 * recursions (#observed 2026-10-19). Count them, surface a rate-limited
 * summary once a minute, and keep the app alive.
 */
export function installCrashForensics({ flush, log, target = process }: CrashForensicsOptions): void {
  let epipeSuppressed = 0
  let epipeLastLoggedAt = 0
  const record = (label: string) => (reason: unknown) => {
    const code = (reason as { code?: unknown } | null | undefined)?.code
    if (code === 'EPIPE') {
      epipeSuppressed += 1
      const now = Date.now()
      if (now - epipeLastLoggedAt > 60_000) {
        epipeLastLoggedAt = now
        try {
          log(`[main] EPIPE on stdio suppressed x${epipeSuppressed} (pipe reader gone; app continuing)`)
          flush()
        } catch {
          // The log channel itself is the dead pipe. Nothing left to do.
        }
      }
      return
    }
    log(`[main] ${label}: ${describeCrashReason(reason)}`)
    flush()
  }

  target.on('uncaughtException', record('Uncaught exception'))
  target.on('unhandledRejection', record('Unhandled rejection'))
}
