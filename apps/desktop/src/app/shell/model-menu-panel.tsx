import { useStore } from '@nanostores/react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { computed } from 'nanostores'
import { useId, useMemo, useRef, useState } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import { Codicon } from '@/components/ui/codicon'
import { DropdownMenuItem, dropdownMenuRow } from '@/components/ui/dropdown-menu'
import type { HermesGateway } from '@/hermes'
import { useI18n } from '@/i18n'
import { modelOptionsQueryKey, requestModelOptions, selectionUnavailable } from '@/lib/model-options'
import { currentPickerSelection } from '@/lib/model-status-label'
import { DEFAULT_REASONING_EFFORT } from '@/lib/reasoning-effort'
import { cn } from '@/lib/utils'
import { activeGatewayConnectionId, requestGatewayForAgent } from '@/store/gateway'
import {
  $modelPresets,
  applyModelPreset,
  modelPresetKey,
  modelPresetScopeKey,
  runtimeOptionsTargetForSession,
  setModelPreset
} from '@/store/model-presets'
import { $visibleModels } from '@/store/model-visibility'
import {
  $defaultReasoningEffort,
  getComposerModelSelection,
  markComposerSelectionManual,
  peekRuntimeOptionIntent
} from '@/store/session'
import { $sessionStates, knownOwnerForSession } from '@/store/session-states'
import type { ModelOptionsResponse } from '@/types/hermes'

import { useDraftComposerOwner } from '../hooks/use-composer-model-owner'

import { isCurrentProvider, ModelCatalogMenu, type ModelMenuController } from './model-catalog-menu'

export { ModelMenuCloseContext } from './model-catalog-menu'

type PresetDimension = 'effort' | 'fast'

interface ModelPresetSelectionIntent {
  modelKey: string
  revisions: Record<PresetDimension, number>
  runtimeTarget?: string
  runtimeTokens?: Record<string, number | undefined>
  scopeKey: string
}

function presetRevisionKey(scopeKey: string, provider: string, model: string, dimension: PresetDimension): string {
  return JSON.stringify([scopeKey, provider, model, dimension])
}

// Draft preset identity belongs to the composing surface, not to this menu: the
// dropdown unmounts its content when closed, so a component-local id would drop
// a draft's remembered options on every reopen. A surface keeps its draft id
// until the session identity it shows changes (draft → session → new draft).
let draftPresetSequence = 0
const draftPresetOwners = new Map<string, { id: string; identity: null | string }>()

export function draftPresetIdFor(surfaceKey: string, sessionIdentity: null | string): string {
  const existing = draftPresetOwners.get(surfaceKey)

  if (existing && existing.identity === sessionIdentity) {
    return existing.id
  }

  const next = { id: `draft-${++draftPresetSequence}`, identity: sessionIdentity }
  draftPresetOwners.delete(surfaceKey)
  draftPresetOwners.set(surfaceKey, next)

  // Bounded: tile surfaces get mount-local keys, so evict the oldest tile entries.
  for (const key of draftPresetOwners.keys()) {
    if (draftPresetOwners.size <= 64) {
      break
    }

    if (key !== 'primary') {
      draftPresetOwners.delete(key)
    }
  }

  return next.id
}

