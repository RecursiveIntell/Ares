import { type QueryClient } from '@tanstack/react-query'
import { useCallback, useRef } from 'react'

import { getApiRequestConnection } from '@/api/client'
import type { ModelSelection } from '@/app/shell/model-menu-panel'
import { getGlobalModelInfo } from '@/hermes'
import { useI18n } from '@/i18n'
import { isBusySessionModelSwitch } from '@/lib/gateway-rpc'
import { surfaceModelSwitchConfirm } from '@/lib/guarded-model-switch'
import { modelOptionsQueryKey } from '@/lib/model-options'
import { activeGatewayConnectionId } from '@/store/gateway'
import { notifyError } from '@/store/notifications'
import { $activeGatewayProfile } from '@/store/profile'
import {
  $activeSessionId,
  $currentModel,
  $currentProvider,
  $selectedStoredSessionId,
  beginRuntimeOptionIntent,
  captureComposerModelSelection,
  getComposerModelSelection,
  getComposerSelectionGeneration,
  getCurrentModelSource,
  markComposerSelectionManual,
  ownsRuntimeOptionIntent,
  recordComposerModelSelection,
  restoreComposerModelSelection,
  setComposerModelSelectionOwner,
  setCurrentModel,
  setCurrentModelSource,
  setCurrentProvider
} from '@/store/session'
import { isSessionGoneError } from '@/store/session-gone'
import { $sessionStates, knownOwnerForSession, sessionTileDelegate } from '@/store/session-states'
import type { ModelOptionsResponse } from '@/types/hermes'

import {
  captureDraftComposerOwner,
  captureModelRequestOwner,
  composerOwnerForSession,
  composerOwnerKey,
  type MainModelSavedChange,
  ownsMainModelSave
} from './composer-model-selection-owner'

type ComposerStamp = NonNullable<ReturnType<typeof getComposerModelSelection>>
// Shared across mounted controls; only the owning intent can restore a chain.
const composerRollbacks = new Map<string, { published: ComposerStamp; baseline: ComposerStamp | null }>()

interface ModelControlsOptions {
  queryClient: QueryClient
  requestGateway: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
  /**
   * Rebind a stored session through the surface that owns its runtime map.
   * The primary caller performs its full foreground resume; a tile caller
   * rebinds only that tile. Returning null refuses a retry after route drift.
   */
  recoverRuntime?: (storedSessionId: string, staleRuntimeId: string) => Promise<null | string>
}

interface ModelSwitchResponse {
  confirm_message?: string
  confirm_required?: boolean
  deferred?: boolean
}

/** A recovery owner declined to rebind because the user moved on. */
class ModelSwitchRecoveryAborted extends Error {}

