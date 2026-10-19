/**
 * Process-lifetime cache for the app-document URL, with a cheap freshness guard.
 *
 * WHY THIS EXISTS (measured on release f1fd00f2):
 *
 * `appDocumentUrl()` used to be `pathToFileURL(resolveRendererIndex()).toString()`.
 * `resolveRendererIndex()` is NOT a path lookup — it walks the renderer bundle
 * (`missingRendererAssets`: reads + regex-scans every chunk to detect a torn
 * generation). On that build: 142 JS files / 28.8 MB / ~186 ms of synchronous
 * CPU per call.
 *
 * That URL is consumed by the *admission* path, which runs far from boot:
 *   - `NativeSocketTransport.admit()` → `trusted()` + `approvedFrame()`
 *     (approvedFrame called it TWICE)
 *   - admit runs on every incoming gateway frame (`wire.on('message')`),
 *     every `ack()`, every `send()`, and every 250 ms transport watchdog tick.
 *
 * Result: ~3 bundle walks (~366 ms) per admission, several times a second, on
 * the Electron MAIN thread — measured 86–100% CPU (one core pinned), rchar
 * 224 MB/s, 930 opens/s, while the app sat idle. Every gateway frame queued
 * behind that, so transcript hydrates (`session.resume`) blew the renderer's
 * 30 s RPC timeout, the readiness probe timed out ("Runtime not ready"), and
 * the transport's own congestion watchdog kept closing/reopening the socket.
 *
 * The URL is an identity and its path is stable for the process lifetime, so
 * resolving it once is correct. Freshness is preserved without walking the
 * bundle: each call re-stats the already-chosen index (microseconds) and only
 * re-resolves when that file's mtime/size actually changed — i.e. after a
 * repair or an update rewrote the bundle. A stat that cannot be read keeps the
 * cached value rather than thrashing the expensive path.
 */

export interface AppDocumentUrlStat {
  mtimeMs: number
  size: number
}

export interface AppDocumentUrlDeps {
  /** Dev-server URL wins outright; no bundle walk is needed. */
  devServer?: string
  /** Resolves the chosen renderer index path (expensive — walks the bundle). */
  resolveIndex?: () => string
  /** pathToFileURL(...).toString() in production; injectable for tests. */
  toUrl?: (indexPath: string) => string
  /**
   * Cheap freshness probe for an already-resolved index. Omit to cache for the
   * whole process; throw/null is treated as "unchanged" (never re-walk).
   */
  stat?: (indexPath: string) => AppDocumentUrlStat
}

interface CachedAppDocumentUrl {
  url: string
  indexPath: string
  mtimeMs: number
  size: number
}

export class AppDocumentUrl {
  private cached: CachedAppDocumentUrl | null = null

  constructor(private deps: AppDocumentUrlDeps = {}) {}

  /** Resolve the app-document URL, re-walking the bundle only when it changed. */
  resolve(): string {
    const { devServer, resolveIndex, toUrl, stat } = this.deps

    if (devServer) {
      return devServer
    }

    if (this.cached && this.unchanged(this.cached)) {
      return this.cached.url
    }

    if (!resolveIndex || !toUrl) {
      throw new Error('AppDocumentUrl requires resolveIndex and toUrl')
    }

    const indexPath = resolveIndex()
    const stats = stat ? safeStat(stat, indexPath) : null

    this.cached = {
      url: toUrl(indexPath),
      indexPath,
      mtimeMs: stats?.mtimeMs ?? 0,
      size: stats?.size ?? 0
    }

    return this.cached.url
  }

  /** Drop the cache (bundle repair/replace). Next resolve() walks again. */
  invalidate(): void {
    this.cached = null
  }

  private unchanged(cached: CachedAppDocumentUrl): boolean {
    if (!this.deps.stat) {
      return true
    }

    const stats = safeStat(this.deps.stat, cached.indexPath)

    // Unreadable stats are not evidence of change: keep the cached identity
    // rather than falling back to a per-call bundle walk.
    if (stats === null) {
      return true
    }

    return stats.mtimeMs === cached.mtimeMs && stats.size === cached.size
  }
}

function safeStat(
  stat: (indexPath: string) => AppDocumentUrlStat,
  indexPath: string
): AppDocumentUrlStat | null {
  try {
    return stat(indexPath) ?? null
  } catch {
    return null
  }
}
