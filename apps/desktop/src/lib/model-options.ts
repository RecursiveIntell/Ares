import { getApiRequestConnection, getApiRequestProfile, type ProfileScope } from '@/api/client'
import { getGlobalModelOptions, type HermesGateway, type ModelOptionsResponse } from '@/hermes'
import type { ModelOptionProvider } from '@/types/hermes'

/** Catalog absence is a visible configuration gap, never a new selection. */
export function selectionUnavailable(
  providers: ModelOptionProvider[] | undefined,
  provider: string,
  model: string
): boolean {
  if (!providers || !provider || !model) {
    return false
  }

  const row = providers.find(p => p.slug === provider || p.name === provider || p.aliases?.includes(provider))

  return !row || !(row.models ?? []).includes(model) || (row.unavailable_models ?? []).includes(model)
}

interface ModelOptionsRequest {
  connectionId?: null | string
  /** When false, include ambient/unconfigured providers (onboarding/setup
   *  surfaces). Chat pickers default to true so only explicitly configured
   *  providers are listed (#56974). */
  explicitOnly?: boolean
  gateway?: HermesGateway
  /** Owner-routed RPC. When set, catalog reads hit this dispatcher instead of
   *  `gateway.request` — a tile's model menu must not query the ambient
   *  chrome socket (#93892). */
  request?: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
  /** Profile for both catalog paths. Must match the catalog owner so a
   *  secondary tile does not fall back to the launch profile's models. */
  profile?: null | string
  refresh?: boolean
  sessionId?: null | string
}

export function modelOptionsQueryKey(
  profile: null | string | undefined,
  sessionId?: null | string,
  connectionId = getApiRequestConnection()
) {
  const profileKey = (profile ?? '').trim() || 'default'

  const sourceKey = connectionId && connectionId !== 'local' ? `${connectionId}::${profileKey}` : profileKey

  return ['model-options', sourceKey, sessionId || 'global'] as const
}

function hasSelectableModels(options: ModelOptionsResponse | null | undefined): boolean {
  return options?.providers?.some(provider => (provider.models?.length ?? 0) > 0) ?? false
}

function restModelOptions(
  explicitOnly: boolean,
  refresh: boolean,
  profile: ProfileScope
): Promise<ModelOptionsResponse> {
  const opts = { explicitOnly, ...(refresh ? { refresh: true } : {}) }
  return getGlobalModelOptions(opts, profile)
}

export async function requestModelOptions({
  connectionId = getApiRequestConnection(),
  explicitOnly = true,
  gateway,
  profile,
  refresh = false,
  request,
  sessionId
}: ModelOptionsRequest): Promise<ModelOptionsResponse> {
  // Capture the owner before either async leg; foreground source changes must
  // not redirect a late REST recovery into another profile or connection.
  const scope = { connectionId: connectionId || 'local', profile: profile ?? getApiRequestProfile() ?? 'default' }
  const dispatch = request ?? (gateway ? gateway.request.bind(gateway) : null)

  if (dispatch) {
    const params: Record<string, unknown> = { profile: scope.profile }

    if (sessionId) {
      params.session_id = sessionId
    }

    if (refresh) {
      params.refresh = true
    }

    if (explicitOnly) {
      params.explicit_only = true
    }

    let gatewayError: unknown
    let gatewayOptions: ModelOptionsResponse | undefined

    try {
      gatewayOptions = await dispatch<ModelOptionsResponse>('model.options', params)
    } catch (error) {
      gatewayError = error
    }

    if (gatewayOptions && hasSelectableModels(gatewayOptions)) {
      return gatewayOptions
    }

    // A connected Desktop gateway can occasionally return only the current
    // provider/model (or an empty provider list) while its authenticated REST
    // catalog is already populated. Recover through the same profile-scoped
    // endpoint Settings uses, but keep the live session selection authoritative.
    try {
      const restOptions = await restModelOptions(explicitOnly, refresh, scope)

      if (hasSelectableModels(restOptions)) {
        return {
          ...restOptions,
          ...(gatewayOptions?.provider ? { provider: gatewayOptions.provider } : {}),
          ...(gatewayOptions?.model ? { model: gatewayOptions.model } : {})
        }
      }
    } catch {
      // Preserve the gateway result (or its original error) when the recovery
      // path is unavailable.
    }

    if (gatewayOptions) {
      return gatewayOptions
    }

    throw gatewayError
  }

  return restModelOptions(explicitOnly, refresh, scope)
}
