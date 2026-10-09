import { useStore } from '@nanostores/react'
import { useCallback, useLayoutEffect, useRef, useState } from 'react'

import {
  captureDraftComposerOwner,
  captureModelRequestOwner,
  composerOwnerKey,
  type ComposerSelectionOwner
} from '@/app/session/hooks/composer-model-selection-owner'
import { $activeGatewayProfile, $newChatConnectionId, $newChatProfile, $newChatRoute } from '@/store/profile'
import { $connection } from '@/store/session'

// These atoms publish rehomes; canonical capture still owns route precedence.
// Do not infer model authority from the mirrored model/provider atoms here.
function useModelOwnerChanges() {
  useStore($connection)
  useStore($activeGatewayProfile)
  useStore($newChatConnectionId)
  useStore($newChatProfile)
  useStore($newChatRoute)
}

export function useModelRequestOwner(scopeProfile?: string): ComposerSelectionOwner {
  // Canonical capture reads imperative stores; it must run on every rehome.
  'use no memo'

  useModelOwnerChanges()

  return captureModelRequestOwner(scopeProfile)
}

export function useDraftComposerOwner(): ComposerSelectionOwner {
  'use no memo'

  useModelOwnerChanges()

  return captureDraftComposerOwner()
}

/** Form lifecycle only: a batched round trip still needs a fresh form. */
export function useModelFormKey(owner: ComposerSelectionOwner, scopeProfile?: string): string {
  const [revision, setRevision] = useState(0)
  useLayoutEffect(() => {
    let previousKey = composerOwnerKey(captureModelRequestOwner(scopeProfile))

    const rehome = () => {
      const nextKey = composerOwnerKey(captureModelRequestOwner(scopeProfile))

      if (nextKey !== previousKey) {
        previousKey = nextKey
        setRevision(value => value + 1)
      }
    }

    const unlisten = [$connection, $activeGatewayProfile, $newChatConnectionId, $newChatProfile, $newChatRoute]
      .map(store => store.listen(rehome))

    return () => { unlisten.forEach(stop => stop()) }
  }, [scopeProfile])

  return JSON.stringify([composerOwnerKey(owner), revision])
}

/** A remounted form cannot regain permission when the user returns A→B→A. */
export function useModelOwnerIsCurrent(owner: ComposerSelectionOwner, scopeProfile?: string): () => boolean {
  const ownerKey = composerOwnerKey(owner)
  const mounted = useRef(true)
  useLayoutEffect(() => {
    mounted.current = true

    const invalidate = () => {
      if (composerOwnerKey(captureModelRequestOwner(scopeProfile)) !== ownerKey) {
        mounted.current = false
      }
    }

    // Observe the transition itself, even when React batches A→B→A into one
    // paint. This lease cannot become valid again before a fresh form mount.
    const unlisten = [$connection, $activeGatewayProfile, $newChatConnectionId, $newChatProfile, $newChatRoute]
      .map(store => store.listen(invalidate))

    return () => {
      mounted.current = false
      unlisten.forEach(stop => stop())
    }
  }, [ownerKey, scopeProfile])

  return useCallback(
    () => mounted.current && composerOwnerKey(captureModelRequestOwner(scopeProfile)) === ownerKey,
    [ownerKey, scopeProfile]
  )
}

export function requireCurrentModelOwner(isCurrent: () => boolean): void {
  if (!isCurrent()) {
    throw new Error('Model settings target changed. Reopen the selection before saving.')
  }
}