/** Test-only reset for the module-scoped draft identities. */
export function _resetDraftPresetOwnersForTests(): void {
  draftPresetOwners.clear()
}

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
 * surface's session or draft, remember target-row choices only for that owner,
 * keep optimistic stores honest, and roll back through the runtime option owner.
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
  const storedSessionId = useStore(view.$storedId)
  const sessionPresetIdentity = storedSessionId || activeSessionId
  // Tiles are always bound to a stored session, so only the primary surface
  // composes drafts; a tile keeps a mount-local key for its (unused) draft slot.
  const tileSurfaceId = useId()
  const surfaceKey = view.kind === 'primary' ? 'primary' : `tile:${tileSurfaceId}`
  const scopedDraftPresetId = draftPresetIdFor(surfaceKey, sessionPresetIdentity ?? null)
  const owner = knownOwnerForSession(activeSessionId)
  const draftOwner = useDraftComposerOwner()

  const connectionId =
    owner && typeof owner === 'object'
      ? owner.connectionId
      : owner
        ? null
        : !activeSessionId
          ? draftOwner.connectionId
          : activeGatewayConnectionId()

  const catalogProfile =
    (typeof owner === 'string' ? owner : owner?.targetProfile || owner?.profile) ||
    (!activeSessionId ? draftOwner.targetProfile || draftOwner.profile : profile)

  const modelPresetScope = modelPresetScopeKey({
    connectionId,
    draftId: scopedDraftPresetId,
    profile: catalogProfile,
    sessionId: sessionPresetIdentity
  })

  const optionPresetRevisions = useRef(new Map<string, number>())

  const catalogRequest = !activeSessionId
    ? <T,>(method: string, params?: Record<string, unknown>) =>
        requestGatewayForAgent<T>(draftOwner.connectionId, draftOwner.profile, method, params)
    : requestGateway

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

  const pendingOptions = useStore(
    useMemo(
      () =>
        computed($sessionStates, states => Boolean(activeSessionId && states[activeSessionId]?.pendingRuntimeOptions)),
      [activeSessionId]
    )
  )

  const currentModel = useStore(view.$model)
  const currentProvider = useStore(view.$provider)
  const draftSelection = !activeSessionId ? getComposerModelSelection(draftOwner) : null
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
    queryKey: modelOptionsQueryKey(catalogProfile, activeSessionId, connectionId),
    queryFn: (): Promise<ModelOptionsResponse> =>
      requestModelOptions({
        connectionId,
        gateway,
        profile: catalogProfile,
        request: catalogRequest,
        sessionId: activeSessionId
      })
  })

  const { model: optionsModel, provider: optionsProvider } = currentPickerSelection(
    activeSessionId
      ? { model: currentModel, provider: currentProvider }
      : {
          model: draftSelection?.model || '',
          provider: draftSelection?.provider || '',
          authoritative: Boolean(draftSelection)
        },
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
      const queryKey = modelOptionsQueryKey(catalogProfile, activeSessionId, connectionId)

      const next = await requestModelOptions({
        connectionId,
        gateway,
        profile: catalogProfile,
        refresh: true,
        request: catalogRequest,
        sessionId: activeSessionId
      })

      queryClient.setQueryData<ModelOptionsResponse>(queryKey, next)

      // Refresh changes discovery only. Preserve the session/manual selection;
      // a missing binding is explained below until the user chooses another.
    } catch {
      // Network/backend hiccup — fall back to a plain invalidate so the next
      // open re-fetches (still cached, but no worse than before).
      void queryClient.invalidateQueries({ queryKey: ['model-options'] })
    } finally {
      setRefreshing(false)
    }
  }

  const patchRuntimeOption = async (dimension: 'effort' | 'fast', next: string | boolean, message: string) => {
    if (touchesPrimary && !activeSessionId) {
      markComposerSelectionManual()
    }

    await applyModelPreset(dimension === 'effort' ? { effort: next as string } : { fast: next as boolean }, {
      failMessage: message,
      primary: touchesPrimary,
      request: requestGateway,
      sessionId: activeSessionId
    })
  }

  const controller: ModelMenuController = {
    preserveOptionsOnSelect: true,
    beginSelection: (provider, model) => {
      const runtimeTarget = activeSessionId ? runtimeOptionsTargetForSession(activeSessionId) : undefined

      // Snapshot, do not revoke: an effort/Fast write already in flight keeps
      // ownership of its own rollback and pending-overlay settlement. Any later
      // option edit changes the newest token and voids this continuation.
      const runtimeTokens = runtimeTarget
        ? {
            effort: peekRuntimeOptionIntent(runtimeTarget, 'effort'),
            fast: peekRuntimeOptionIntent(runtimeTarget, 'fast')
          }
        : undefined

      return {
        modelKey: JSON.stringify([provider, model]),
        revisions: {
          effort:
            optionPresetRevisions.current.get(presetRevisionKey(modelPresetScope, provider, model, 'effort')) ?? 0,
          fast: optionPresetRevisions.current.get(presetRevisionKey(modelPresetScope, provider, model, 'fast')) ?? 0
        },
        runtimeTarget,
        runtimeTokens,
        scopeKey: modelPresetScope
      } satisfies ModelPresetSelectionIntent
    },
    // Only the target row's explicit settings are applied. The runtime intent
    // token and local per-dimension revisions prevent a delayed model ACK from
    // overwriting an option edit made while that switch was pending.
    applyPreset: (preset, row, intent) => {
      const selectionModel = row.selectionModel ?? row.model
      const provider = modelOptions.data?.providers?.find(item => item.slug === row.provider)

      if (view.$model.get() !== selectionModel || !provider || !isCurrentProvider(provider, view.$provider.get())) {
        return
      }

      const selectionIntent = intent as ModelPresetSelectionIntent | undefined
      const applicable: { effort?: string; fast?: boolean } = { ...preset }

      if (selectionIntent) {
        if (
          selectionIntent.scopeKey !== modelPresetScope ||
          selectionIntent.modelKey !== JSON.stringify([row.provider, row.model])
        ) {
          return
        }

        for (const dimension of ['effort', 'fast'] as const) {
          const key = presetRevisionKey(modelPresetScope, row.provider, row.model, dimension)
          const currentRevision = optionPresetRevisions.current.get(key) ?? 0
          const runtimeIntent = selectionIntent.runtimeTokens?.[dimension]

          const runtimeStillOwned =
            !activeSessionId ||
            !selectionIntent.runtimeTarget ||
            (runtimeOptionsTargetForSession(activeSessionId) === selectionIntent.runtimeTarget &&
              peekRuntimeOptionIntent(selectionIntent.runtimeTarget, dimension) === runtimeIntent)

          if (currentRevision !== selectionIntent.revisions[dimension] || !runtimeStillOwned) {
            delete applicable[dimension]
          }
        }
      }

      if (applicable.effort === undefined && applicable.fast === undefined) {
        return
      }

      setModelPreset(modelPresetScope, row.provider, row.model, applicable)

      void applyModelPreset(applicable, {
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

    presetFor: (provider, model) => modelPresets[modelPresetKey(modelPresetScope, provider, model)] ?? {},

    rememberOptions: (patch, row) => {
      if (patch.fast === undefined) {
        return
      }

      const key = presetRevisionKey(modelPresetScope, row.provider, row.model, 'fast')
      optionPresetRevisions.current.set(key, (optionPresetRevisions.current.get(key) ?? 0) + 1)
      setModelPreset(modelPresetScope, row.provider, row.model, { fast: patch.fast })
    },

    // The composer picker never persists the profile default. With a session it
    // scopes the switch to that session; with none it's UI state shipped on the
    // next session.create. Always stamp sessionId from this surface so a tile
    // switch never hits the primary (busy) session by accident.
    select: (model, provider) => onSelectModel({ model, provider, sessionId: activeSessionId || null }),

    setOptions: (patch, row) => {
      // Pending target-row options are isolated to the surface's owning session.
      // Revisions also invalidate post-switch writes that started before a newer edit.
      if (patch.effort !== undefined) {
        const key = presetRevisionKey(modelPresetScope, row.provider, row.model, 'effort')
        optionPresetRevisions.current.set(key, (optionPresetRevisions.current.get(key) ?? 0) + 1)
      }

      if (patch.fast !== undefined) {
        const key = presetRevisionKey(modelPresetScope, row.provider, row.model, 'fast')
        optionPresetRevisions.current.set(key, (optionPresetRevisions.current.get(key) ?? 0) + 1)
      }

      if (patch.effort !== undefined || patch.fast !== undefined) {
        setModelPreset(modelPresetScope, row.provider, row.model, patch)
      }

      if (!row.isActive) {
        return
      }

      if (patch.effort !== undefined) {
        void patchRuntimeOption('effort', patch.effort, t.shell.modelOptions.updateFailed)
      }

      if (patch.fast !== undefined) {
        void patchRuntimeOption('fast', patch.fast, t.shell.modelOptions.fastFailed)
      }
    }
  }

  return (
    <ModelCatalogMenu
      connectionId={connectionId}
      controller={controller}
      footer={
        <>
          {modelOptions.data && selectionUnavailable(modelOptions.data.providers, optionsProvider, optionsModel) && (
            <div className="px-2 py-1 text-xs text-(--ui-text-secondary)" role="status">
              {t.modelPicker.selectionUnavailable}
            </div>
          )}
          {activeSessionId && (
            <div className="px-2 py-1 text-xs text-(--ui-text-secondary)" role="status">
              {t.modelPicker.sessionSelection}
            </div>
          )}
          {unconfirmedOptions && (
            <div className="px-2 py-1 text-xs text-(--ui-text-secondary)" role="status">
              {t.shell.modelOptions.unconfirmed}
            </div>
          )}
          {pendingOptions && (
            <div className="px-2 py-1 text-xs text-(--ui-text-secondary)" role="status">
              {t.install.stageStates.pending} · {t.shell.modelOptions.options}
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
      profile={catalogProfile}
      request={catalogRequest}
      sessionId={activeSessionId}
    />
  )
}