export function useModelControls({ queryClient, recoverRuntime, requestGateway }: ModelControlsOptions) {
  const { t } = useI18n()
  const copy = t.desktop
  const profileRefreshEpochRef = useRef(0)

  // All callbacks here read reactive session state from the store (.get())
  // rather than capturing it as a prop. The actions bag in wiring.tsx mutates
  // in place to keep a stable identity, so memoized surfaces capture these
  // callbacks once and never re-evaluate — a captured prop would be stale
  // forever. The store read is always current.
  const updateModelOptionsCache = useCallback(
    (
      sessionId: null | string,
      provider: string,
      model: string,
      includeGlobal: boolean,
      profile = $activeGatewayProfile.get(),
      connectionId = getApiRequestConnection()
    ) => {
      const patch = (prev: ModelOptionsResponse | undefined) => {
        // Selection state can update before the catalog query has resolved.
        // Keep that optimistic cache structurally complete; the composer
        // interprets a response without `providers` as an empty catalog.
        const providers = prev?.providers?.length
          ? prev.providers
          : provider && model
            ? [{ models: [model], name: provider, slug: provider }]
            : []

        return { ...prev, provider, model, providers }
      }

      queryClient.setQueryData<ModelOptionsResponse>(modelOptionsQueryKey(profile, sessionId, connectionId), patch)

      if (includeGlobal) {
        queryClient.setQueryData<ModelOptionsResponse>(modelOptionsQueryKey(profile, null, connectionId), patch)
      }
    },
    [queryClient]
  )

  // Settings → Model writes the profile default, which the backend applies to
  // new sessions only. Keep a live session's renderer state and session-scoped
  // model-options cache authoritative instead of briefly painting the saved
  // default as if the active agent had switched. Marking the composer as
  // default-derived still lets the next fresh draft reseed from profile config.
  const applySavedMainModel = useCallback(
    (change: MainModelSavedChange) => {
      if (!ownsMainModelSave(change)) {
        return
      }

      profileRefreshEpochRef.current += 1
      const { owner, provider, model } = change
      const current = getComposerModelSelection(owner)

      // The latest confirmed save supersedes a weaker default read, but a
      // deliberate owner-scoped pin survives all profile-default saves.
      if (current?.source !== 'manual') {
        const stamp = recordComposerModelSelection(captureComposerModelSelection(owner), {
          provider,
          model,
          source: 'default'
        })

        if (
          stamp &&
          !$activeSessionId.get() &&
          composerOwnerKey(captureDraftComposerOwner()) === composerOwnerKey(owner)
        ) {
          setCurrentProvider(stamp.provider)
          setCurrentModel(stamp.model)
          setCurrentModelSource(stamp.source)
        }
      }

      updateModelOptionsCache(null, provider, model, false, owner.targetProfile || owner.profile, owner.connectionId)
    },
    [updateModelOptionsCache]
  )

  // Seed the composer's model state from the profile default. `force` reseeds
  // for a profile swap (the new profile has its own default); otherwise this
  // only fills an EMPTY selection so a user's pick (plain UI state in
  // $currentModel) survives the lifecycle refreshes that fire on boot / fresh
  // draft / session events. A live session owns the footer, so skip entirely.
  const refreshCurrentModel = useCallback(
    async (force = false) => {
      // A forced profile swap opens a new intent epoch; an older in-flight
      // response for a previous profile must stand down when it resolves.
      if (force) {
        profileRefreshEpochRef.current += 1
      }

      const profileRefreshEpoch = profileRefreshEpochRef.current

      try {
        if ($activeSessionId.get()) {
          return
        }

        const draftOwner = captureDraftComposerOwner()
        setComposerModelSelectionOwner(draftOwner)
        const ownedSelection = getComposerModelSelection(draftOwner)

        if (ownedSelection?.source === 'manual') {
          setCurrentModel(ownedSelection.model)
          setCurrentProvider(ownedSelection.provider)
          setCurrentModelSource('manual')

          return
        }

        // A route swap can leave the old mirror visible until activation
        // completes. Seed only the new owner's known pair, never relabel it.
        if (force) {
          setCurrentModel(ownedSelection?.model ?? '')
          setCurrentProvider(ownedSelection?.provider ?? '')
          setCurrentModelSource(ownedSelection?.source ?? '')
        }

        const requestOwner = captureModelRequestOwner()

        if (composerOwnerKey(requestOwner) !== composerOwnerKey(draftOwner)) {
          return
        }

        const requestTicket = captureComposerModelSelection(requestOwner)
        const selectionGeneration = getComposerSelectionGeneration()
        const connectionId = getApiRequestConnection()
        const profile = $activeGatewayProfile.get()
        // The helper builds REST scope synchronously, before this first await.
        const result = await getGlobalModelInfo(requestOwner.targetProfile || requestOwner.profile)

        if (
          profileRefreshEpochRef.current !== profileRefreshEpoch ||
          $activeGatewayProfile.get() !== profile ||
          getApiRequestConnection() !== connectionId ||
          $activeSessionId.get() ||
          getComposerSelectionGeneration() !== selectionGeneration ||
          composerOwnerKey(captureDraftComposerOwner()) !== composerOwnerKey(requestOwner) ||
          getComposerModelSelection(requestOwner)?.source === 'manual'
        ) {
          return
        }

        if (typeof result.model !== 'string' || typeof result.provider !== 'string') {
          return
        }

        const stamp = recordComposerModelSelection(requestTicket, {
          model: result.model,
          provider: result.provider,
          source: 'default'
        })

        if (stamp) {
          setCurrentModel(stamp.model)
          setCurrentProvider(stamp.provider)
          setCurrentModelSource(stamp.source)
        }
      } catch {
        // The delayed session.info event still updates this once the agent is ready.
      }
    },
    []
  )

  // Returns whether the switch was applied so callers can await it before
  // applying follow-up changes. `true` means applied (or deferred/busy-queued
  // for the next turn). `false` means NOT applied — either pending
  // confirmation (warning with Confirm action already shown, pill rolled back)
  // or a real failure (error toast). Callers must NOT treat `false` as a
  // generic failure: for `pending` the gateway intentionally returned
  // `confirm_required` and no error should be surfaced.
  // The composer model is plain UI state: with no live session it's just
  // stored (and shipped on the next session.create); with one it's scoped to
  // that session via config.set. It NEVER writes the profile default — that
  // lives in Settings → Model — so picking a model here can't silently mutate
  // global config.
  //
  // `selection.sessionId` targets a specific surface (tile). When omitted, the
  // primary `$activeSessionId` is used (overlay / legacy callers). A tile
  // switch must not touch the primary globals — and must not be blocked by a
  // busy primary turn.
  const selectModel = useCallback(
    async (selection: ModelSelection): Promise<boolean> => {
      const primaryRuntimeId = $activeSessionId.get()
      let liveSessionId = 'sessionId' in selection ? (selection.sessionId ?? null) : primaryRuntimeId
      const touchesPrimary = !liveSessionId || liveSessionId === primaryRuntimeId

      const runtimeOwner = liveSessionId ? knownOwnerForSession(liveSessionId) : null
      const composerOwner = liveSessionId ? composerOwnerForSession(runtimeOwner) : captureDraftComposerOwner()

      if (!liveSessionId && composerOwner) {
        setComposerModelSelectionOwner(composerOwner)
      }

      const previousComposerStamp = composerOwner ? getComposerModelSelection(composerOwner) : null

      const prevModel = !liveSessionId
        ? (previousComposerStamp?.model ?? '')
        : touchesPrimary
          ? $currentModel.get()
          : ($sessionStates.get()[liveSessionId]?.model ?? '')

      const prevProvider = !liveSessionId
        ? (previousComposerStamp?.provider ?? '')
        : touchesPrimary
          ? $currentProvider.get()
          : ($sessionStates.get()[liveSessionId]?.provider ?? '')

      const prevSource = previousComposerStamp?.source ?? getCurrentModelSource()
      const pendingBefore = liveSessionId ? $sessionStates.get()[liveSessionId]?.pendingModelSelection : null
      const continuesOptimisticChain = pendingBefore?.model === prevModel && pendingBefore?.provider === prevProvider

      const rollbackModel = continuesOptimisticChain
        ? (pendingBefore?.rollbackModel ?? pendingBefore?.previousModel ?? prevModel)
        : prevModel

      const rollbackProvider = continuesOptimisticChain
        ? (pendingBefore?.rollbackProvider ?? pendingBefore?.previousProvider ?? prevProvider)
        : prevProvider

      const rollbackSource = continuesOptimisticChain ? (pendingBefore?.rollbackSource ?? prevSource) : prevSource
      const owner = runtimeOwner
      const liveConnectionId = composerOwner ? composerOwner.connectionId : getApiRequestConnection()

      const liveGatewayProfile = composerOwner
        ? composerOwner.targetProfile || composerOwner.profile
        : $activeGatewayProfile.get()

      // A runtime id is ephemeral. Keep its durable owner while the switch is
      // in flight so a 4001 can be resumed by the correct surface instead of
      // being reported as a model failure.
      const storedSessionId = liveSessionId
        ? ($sessionStates.get()[liveSessionId]?.storedSessionId ??
          (touchesPrimary ? $selectedStoredSessionId.get() : null))
        : null

      const targetKey = (runtimeId: string | null, storedId: string | null) => {
        const owner = runtimeId ? knownOwnerForSession(runtimeId) : undefined
        const ownerConnection = owner && typeof owner === 'object' ? owner.connectionId : owner ? 'local' : undefined
        const ownerProfile = typeof owner === 'string' ? owner : owner?.targetProfile || owner?.profile

        const stampOwner = runtimeId ? composerOwnerForSession(owner) : captureDraftComposerOwner()

        return JSON.stringify([
          runtimeId,
          (runtimeId && $sessionStates.get()[runtimeId]?.storedSessionId) || storedId,
          stampOwner?.connectionId ?? ownerConnection ?? activeGatewayConnectionId(),
          stampOwner?.profile ?? ownerProfile ?? $activeGatewayProfile.get(),
          stampOwner?.targetProfile ?? stampOwner?.profile ?? ownerProfile ?? $activeGatewayProfile.get()
        ])
      }

      let selectionTarget = targetKey(liveSessionId, storedSessionId)
      let intentTokens = beginRuntimeOptionIntent(selectionTarget, ['model'])

      const owns = (dimension: string) =>
        targetKey(liveSessionId, storedSessionId) === selectionTarget &&
        ownsRuntimeOptionIntent(selectionTarget, dimension, intentTokens[dimension])

      const updateLiveRuntimeSelection = (model: string, provider: string, optimistic = true) => {
        if (!liveSessionId) {
          return
        }

        // Every live surface (including the primary pane) renders model metadata
        // from its runtime slice. Updating only the primary global composer
        // atoms leaves the picker reading the old runtime model until a later
        // session.info arrives — or indefinitely if that event is delayed.
        sessionTileDelegate()?.updateSession(liveSessionId, state => {
          const pendingModelSelection = optimistic
            ? {
                model,
                provider,
                previousModel: prevModel,
                previousProvider: prevProvider,
                rollbackModel,
                rollbackProvider,
                rollbackSource
              }
            : null

          const pendingMatches =
            pendingModelSelection === null
              ? state.pendingModelSelection === null
              : state.pendingModelSelection?.model === pendingModelSelection.model &&
                state.pendingModelSelection.provider === pendingModelSelection.provider &&
                state.pendingModelSelection.previousModel === pendingModelSelection.previousModel &&
                state.pendingModelSelection.previousProvider === pendingModelSelection.previousProvider &&
                state.pendingModelSelection.rollbackModel === pendingModelSelection.rollbackModel &&
                state.pendingModelSelection.rollbackProvider === pendingModelSelection.rollbackProvider &&
                state.pendingModelSelection.rollbackSource === pendingModelSelection.rollbackSource

          const nextState = {
            ...state,
            model,
            provider,
            pendingModelSelection
          }

          return state.model === nextState.model && state.provider === nextState.provider && pendingMatches
            ? state
            : nextState
        })
      }

      const inheritedRollback = composerRollbacks.get(selectionTarget)

      const rollbackComposerStamp =
        continuesOptimisticChain && inheritedRollback?.published === previousComposerStamp
          ? inheritedRollback.baseline
          : previousComposerStamp

      let publishedComposerStamp: ComposerStamp | null = null

      const paintSelection = () => {
        if (touchesPrimary) {
          // Mirror first, then capture the exact manual intent generation as
          // required by the independently owned ticket/receipt store.
          setCurrentModel(selection.model)
          setCurrentProvider(selection.provider)
          markComposerSelectionManual()

          if (composerOwner) {
            const stamp = recordComposerModelSelection(captureComposerModelSelection(composerOwner), {
              model: selection.model,
              provider: selection.provider,
              source: 'manual'
            })

            if (stamp) {
              publishedComposerStamp = stamp
              composerRollbacks.set(selectionTarget, { published: stamp, baseline: rollbackComposerStamp })

              while (composerRollbacks.size > 1024) {
                composerRollbacks.delete(composerRollbacks.keys().next().value!)
              }
            }
          }
        }

        updateLiveRuntimeSelection(selection.model, selection.provider)
      }

      const cacheSelection = (provider: string, model: string) => {
        updateModelOptionsCache(
          liveSessionId,
          provider,
          model,
          touchesPrimary && !liveSessionId,
          liveGatewayProfile,
          liveConnectionId
        )
      }

      const stillOwnsPrimarySelection = () =>
        !touchesPrimary ||
        ($activeSessionId.get() === liveSessionId &&
          (!storedSessionId || $selectedStoredSessionId.get() === storedSessionId))

      const rollbackSelection = () => {
        if (!owns('model')) {
          return
        }

        const pending = liveSessionId ? $sessionStates.get()[liveSessionId]?.pendingModelSelection : null

        const currentRollback =
          pending?.model === selection.model && pending.provider === selection.provider ? pending : null

        const appliedRollbackModel = currentRollback?.rollbackModel ?? rollbackModel
        const appliedRollbackProvider = currentRollback?.rollbackProvider ?? rollbackProvider
        const appliedRollbackSource = currentRollback?.rollbackSource ?? rollbackSource

        // Roll back the owning runtime even if its primary surface lost focus
        // while the RPC was pending. That state is separate from the current
        // foreground globals and must not remain as a false applied switch.
        updateLiveRuntimeSelection(appliedRollbackModel, appliedRollbackProvider, false)

        if (touchesPrimary) {
          if (!stillOwnsPrimarySelection() || !owns('model')) {
            return
          }

          if (composerOwner && publishedComposerStamp) {
            const checkpoint = composerRollbacks.get(selectionTarget)

            const previous =
              checkpoint?.published === publishedComposerStamp ? checkpoint.baseline : rollbackComposerStamp

            const restored = restoreComposerModelSelection(
              captureComposerModelSelection(composerOwner),
              previous,
              publishedComposerStamp
            )

            if (!restored) {
              return
            }

            publishedComposerStamp = null
            composerRollbacks.delete(selectionTarget)
          }

          setCurrentModel(appliedRollbackModel)
          setCurrentProvider(appliedRollbackProvider)
          setCurrentModelSource(appliedRollbackSource)
        }

        cacheSelection(appliedRollbackProvider, appliedRollbackModel)
      }

      paintSelection()
      cacheSelection(selection.provider, selection.model)

      // No live session yet: the pick is pure UI state. session.create reads
      // $currentModel/$currentProvider and applies it as that session's override.
      if (!liveSessionId) {
        return true
      }

      // Primary and tile composers both target their owning conversation.
      // Persisted profile defaults belong to Settings → Model; layout identity
      // must not silently widen a session selection into a profile-wide write.
      const scope = '--session'

      const requestSwitch = (confirmExpensiveModel = false) =>
        requestGateway<ModelSwitchResponse>('config.set', {
          session_id: liveSessionId,
          key: 'model',
          value: `${selection.model} --provider ${selection.provider} ${scope}`,
          ...(confirmExpensiveModel ? { confirm_expensive_model: true } : {})
        })

      let recoveryAttempted = false

      const requestSwitchWithRecovery = async (
        confirmExpensiveModel = false
      ): Promise<ModelSwitchResponse | undefined> => {
        try {
          return await requestSwitch(confirmExpensiveModel)
        } catch (error) {
          if (!owns('model')) {
            throw error
          }

          if (
            !isSessionGoneError(error) ||
            recoveryAttempted ||
            !recoverRuntime ||
            !storedSessionId ||
            !liveSessionId
          ) {
            throw error
          }

          recoveryAttempted = true
          const staleRuntimeId = liveSessionId
          const recoveredRuntimeId = await recoverRuntime(storedSessionId, staleRuntimeId)

          if (!owns('model')) {
            throw new ModelSwitchRecoveryAborted()
          }

          // A recovery owner returns null after route drift or a failed durable
          // resume that it already surfaced. Do not roll the old picker back
          // over a newer session or show its stale error toast.
          if (!recoveredRuntimeId || recoveredRuntimeId === staleRuntimeId) {
            throw new ModelSwitchRecoveryAborted()
          }

          liveSessionId = recoveredRuntimeId
          selectionTarget = targetKey(liveSessionId, storedSessionId)
          intentTokens = beginRuntimeOptionIntent(selectionTarget, ['model'])
          // session.resume minted a new runtime slice. Repaint that owner before
          // retrying so the picker never falls back to the pre-switch model in
          // the recovery gap.
          paintSelection()
          cacheSelection(selection.provider, selection.model)

          return requestSwitch(confirmExpensiveModel)
        }
      }

      const finishSwitch = (result: ModelSwitchResponse | undefined) => {
        if (!owns('model')) {
          // A prior pick may be acknowledged while a newer optimistic pick
          // remains pending. Advance only that pick's rollback baseline.
          if (
            result &&
            !result.deferred &&
            liveSessionId &&
            targetKey(liveSessionId, storedSessionId) === selectionTarget
          ) {
            let acknowledgedAfterRollback = false
            let acknowledgedPendingPick = false

            sessionTileDelegate()?.updateSession(liveSessionId, state => {
              const pending = state.pendingModelSelection

              if (pending?.previousModel === selection.model && pending.previousProvider === selection.provider) {
                acknowledgedPendingPick = true

                return {
                  ...state,
                  pendingModelSelection: {
                    ...pending,
                    rollbackModel: selection.model,
                    rollbackProvider: selection.provider,
                    rollbackSource: touchesPrimary ? 'manual' : pending.rollbackSource
                  }
                }
              }

              // The newer pick already rejected and restored this request's
              // baseline. The older acknowledged result can now be painted,
              // but never over another pending or different successful pick.
              if (pending === null && state.model === rollbackModel && state.provider === rollbackProvider) {
                acknowledgedAfterRollback = true

                return { ...state, model: selection.model, provider: selection.provider }
              }

              return state
            })

            // An ACK before the newer rejection proves an exact checkpoint:
            // retain the earlier receipt returned at its original paint. This
            // never publishes a new stamp from reply-time values or globals.
            if (acknowledgedPendingPick && composerOwner && publishedComposerStamp) {
              const checkpoint = composerRollbacks.get(selectionTarget)
              const pending = $sessionStates.get()[liveSessionId]?.pendingModelSelection

              if (
                checkpoint &&
                checkpoint.published === getComposerModelSelection(composerOwner) &&
                checkpoint.published.model === pending?.model &&
                checkpoint.published.provider === pending?.provider &&
                composerOwnerKey(publishedComposerStamp.owner) === composerOwnerKey(composerOwner)
              ) {
                composerRollbacks.set(selectionTarget, { ...checkpoint, baseline: publishedComposerStamp })
              }
            }

            if (acknowledgedAfterRollback) {
              if (
                touchesPrimary &&
                stillOwnsPrimarySelection() &&
                $currentModel.get() === rollbackModel &&
                $currentProvider.get() === rollbackProvider
              ) {
                setCurrentModel(selection.model)
                setCurrentProvider(selection.provider)
                setCurrentModelSource('manual')
                // Preserve the live-runtime acknowledgement. This late result
                // has no proven draft-stamp checkpoint lineage, so it cannot
                // republish a manual stamp over newer owner intent.
              }

              cacheSelection(selection.provider, selection.model)
            }
          }

          return
        }

        // A pick made DURING a turn is queued by the gateway and applied at the
        // next turn start (`deferred`). Re-fetching now would answer with the
        // model still running and repaint the old name over the user's choice —
        // the switch publishes session.info when it lands, and that is what
        // re-syncs every surface.
        if (!result?.deferred) {
          void queryClient.invalidateQueries({
            queryKey: modelOptionsQueryKey(liveGatewayProfile, liveSessionId, liveConnectionId)
          })
        }
      }

      try {
        const result = await requestSwitchWithRecovery()

        if (result?.confirm_required) {
          if (!owns('model')) {
            return false
          }

          rollbackSelection()
          // ONE shared applier for guarded switches (#95293): the same
          // confirm flow the Bots editor routes through — never fork this
          // logic per surface.
          surfaceModelSwitchConfirm({
            confirmLabel: t.common.confirm,
            confirmMessage: result.confirm_message,
            failureMessage: copy.modelSwitchFailed,
            finish: finishSwitch,
            // Staleness guard — the warning can linger while the user picks
            // a different model or switches sessions. Clicking Confirm must
            // not clobber the newer choice: bail if the live state no longer
            // matches the snapshot this notification was created for.
            isStale: () =>
              !owns('model') ||
              (touchesPrimary
                ? !stillOwnsPrimarySelection() ||
                  $currentModel.get() !== prevModel ||
                  $currentProvider.get() !== prevProvider
                : !liveSessionId ||
                  $sessionStates.get()[liveSessionId]?.model !== prevModel ||
                  $sessionStates.get()[liveSessionId]?.provider !== prevProvider),
            repaint: () => {
              paintSelection()
              cacheSelection(selection.provider, selection.model)
            },
            requestConfirmed: () => requestSwitchWithRecovery(true),
            rollback: rollbackSelection
          })

          return false
        }

        finishSwitch(result)

        return true
      } catch (err) {
        if (err instanceof ModelSwitchRecoveryAborted || !owns('model')) {
          return false
        }

        // An OLDER gateway refuses a mid-turn switch outright (4009) instead of
        // deferring it. Don't punish the user for a backend they haven't
        // updated: keep the pick painted as the composer's selection, which is
        // what the NEXT turn runs anyway. Current gateways never take this
        // path — they answer `deferred`.
        if (isBusySessionModelSwitch(err)) {
          return true
        }

        rollbackSelection()
        notifyError(err, copy.modelSwitchFailed)

        return false
      }
    },
    [copy.modelSwitchFailed, queryClient, recoverRuntime, requestGateway, t.common.confirm, updateModelOptionsCache]
  )

  return { applySavedMainModel, refreshCurrentModel, selectModel }
}
