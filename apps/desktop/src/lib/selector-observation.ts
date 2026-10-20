import type { ClientSessionState } from '@/app/types'
import type { SessionRuntimeInfo } from '@/types/hermes'

export interface SelectorVersion {
  epoch: string
  revision: number
}
export type SelectorFields = Pick<ClientSessionState, 'model' | 'provider' | 'reasoningEffort' | 'serviceTier' | 'fast'>
export interface SelectorObservation {
  version: SelectorVersion
  values: Partial<SelectorFields>
}
type SelectorInfo = Omit<
  Pick<
    SessionRuntimeInfo,
    'model' | 'provider' | 'reasoning_effort' | 'service_tier' | 'fast' | 'pending_runtime_options'
  >,
  never
> & { selector_version?: unknown }

export function validSelectorVersion(value: unknown): value is SelectorVersion {
  if (!value || typeof value !== 'object') {
    return false
  }

  const stamp = value as SelectorVersion

  return (
    typeof stamp.epoch === 'string' &&
    stamp.epoch.length > 0 &&
    Number.isSafeInteger(stamp.revision) &&
    stamp.revision > 0
  )
}

export function selectorFields(info: SelectorInfo): Partial<SelectorFields> {
  return {
    ...(typeof info.model === 'string' ? { model: info.model } : {}),
    ...(typeof info.provider === 'string' ? { provider: info.provider } : {}),
    ...(typeof info.reasoning_effort === 'string' ? { reasoningEffort: info.reasoning_effort } : {}),
    ...(typeof info.service_tier === 'string' ? { serviceTier: info.service_tier } : {}),
    ...(typeof info.fast === 'boolean' ? { fast: info.fast } : {})
  }
}

/** One gate for event, correlated ACK and explicit resume/activation readback.
 * Displayed optimistic state is not an observation. Preserve an admitted newer
 * observation behind a pending local overlay, then reveal it when that exact
 * request settles. Unstamped legacy reads cannot downgrade an ordered owner.
 */
export function reconcileSelectorInfo(
  state: ClientSessionState,
  info: SelectorInfo,
  options: { settleModel?: boolean; settleOptions?: readonly ('effort' | 'fast')[] } = {}
): Partial<ClientSessionState> {
  const stamp = info.selector_version
  const prior = state.selectorObservation
  const incoming = selectorFields(info)
  let observation = prior
  let accepted = false

  if (prior && (!validSelectorVersion(stamp) || stamp.epoch !== prior.version.epoch)) {
    return {}
  }

  if (validSelectorVersion(stamp)) {
    const sameOwner = !prior || stamp.epoch === prior.version.epoch

    if (sameOwner && (!prior || stamp.revision >= prior.version.revision)) {
      const conflict =
        prior &&
        sameOwner &&
        stamp.revision === prior.version.revision &&
        Object.entries(incoming).some(
          ([key, value]) => key in prior.values && prior.values[key as keyof SelectorFields] !== value
        )

      if (!conflict) {
        observation = { version: stamp, values: { ...(sameOwner ? prior?.values : {}), ...incoming } }
        accepted = true
      }
    }
  } else if (stamp === undefined && !prior) {
    // Legacy capability only: do not claim ordering. Preserve pending intent;
    // once it settles, genuine backend changes retain the established behavior.
    accepted = true
  }

  let pendingModel = state.pendingModelSelection
  let pendingOptions = state.pendingRuntimeOptions
  const values = observation?.values ?? (accepted ? incoming : {})

  if (
    accepted &&
    stamp === undefined &&
    !prior &&
    pendingModel &&
    (incoming.model !== undefined || incoming.provider !== undefined)
  ) {
    // Unversioned observations retain the existing normalization behavior
    // only before an ordered owner is admitted. Known previous/baseline pairs
    // are protected while pending; this does not qualify legacy ordering.
    const repeats = (model?: string, provider?: string) =>
      (incoming.model === undefined || incoming.model === model) &&
      (incoming.provider === undefined || incoming.provider === provider)

    if (
      !repeats(pendingModel.previousModel, pendingModel.previousProvider) &&
      !repeats(pendingModel.rollbackModel, pendingModel.rollbackProvider)
    ) {
      pendingModel = null
    }
  }

  if (
    options.settleModel ||
    (accepted && pendingModel && values.model === pendingModel.model && values.provider === pendingModel.provider)
  ) {
    pendingModel = null
  }

  if (options.settleOptions && pendingOptions) {
    pendingOptions = { ...pendingOptions }

    for (const dimension of options.settleOptions) {
      delete pendingOptions[dimension]
    }

    if (Object.keys(pendingOptions).length === 0) {
      pendingOptions = null
    }
  }

  if (
    accepted &&
    pendingOptions &&
    info.pending_runtime_options === null &&
    (pendingOptions.effort === undefined || values.reasoningEffort === pendingOptions.effort) &&
    (pendingOptions.fast === undefined || values.fast === pendingOptions.fast)
  ) {
    pendingOptions = null
  }

  const paint = { ...values }

  if (pendingModel) {
    delete paint.model
    delete paint.provider
  }

  if (pendingOptions?.effort !== undefined) {
    delete paint.reasoningEffort
  }

  if (pendingOptions?.fast !== undefined) {
    delete paint.fast
    delete paint.serviceTier
  }

  if (!accepted && !options.settleModel && !options.settleOptions) {
    return {}
  }

  return {
    ...paint,
    ...(observation ? { selectorObservation: observation } : {}),
    pendingModelSelection: pendingModel,
    pendingRuntimeOptions: pendingOptions
  }
}
