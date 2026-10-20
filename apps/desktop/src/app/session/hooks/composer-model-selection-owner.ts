import { getApiRequestConnection, getApiRequestProfile } from '@/api/client'
import { $activeGatewayProfile, $newChatProfile, resolveNewChatOwnerRoute } from '@/store/profile'
import {
  beginRuntimeOptionIntent,
  captureComposerModelSelection,
  type ComposerModelOwner,
  ownsRuntimeOptionIntent
} from '@/store/session'
import type { SessionOwnerScope } from '@/store/session-request-router'

// Structural contract shared with the independently owned core store. This
// producer file never infers selection ownership from the legacy mirror atoms.
export type ComposerSelectionOwner = Readonly<ComposerModelOwner>

export interface MainModelSaveOrigin {
  readonly owner: ComposerSelectionOwner
  readonly startedAtGeneration: number
  readonly saveIntent: Readonly<{ target: string; token: number }>
}

export interface MainModelSavedChange extends MainModelSaveOrigin {
  readonly model: string
  readonly provider: string
}

export type OnMainModelChanged = (change: MainModelSavedChange) => void

export function composerOwnerKey(owner: ComposerSelectionOwner): string {
  return JSON.stringify([owner.connectionId, owner.profile, owner.targetProfile || owner.profile])
}

export function composerOwnerForSession(owner: SessionOwnerScope): ComposerSelectionOwner | null {
  if (typeof owner === 'string') {
    return { connectionId: null, profile: owner.trim() || 'default' }
  }

  if (!owner?.connectionId?.trim()) {
    return null
  }

  return {
    connectionId: owner.connectionId.trim(),
    profile: owner.profile.trim() || 'default',
    ...(owner.targetProfile?.trim() ? { targetProfile: owner.targetProfile.trim() } : {})
  }
}

export function captureDraftComposerOwner(): ComposerSelectionOwner {
  return (
    composerOwnerForSession(resolveNewChatOwnerRoute()) || {
      connectionId: getApiRequestConnection(),
      profile: ($newChatProfile.get() || $activeGatewayProfile.get()).trim() || 'default'
    }
  )
}

export function captureModelRequestOwner(scopeProfile?: string): ComposerSelectionOwner {
  const connectionId = getApiRequestConnection()
  const profile = (scopeProfile || getApiRequestProfile() || $activeGatewayProfile.get()).trim() || 'default'
  const draftRoute = composerOwnerForSession(resolveNewChatOwnerRoute())

  // A pending draft on another source/profile cannot label this active REST
  // request. Retain a distinct backend target only for the matching route.
  return draftRoute?.connectionId === connectionId && draftRoute.profile === profile
    ? { ...draftRoute }
    : { connectionId, profile }
}

export function beginMainModelSave(scopeProfile?: string): MainModelSaveOrigin {
  const owner = captureModelRequestOwner(scopeProfile)
  const ticket = captureComposerModelSelection(owner)
  const target = ticket.ownerKey
  const token = beginRuntimeOptionIntent(target, ['profile-default-save'])['profile-default-save']

  return { owner, startedAtGeneration: ticket.selectionGeneration, saveIntent: { target, token } }
}

export function ownsMainModelSave(origin: MainModelSaveOrigin): boolean {
  return ownsRuntimeOptionIntent(origin.saveIntent.target, 'profile-default-save', origin.saveIntent.token)
}

export function isMainModelSaveOriginCurrent(origin: MainModelSaveOrigin, scopeProfile?: string): boolean {
  return (
    ownsMainModelSave(origin) &&
    composerOwnerKey(captureModelRequestOwner(scopeProfile)) === composerOwnerKey(origin.owner)
  )
}
