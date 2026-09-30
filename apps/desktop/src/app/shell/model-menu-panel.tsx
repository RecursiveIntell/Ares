import { useStore } from '@nanostores/react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { computed } from 'nanostores'
import { useMemo, useState } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import { Codicon } from '@/components/ui/codicon'
import { DropdownMenuItem, dropdownMenuRow } from '@/components/ui/dropdown-menu'
import type { HermesGateway } from '@/hermes'
import { useI18n } from '@/i18n'
import { modelOptionsQueryKey, reconcileSelectionAfterCatalogRefresh, requestModelOptions } from '@/lib/model-options'
import { currentPickerSelection } from '@/lib/model-status-label'
import { DEFAULT_REASONING_EFFORT } from '@/lib/reasoning-effort'
import { cn } from '@/lib/utils'
import { activeGatewayConnectionId } from '@/store/gateway'
import { $modelPresets, applyModelPreset, modelPresetKey, setModelPreset } from '@/store/model-presets'
import { $visibleModels } from '@/store/model-visibility'
import { notifyError } from '@/store/notifications'
import { $activeGatewayProfile } from '@/store/profile'
import { clearRuntimeOptionUncertainty, reconcileRuntimeOptionFailure } from '@/store/runtime-option-recovery'
import {
  $activeSessionId,
  $defaultReasoningEffort,
  $selectedStoredSessionId,
  beginRuntimeOptionIntent,
  markComposerSelectionManual,
  ownsRuntimeOptionIntent,
  setCurrentFastMode,
  setCurrentReasoningEffort
} from '@/store/session'
import { $sessionStates, knownOwnerForSession, sessionTileDelegate } from '@/store/session-states'
import type { ModelOptionsResponse } from '@/types/hermes'

import { ModelCatalogMenu, type ModelMenuController } from './model-catalog-menu'

export { ModelMenuCloseContext } from './model-catalog-menu'

export interface ModelSelection {
  model: string
  provider: string
  /** Runtime id of the surface that opened the menu. When set, the switch
   *  targets that session (a tile) instead of the primary `$activeSessionId`. */
  sessionId?: null | string
}

interface ModelMenuPanelProps {
  gateway?: HermesGateway
  onSelectModel: (selection: ModelSelection) => Promise<boolean> | void
  profile?: string
  requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
}

/**
 * The composer's model menu: `ModelCatalogMenu` (the shared renderer) plus the
 * controller that gives a selection its meaning HERE — write through to this
 * surface's session, remember the pick as a global preset, keep the optimistic
 * stores honest, and roll back on a failed gateway write.
 */
