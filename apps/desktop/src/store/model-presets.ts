import { atom } from 'nanostores'

import { reconcileSelectorInfo } from '@/lib/selector-observation'
import { persistString, storedString } from '@/lib/storage'
import type { SessionRuntimeInfo } from '@/types/hermes'

import { activeGatewayConnectionId } from './gateway'
import { notifyError } from './notifications'
import { $activeGatewayProfile } from './profile'
import { clearRuntimeOptionUncertainty, reconcileRuntimeOptionFailure } from './runtime-option-recovery'
import {
  $activeSessionId,
  $currentFastMode,
  $currentReasoningEffort,
  $selectedStoredSessionId,
  beginRuntimeOptionIntent,
  ownsRuntimeOptionIntent,
  setCurrentFastMode,
  setCurrentReasoningEffort
} from './session'
import { $sessionStates, knownOwnerForSession, sessionTileDelegate } from './session-states'

const STORAGE_KEY = 'hermes.desktop.model-presets'

/** Per-model reasoning/fast preset, remembered globally across sessions and
 *  re-applied to the session whenever that model is selected. Unset dimensions
 *  fall back to the Hermes default (medium effort, no fast). */
export interface ModelPreset {
  effort?: string
  fast?: boolean
}

type RequestGateway = <T>(method: string, params?: Record<string, unknown>) => Promise<T>

/** Stable `provider::model` key (matches the visibility-store format). */
export const modelPresetKey = (provider: string, model: string): string => `${provider}::${model}`

function load(): Record<string, ModelPreset> {
  const raw = storedString(STORAGE_KEY)

  if (!raw) {
    return {}
  }

  try {
    const parsed = JSON.parse(raw)

    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? (parsed as Record<string, ModelPreset>) : {}
  } catch {
    return {}
  }
}

export const $modelPresets = atom<Record<string, ModelPreset>>(load())

export function getModelPreset(provider: string, model: string): ModelPreset {
  return $modelPresets.get()[modelPresetKey(provider, model)] ?? {}
}

/** Merge a partial preset for one model and persist. */
export function setModelPreset(provider: string, model: string, patch: ModelPreset): void {
  const key = modelPresetKey(provider, model)
  const next = { ...$modelPresets.get(), [key]: { ...$modelPresets.get()[key], ...patch } }

  $modelPresets.set(next)
  persistString(STORAGE_KEY, JSON.stringify(next))
}

/** Only a backend that predates the combined `runtime_options` key answers this
 *  way, and only before it has applied anything (config.set rejects the key up
 *  front). Any other failure keeps the single-write semantics below. */
function isLegacyRuntimeOptionsBackend(error: unknown): boolean {
  const code = error && typeof error === 'object' && 'code' in error ? error.code : undefined
  const message = error instanceof Error ? error.message : ''

  return code === 4002 && /unknown config key:\s*runtime_options/i.test(message)
}

/** Apply a model's preset to the composer, then push it to a live session.
 *  `undefined` skips that dimension; values are capability-gated upstream.
 *  Without a session the local draft still needs the preset, but must not call
 *  `config.set`: that falls back to persistent profile config when no session
 *  matches and would rewrite the user's defaults.
 *
 *  `primary: false` scopes the optimistic write to the tile's session slice —
 *  a tile's picker must not clobber the primary composer's effort/fast. */
