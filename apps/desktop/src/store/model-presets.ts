import { atom } from 'nanostores'

import { persistString, storedString } from '@/lib/storage'

import { activeGatewayConnectionId } from './gateway'
import { notifyError } from './notifications'
import { $activeGatewayProfile } from './profile'
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

    return currentTarget === target && ownsRuntimeOptionIntent(target, dimension, tokens[dimension])
  }

  const previousEffort = state?.reasoningEffort ?? (primary ? $currentReasoningEffort.get() : '')
  const previousFast = state?.fast ?? (primary ? $currentFastMode.get() : false)

  const paint = (patch: Partial<{ reasoningEffort: string; fast: boolean }>) => {
    sessionTileDelegate()?.updateSession(runtimeId, current => ({ ...current, ...patch }))

    if (primary && $activeSessionId.get() === runtimeId && $selectedStoredSessionId.get() === state?.storedSessionId) {
      if (patch.reasoningEffort !== undefined) {
        setCurrentReasoningEffort(patch.reasoningEffort)
      }

      if (patch.fast !== undefined) {
        setCurrentFastMode(patch.fast)
      }
    }
  }

  paint({
    ...(effort !== undefined ? { reasoningEffort: effort } : {}),
    ...(fast !== undefined ? { fast } : {})
  })

  try {
    if (effort !== undefined) {
      await ctx.request('config.set', { key: 'reasoning', session_id: ctx.sessionId, value: effort })
    }
  } catch (err) {
    if (owns('effort')) {
      paint({ reasoningEffort: previousEffort })
    }

    // Fast was painted but never sent when the first write rejected.
    if (fast !== undefined && owns('fast')) {
      paint({ fast: previousFast })
    }
    notifyError(err, ctx.failMessage)

    return
  }

  if (fast !== undefined && owns('fast')) {
    try {
      await ctx.request('config.set', { key: 'fast', session_id: runtimeId, value: fast ? 'fast' : 'normal' })
    } catch (err) {
      // The reasoning write is already acknowledged. Restore only the failed
      // Fast dimension; this sequence is intentionally not atomic.
      if (owns('fast')) {
        paint({ fast: previousFast })
      }
      notifyError(err, ctx.failMessage)
    }
  }
}