export function ModelMenuPanel({ gateway, onSelectModel, profile = 'default', requestGateway }: ModelMenuPanelProps) {
  const { t } = useI18n()
  const copy = t.shell.modelMenu
  const [refreshing, setRefreshing] = useState(false)
  const queryClient = useQueryClient()
  // Bind to THIS surface's SessionView (primary or tile) so each pane's menu
  // shows/switches its own model — not the primary-only globals.
  const view = useSessionView()
  const activeSessionId = useStore(view.$runtimeId)

  const unconfirmedOptions = useStore(
    useMemo(
      () =>
        computed($sessionStates, states =>
          Boolean(activeSessionId && states[activeSessionId]?.unconfirmedRuntimeOptions?.length)
        ),
      [activeSessionId]
    )
  )

  const currentFastMode = useStore(view.$fast)
  const currentModel = useStore(view.$model)
  const currentProvider = useStore(view.$provider)
  const currentReasoningEffort = useStore(view.$reasoningEffort)
  const modelPresets = useStore($modelPresets)
  const defaultEffort = useStore($defaultReasoningEffort) || DEFAULT_REASONING_EFFORT
  const visibleModels = useStore($visibleModels)
  const touchesPrimary = view.kind === 'primary'

  // Subscribe to the SAME query the menu runs (identical key ⇒ React Query
  // dedupes, no second fetch). It must be a live subscription, not a cache
  // peek: with no model in the session store yet, currentPickerSelection falls
  // back to the catalog's reported current, and a non-reactive read would
  // never repaint that fallback once the catalog resolved.
  const modelOptions = useQuery({
    queryKey: modelOptionsQueryKey(profile, activeSessionId),
    queryFn: (): Promise<ModelOptionsResponse> =>
      requestModelOptions({ gateway, profile, request: requestGateway, sessionId: activeSessionId })
  })

  const { model: optionsModel, provider: optionsProvider } = currentPickerSelection(
    { model: currentModel, provider: currentProvider },
    modelOptions.data
  )

  // Explicit "Refresh Models": re-fetch the catalog with refresh:true so the
  // backend busts its 1h provider-model disk cache and re-pulls each provider's
  // live list. Fixes live-only models (e.g. OpenCode Zen free tier) vanishing
  // when the cache expires and falls back to the curated static list.
  const refreshModels = async () => {
    if (refreshing) {
      return
    }

    setRefreshing(true)

    try {
      const queryKey = modelOptionsQueryKey(profile, activeSessionId)

      const next = await requestModelOptions({
        gateway,
        profile,
        refresh: true,
        request: requestGateway,
        sessionId: activeSessionId
      })

      queryClient.setQueryData<ModelOptionsResponse>(queryKey, next)

      // Group / credential swaps can return a catalog that no longer contains
      // the session's current model. The store + currentPickerSelection would
      // otherwise keep painting the stale id (it is not in the new list).
      const switchTo = reconcileSelectionAfterCatalogRefresh(optionsModel, next.providers)

      if (switchTo) {
        await onSelectModel({ ...switchTo, sessionId: activeSessionId || null })
      }
    } catch {
      // Network/backend hiccup — fall back to a plain invalidate so the next
      // open re-fetches (still cached, but no worse than before).
      void queryClient.invalidateQueries({ queryKey: ['model-options'] })
    } finally {
      setRefreshing(false)
    }
  }

  const optionTarget = (runtimeId: string) => {
    const state = $sessionStates.get()[runtimeId]
    const owner = knownOwnerForSession(runtimeId)

    return JSON.stringify([
      runtimeId,
      state?.storedSessionId ?? null,
      owner && typeof owner === 'object' ? owner.connectionId : activeGatewayConnectionId(),
      typeof owner === 'string' ? owner : (owner?.profile ?? $activeGatewayProfile.get())
    ])
  }

  const patchRuntimeOption = async (
    dimension: 'effort' | 'fast',
    next: string | boolean,
    previous: string | boolean,
    message: string
  ) => {
    if (!activeSessionId) {
      if (dimension === 'effort') {
        setCurrentReasoningEffort(next as string)
      } else {
        setCurrentFastMode(next as boolean)
      }

      markComposerSelectionManual()

      return
    }

    const runtimeId = activeSessionId
    const storedSessionId = $sessionStates.get()[runtimeId]?.storedSessionId
    const target = optionTarget(runtimeId)
    const token = beginRuntimeOptionIntent(target, [dimension])[dimension]

    const owns = () =>
      Boolean($sessionStates.get()[runtimeId]) &&
      optionTarget(runtimeId) === target &&
      $sessionStates.get()[runtimeId]?.storedSessionId === storedSessionId &&
      ownsRuntimeOptionIntent(target, dimension, token)

    const update = (value: string | boolean) => {
      if (dimension === 'effort') {
        sessionTileDelegate()?.updateSession(runtimeId, state => ({ ...state, reasoningEffort: value as string }))
      } else {
        sessionTileDelegate()?.updateSession(runtimeId, state => ({ ...state, fast: value as boolean }))
      }

      // Explicit primary choices still seed future drafts. An old callback
      // may update its own runtime, but never another foreground draft.
      if (
        touchesPrimary &&
        $activeSessionId.get() === runtimeId &&
        $selectedStoredSessionId.get() === storedSessionId
      ) {
        if (dimension === 'effort') {
          setCurrentReasoningEffort(value as string)
        } else {
          setCurrentFastMode(value as boolean)
        }
      }
    }

    if (touchesPrimary) {
      markComposerSelectionManual()
    }

    update(next)

    try {
      await requestGateway('config.set', {
        key: dimension === 'effort' ? 'reasoning' : 'fast',
        session_id: runtimeId,
        value: dimension === 'fast' ? ((next as boolean) ? 'fast' : 'normal') : next
      })

      if (owns()) {
        clearRuntimeOptionUncertainty(runtimeId, dimension)
      }
    } catch (err) {
      if (
        await reconcileRuntimeOptionFailure(err, {
          sessionId: runtimeId,
          dimension,
          request: requestGateway,
          owns,
          applyObserved: update
        })
      ) {
        return
      }

      if (owns()) {
        update(previous)
      }

      notifyError(err, message)
    }
  }

  const controller: ModelMenuController = {
    // Selecting a model row restores that model's remembered preset onto the
    // session (effort/fast). applyModelPreset owns the batched gateway write.
    applyPreset: (preset, row) => {
      setModelPreset(row.provider, row.model, preset)

      void applyModelPreset(preset, {
        failMessage: t.shell.modelOptions.updateFailed,
        primary: touchesPrimary,
        request: requestGateway,
        sessionId: activeSessionId
      })
    },

    current: {
      effort: currentReasoningEffort,
      fast: currentFastMode,
      model: optionsModel,
      provider: optionsProvider
    },

    presetFor: (provider, model) => modelPresets[modelPresetKey(provider, model)] ?? {},

    // The composer picker never persists the profile default. With a session it
    // scopes the switch to that session; with none it's UI state shipped on the
    // next session.create. Always stamp sessionId from this surface so a tile
    // switch never hits the primary (busy) session by accident.
    select: (model, provider) => onSelectModel({ model, provider, sessionId: activeSessionId || null }),

    setOptions: (patch, row) => {
      // Editing always records the model's global preset (keyed by
      // provider::model, not per-surface — a tile edit re-applies to that model
      // everywhere); the active model also gets it pushed onto its OWN session.
      // Non-active edits stay preset-only — no model switch, no session write.
      if (patch.effort !== undefined || patch.fast !== undefined) {
        setModelPreset(row.provider, row.model, patch)
      }

      if (!row.isActive) {
        return
      }

      if (patch.effort !== undefined) {
        void patchRuntimeOption('effort', patch.effort, currentReasoningEffort, t.shell.modelOptions.updateFailed)
      }

      if (patch.fast !== undefined) {
        void patchRuntimeOption('fast', patch.fast, currentFastMode, t.shell.modelOptions.fastFailed)
      }
    }
  }

  return (
    <ModelCatalogMenu
      controller={controller}
      footer={
        <>
          {unconfirmedOptions && (
            <div className="px-2 py-1 text-xs text-(--ui-text-secondary)" role="status">
              {t.shell.modelOptions.unconfirmed}
            </div>
          )}
          <DropdownMenuItem
            className={cn(dropdownMenuRow, 'text-(--ui-text-tertiary)')}
            disabled={refreshing}
            onSelect={event => {
              event.preventDefault()
              void refreshModels()
            }}
          >
            <Codicon className={cn(refreshing && 'animate-spin')} name="sync" size="0.75rem" />
            {copy.refreshModels}
          </DropdownMenuItem>
        </>
      }
      gateway={gateway}
      includeMoa
      profile={profile}
      request={requestGateway}
      sessionId={activeSessionId}
    />
  )
}