export async function applyModelPreset(
  { effort, fast }: ModelPreset,
  ctx: { failMessage: string; primary?: boolean; request: RequestGateway; sessionId: null | string }
): Promise<void> {
  if (effort === undefined && fast === undefined) {
    return
  }

  if (!ctx.sessionId) {
    if (effort !== undefined) {
      setCurrentReasoningEffort(effort)
    }

    if (fast !== undefined) {
      setCurrentFastMode(fast)
    }

    return
  }

  const runtimeId = ctx.sessionId
  const primary = (ctx.primary ?? true) && $activeSessionId.get() === runtimeId
  const state = $sessionStates.get()[runtimeId]
  const owner = knownOwnerForSession(runtimeId)

  const target = JSON.stringify([
    runtimeId,
    state?.storedSessionId ?? null,
    owner && typeof owner === 'object' ? owner.connectionId : activeGatewayConnectionId(),
    typeof owner === 'string' ? owner : (owner?.profile ?? $activeGatewayProfile.get())
  ])

  const tokens = beginRuntimeOptionIntent(target, [
    ...(effort !== undefined ? ['effort'] : []),
    ...(fast !== undefined ? ['fast'] : [])
  ])

  const owns = (dimension: string) => {
    const current = $sessionStates.get()[runtimeId]
    const currentOwner = knownOwnerForSession(runtimeId)

    const currentTarget = JSON.stringify([
      runtimeId,
      current?.storedSessionId ?? null,
      currentOwner && typeof currentOwner === 'object' ? currentOwner.connectionId : activeGatewayConnectionId(),
      typeof currentOwner === 'string' ? currentOwner : (currentOwner?.profile ?? $activeGatewayProfile.get())
    ])

    return Boolean(current) && currentTarget === target && ownsRuntimeOptionIntent(target, dimension, tokens[dimension])
  }

  const previousEffort = state?.reasoningEffort ?? (primary ? $currentReasoningEffort.get() : '')
  const previousFast = state?.fast ?? (primary ? $currentFastMode.get() : false)

  const paint = (patch: Partial<{ reasoningEffort: string; fast: boolean }>, persist = true) => {
    sessionTileDelegate()?.updateSession(runtimeId, current => ({ ...current, ...patch }))

    if (primary && $activeSessionId.get() === runtimeId && $selectedStoredSessionId.get() === state?.storedSessionId) {
      if (patch.reasoningEffort !== undefined) {
        setCurrentReasoningEffort(patch.reasoningEffort, persist)
      }

      if (patch.fast !== undefined) {
        setCurrentFastMode(patch.fast, persist)
      }
    }
  }

  paint({
    ...(effort !== undefined ? { reasoningEffort: effort } : {}),
    ...(fast !== undefined ? { fast } : {})
  })

  const desired = { ...(effort !== undefined ? { effort } : {}), ...(fast !== undefined ? { fast } : {}) }
  // Legacy RPCs commit dimensions independently. A later failure cannot undo
  // an earlier acknowledgement, and deferred dimensions stay pending.
  const legacyAccepted = new Set<'effort' | 'fast'>()
  const legacyDeferred = new Set<'effort' | 'fast'>()
  sessionTileDelegate()?.updateSession(runtimeId, current => ({
    ...current,
    pendingRuntimeOptions: { ...current.pendingRuntimeOptions, ...desired }
  }))

  const clearPending = () => {
    // A settled write cannot recreate a closed runtime or touch a successor's
    // intent. Check ownership before asking the delegate to update any slice.
    if ((effort === undefined || !owns('effort')) && (fast === undefined || !owns('fast'))) {
      return
    }

    sessionTileDelegate()?.updateSession(runtimeId, current => {
      const pending = { ...current.pendingRuntimeOptions }

      if (effort !== undefined && owns('effort') && !legacyDeferred.has('effort')) {
        delete pending.effort
      }

      if (fast !== undefined && owns('fast') && !legacyDeferred.has('fast')) {
        delete pending.fast
      }

      return { ...current, pendingRuntimeOptions: Object.keys(pending).length ? pending : null }
    })
  }

  try {
    let result: { deferred?: boolean; selector_info?: SessionRuntimeInfo } | undefined

    try {
      result = await ctx.request<{ deferred?: boolean; selector_info?: SessionRuntimeInfo }>('config.set', {
        key: 'runtime_options',
        session_id: runtimeId,
        value: desired
      })
    } catch (err) {
      if (!isLegacyRuntimeOptionsBackend(err)) {
        throw err
      }

      // Older backend: nothing was applied. Use its established per-option
      // writes, in the same order, so a mid-upgrade user keeps the feature.
      if (effort !== undefined) {
        const response = await ctx.request<{ deferred?: boolean }>('config.set', {
          key: 'reasoning',
          session_id: runtimeId,
          value: effort
        })

        legacyAccepted.add('effort')

        if (response?.deferred) {
          legacyDeferred.add('effort')
        }
      }

      if (fast !== undefined) {
        const response = await ctx.request<{ deferred?: boolean }>('config.set', {
          key: 'fast',
          session_id: runtimeId,
          value: fast ? 'fast' : 'normal'
        })

        legacyAccepted.add('fast')

        if (response?.deferred) {
          legacyDeferred.add('fast')
        }
      }
    }

    if (effort !== undefined && owns('effort')) {
      clearRuntimeOptionUncertainty(runtimeId, 'effort')
    }

    if (fast !== undefined && owns('fast')) {
      clearRuntimeOptionUncertainty(runtimeId, 'fast')
    }

    if (!result?.deferred) {
      if (result?.selector_info && ((effort !== undefined && owns('effort')) || (fast !== undefined && owns('fast')))) {
        sessionTileDelegate()?.updateSession(runtimeId, current => ({
          ...current,
          ...reconcileSelectorInfo(current, result!.selector_info!, {
            settleOptions: (['effort', 'fast'] as const).filter(dimension => dimension in desired && owns(dimension))
          })
        }))
      }

      clearPending()
    }
  } catch (err) {
    let definitiveFailure = false

    for (const dimension of ['effort', 'fast'] as const) {
      if (!(dimension in desired) || !owns(dimension)) {
        continue
      }

      if (legacyAccepted.has(dimension)) {
        clearRuntimeOptionUncertainty(runtimeId, dimension)

        continue
      }

      if (legacyAccepted.has(dimension)) {
        clearRuntimeOptionUncertainty(runtimeId, dimension)
        continue
      }

      const reconciled = await reconcileRuntimeOptionFailure(err, {
        sessionId: runtimeId,
        dimension,
        request: ctx.request,
        owns: () => owns(dimension),
        applyObserved: value =>
          paint(dimension === 'effort' ? { reasoningEffort: value as string } : { fast: value as boolean }, false)
      })

      if (reconciled === 'not_uncertain' && owns(dimension)) {
        definitiveFailure = true
        paint(dimension === 'effort' ? { reasoningEffort: previousEffort } : { fast: previousFast })
      }
    }

    // The request has settled, even if delivery could not be reconciled.
    // Uncertainty remains explicit via its warning/marker; it must not keep
    // masking later authoritative selector observations or newer user edits.
    clearPending()

    if (definitiveFailure) {
      notifyError(err, ctx.failMessage)
    }
  }
}
