import { type QueryClient } from '@tanstack/react-query'
import { useCallback, useRef } from 'react'

import type { ModelSelection } from '@/app/shell/model-menu-panel'
import { getGlobalModelInfo } from '@/hermes'
import { useI18n } from '@/i18n'
import { isBusySessionModelSwitch } from '@/lib/gateway-rpc'
import { surfaceModelSwitchConfirm } from '@/lib/guarded-model-switch'
import { manualPickRemoved, modelOptionsQueryKey } from '@/lib/model-options'
import { notifyError } from '@/store/notifications'
import { $activeGatewayProfile } from '@/store/profile'
import {
  $activeSessionId,
  $currentModel,
  $currentProvider,
  $selectedStoredSessionId,
  getComposerSelectionGeneration,
  getCurrentModelSource,
  markComposerSelectionManual,
  setCurrentModel,
  setCurrentModelSource,
  setCurrentProvider
} from '@/store/session'
import { isSessionGoneError } from '@/store/session-gone'
import { $sessionStates, sessionTileDelegate } from '@/store/session-states'
import type { ModelOptionsResponse } from '@/types/hermes'

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
      profile = $activeGatewayProfile.get()
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

      queryClient.setQueryData<ModelOptionsResponse>(modelOptionsQueryKey(profile, sessionId), patch)

      if (includeGlobal) {
        queryClient.setQueryData<ModelOptionsResponse>(modelOptionsQueryKey(profile), patch)
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
    (provider: string, model: string) => {
      const liveSessionId = $activeSessionId.get()

      setCurrentModelSource('default')

      if (!liveSessionId) {
        setCurrentProvider(provider)
        setCurrentModel(model)
      }

      // A null session id is the profile-global model-options key. Never patch
      // the live session key here: only config.set --session may change it.
      updateModelOptionsCache(null, provider, model, false)
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

        // A manual pick stays sticky UNLESS it was removed from the catalog (its
        // model no longer exists on the provider), in which case keeping it would
        // 404 every new chat — fall through to reseed from the profile default.
        // Reads the model-options cache the composer already populated; an
        // unknown/not-yet-loaded catalog conservatively preserves the pick.
        const keepManualPick = () => {
          if (force || !$currentModel.get() || getCurrentModelSource() !== 'manual') {
            return false
          }

          const options = queryClient.getQueryData<ModelOptionsResponse>(
            modelOptionsQueryKey($activeGatewayProfile.get())
          )

          return !manualPickRemoved(options?.providers, $currentProvider.get(), $currentModel.get())
        }

        if (keepManualPick()) {
          return
        }

        // Snapshot the selection generation before awaiting so a picker click
        // that lands while getGlobalModelInfo is in flight wins over this older
        // default — value comparisons alone miss re-selecting the same row.
        const selectionGeneration = getComposerSelectionGeneration()
        const result = await getGlobalModelInfo()

        if (
          profileRefreshEpochRef.current !== profileRefreshEpoch ||
          $activeSessionId.get() ||
          getComposerSelectionGeneration() !== selectionGeneration ||
          keepManualPick()
        ) {
          return
        }

        if (typeof result.model === 'string') {
          setCurrentModel(result.model)
        }

        if (typeof result.provider === 'string') {
          setCurrentProvider(result.provider)
        }

        if (typeof result.model === 'string' || typeof result.provider === 'string') {
          setCurrentModelSource('default')
        }
      } catch {
        // The delayed session.info event still updates this once the agent is ready.
      }
    },
    [queryClient]
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

      const prevModel = touchesPrimary ? $currentModel.get() : ($sessionStates.get()[liveSessionId!]?.model ?? '')

      const prevProvider = touchesPrimary
        ? $currentProvider.get()
        : ($sessionStates.get()[liveSessionId!]?.provider ?? '')

      const prevSource = getCurrentModelSource()
      const liveGatewayProfile = $activeGatewayProfile.get()

      // A runtime id is ephemeral. Keep its durable owner while the switch is
      // in flight so a 4001 can be resumed by the correct surface instead of
      // being reported as a model failure.
      const storedSessionId = liveSessionId
        ? ($sessionStates.get()[liveSessionId]?.storedSessionId ??
          (touchesPrimary ? $selectedStoredSessionId.get() : null))
        : null

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
                previousProvider: prevProvider
              }
            : null

          const pendingMatches =
            pendingModelSelection === null
              ? state.pendingModelSelection === null
              : state.pendingModelSelection?.model === pendingModelSelection.model &&
                state.pendingModelSelection.provider === pendingModelSelection.provider &&
                state.pendingModelSelection.previousModel === pendingModelSelection.previousModel &&
                state.pendingModelSelection.previousProvider === pendingModelSelection.previousProvider

          return state.model === model && state.provider === provider && pendingMatches
            ? state
            : { ...state, model, provider, pendingModelSelection }
        })
      }

      const paintSelection = () => {
        if (touchesPrimary) {
          // Retain the draft/global mirror for no-runtime and legacy composer
          // consumers, but it is not the live pane's source of truth.
          setCurrentModel(selection.model)
          setCurrentProvider(selection.provider)
          markComposerSelectionManual()
        }

        updateLiveRuntimeSelection(selection.model, selection.provider)
      }

      const cacheSelection = (provider: string, model: string) => {
        updateModelOptionsCache(liveSessionId, provider, model, touchesPrimary && !liveSessionId, liveGatewayProfile)
      }

      const stillOwnsPrimarySelection = () =>
        !touchesPrimary ||
        ($activeSessionId.get() === liveSessionId &&
          (!storedSessionId || $selectedStoredSessionId.get() === storedSessionId))

      const rollbackSelection = () => {
        // Roll back the owning runtime even if its primary surface lost focus
        // while the RPC was pending. That state is separate from the current
        // foreground globals and must not remain as a false applied switch.
        updateLiveRuntimeSelection(prevModel, prevProvider, false)

        if (touchesPrimary) {
          if (!stillOwnsPrimarySelection()) {
            return
          }

          setCurrentModel(prevModel)
          setCurrentProvider(prevProvider)
          setCurrentModelSource(prevSource)
        }

        cacheSelection(prevProvider, prevModel)
      }

      paintSelection()
      cacheSelection(selection.provider, selection.model)

      // No live session yet: the pick is pure UI state. session.create reads
      // $currentModel/$currentProvider and applies it as that session's override.
      if (!liveSessionId) {
        return true
      }

      // The PRIMARY profile's main agent is the profile's default — its
      // model/provider choice IS the default, so persist it to config.yaml
      // (model.default + model.provider) via --global. This is what makes
      // the selection "stick": a set model.provider outranks a leftover
      // OPENAI_API_KEY env var in resolve_provider(), so the main agent
      // keeps the chosen (e.g. subscription) provider across restarts
      // instead of silently falling back to an env key.
      //
      // Two things stay --session, deliberately:
      //  - a SECONDARY chat tile: picking a model there must not rewrite the
      //    profile default (the cross-session-contamination guard).
      //  - MoA (mixture-of-agents) presets: a transient orchestration choice
      //    that must never become the persisted global gateway default.
      const isSessionOnlyPreset = (selection.provider || '').toLowerCase() === 'moa'
      const persistsAsDefault = touchesPrimary && !isSessionOnlyPreset
      const scope = persistsAsDefault ? '--global' : '--session'

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

          // A recovery owner returns null after route drift or a failed durable
          // resume that it already surfaced. Do not roll the old picker back
          // over a newer session or show its stale error toast.
          if (!recoveredRuntimeId || recoveredRuntimeId === staleRuntimeId) {
            throw new ModelSwitchRecoveryAborted()
          }

          liveSessionId = recoveredRuntimeId
          // session.resume minted a new runtime slice. Repaint that owner before
          // retrying so the picker never falls back to the pre-switch model in
          // the recovery gap.
          paintSelection()
          cacheSelection(selection.provider, selection.model)

          return requestSwitch(confirmExpensiveModel)
        }
      }

      const finishSwitch = (result: ModelSwitchResponse | undefined) => {
        // A pick made DURING a turn is queued by the gateway and applied at the
        // next turn start (`deferred`). Re-fetching now would answer with the
        // model still running and repaint the old name over the user's choice —
        // the switch publishes session.info when it lands, and that is what
        // re-syncs every surface.
        if (!result?.deferred) {
          void queryClient.invalidateQueries({ queryKey: modelOptionsQueryKey(liveGatewayProfile, liveSessionId) })
        }
      }

      try {
        const result = await requestSwitchWithRecovery()

        if (result?.confirm_required) {
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
              touchesPrimary
                ? !stillOwnsPrimarySelection() ||
                  $currentModel.get() !== prevModel ||
                  $currentProvider.get() !== prevProvider
                : !liveSessionId ||
                  $sessionStates.get()[liveSessionId]?.model !== prevModel ||
                  $sessionStates.get()[liveSessionId]?.provider !== prevProvider,
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
        if (err instanceof ModelSwitchRecoveryAborted) {
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
