import { beforeEach, describe, expect, it } from 'vitest'

import {
  _resetComposerModelSelectionsForTests,
  beginRuntimeOptionIntent,
  captureComposerModelSelection,
  type ComposerModelOwner,
  getComposerModelSelection,
  markComposerSelectionManual,
  ownsRuntimeOptionIntent,
  recordComposerModelSelection,
  restoreComposerModelSelection,
  setComposerModelSelectionOwner,
  setCurrentModel,
  setCurrentProvider
} from './session'

const a: ComposerModelOwner = { connectionId: 'source-a', profile: 'worker', targetProfile: 'backend-worker' }
const b: ComposerModelOwner = { connectionId: 'source-b', profile: 'worker', targetProfile: 'backend-worker' }
const pair = { model: 'model-a', provider: 'provider-a', source: 'default' as const }

describe('owner-qualified composer selections', () => {
  beforeEach(() => _resetComposerModelSelectionsForTests())

  it('never adopts an ambient or persisted pair for an unstamped owner', () => {
    setCurrentModel('model-a')
    setCurrentProvider('provider-a')
    setComposerModelSelectionOwner(b)
    expect(getComposerModelSelection(b)).toBeNull()
  })

  it('keeps connection, logical profile and backend target distinct', () => {
    setComposerModelSelectionOwner(a)
    const value = recordComposerModelSelection(captureComposerModelSelection(a), pair)
    expect(getComposerModelSelection(a)).toBe(value)
    expect(getComposerModelSelection(b)).toBeNull()
    expect(getComposerModelSelection({ ...a, profile: 'alias' })).toBeNull()
    expect(getComposerModelSelection({ ...a, targetProfile: 'other-target' })).toBeNull()
    expect(value?.selectionGeneration).toBeTypeOf('number')
  })

  it('refuses incomplete defaults and an unspecified connection', () => {
    setComposerModelSelectionOwner(a)
    expect(recordComposerModelSelection(captureComposerModelSelection(a), { ...pair, provider: '' })).toBeNull()
    expect(() => captureComposerModelSelection({ ...a, connectionId: '' })).toThrow('explicit connection owner')
    expect(getComposerModelSelection(a)).toBeNull()
  })

  it('drops delayed replies from old owners and from an A→B→A epoch', () => {
    setComposerModelSelectionOwner(a)
    const oldA = captureComposerModelSelection(a)
    setComposerModelSelectionOwner(b)
    const oldB = captureComposerModelSelection(b)
    expect(recordComposerModelSelection(oldA, pair)).toBeNull()
    setComposerModelSelectionOwner(a)
    expect(recordComposerModelSelection(oldA, pair)).toBeNull()
    expect(recordComposerModelSelection(oldB, pair)).toBeNull()
    expect(recordComposerModelSelection(captureComposerModelSelection(a), pair)).not.toBeNull()
  })

  it('lets only the latest same-owner request record a default', () => {
    setComposerModelSelectionOwner(a)
    const old = captureComposerModelSelection(a)
    const latest = captureComposerModelSelection(a)
    expect(recordComposerModelSelection(old, pair)).toBeNull()
    expect(recordComposerModelSelection(latest, { ...pair, model: 'latest' })?.model).toBe('latest')
  })

  it('preserves a deliberate owner pin before and after default replies, including same-value reselect', () => {
    setComposerModelSelectionOwner(a)
    const old = captureComposerModelSelection(a)
    markComposerSelectionManual()
    const manual = recordComposerModelSelection(captureComposerModelSelection(a), { ...pair, source: 'manual' })
    expect(recordComposerModelSelection(old, pair)).toBeNull()
    expect(recordComposerModelSelection(captureComposerModelSelection(a), pair)).toBeNull()
    setComposerModelSelectionOwner(b)
    recordComposerModelSelection(captureComposerModelSelection(b), { ...pair, model: 'model-b' })
    setComposerModelSelectionOwner(a)
    expect(getComposerModelSelection(a)).toBe(manual)
    expect(getComposerModelSelection(b)).toBeNull()
    expect(recordComposerModelSelection(captureComposerModelSelection(a), pair)).toBeNull()
  })

  it('records a live tile only under its own route without changing the foreground draft', () => {
    setComposerModelSelectionOwner(b)
    markComposerSelectionManual()
    recordComposerModelSelection(captureComposerModelSelection(a), { ...pair, source: 'manual' })
    expect(getComposerModelSelection(b)).toBeNull()
    expect(recordComposerModelSelection(captureComposerModelSelection(b), pair)).not.toBeNull()
    expect(getComposerModelSelection(a)?.source).toBe('manual')
  })

  it('restores the prior complete default/source only while the rejected receipt still owns the slot', () => {
    setComposerModelSelectionOwner(a)
    const previous = recordComposerModelSelection(captureComposerModelSelection(a), pair)!
    markComposerSelectionManual()
    const rejected = recordComposerModelSelection(captureComposerModelSelection(a), {
      ...pair,
      model: 'rejected',
      source: 'manual'
    })!
    expect(restoreComposerModelSelection(captureComposerModelSelection(a), previous, rejected)).toBe(true)
    expect(getComposerModelSelection(a)).toMatchObject(pair)
    markComposerSelectionManual()
    const newest = recordComposerModelSelection(captureComposerModelSelection(a), {
      ...pair,
      model: 'newest',
      source: 'manual'
    })!
    expect(restoreComposerModelSelection(captureComposerModelSelection(a), previous, rejected)).toBe(false)
    expect(getComposerModelSelection(a)).toBe(newest)
    expect(restoreComposerModelSelection(captureComposerModelSelection(b), previous, newest)).toBe(false)
  })

  it('keeps stale requests invalid after a missing-baseline rollback without blocking a fresh default', () => {
    setComposerModelSelectionOwner(a)
    const stale = captureComposerModelSelection(a)
    markComposerSelectionManual()
    const rejected = recordComposerModelSelection(captureComposerModelSelection(a), { ...pair, source: 'manual' })!
    expect(restoreComposerModelSelection(captureComposerModelSelection(a), null, rejected)).toBe(true)
    expect(getComposerModelSelection(a)).toBeNull()
    expect(recordComposerModelSelection(stale, pair)).toBeNull()
    expect(recordComposerModelSelection(captureComposerModelSelection(a), pair)).not.toBeNull()
  })

  it('lets the latest confirmed save supersede a weaker default read using the shared save intent', () => {
    setComposerModelSelectionOwner(a)
    const origin = captureComposerModelSelection(a)
    const token = beginRuntimeOptionIntent(origin.ownerKey, ['profile-default-save'])['profile-default-save']
    const weakRead = captureComposerModelSelection(a)
    recordComposerModelSelection(weakRead, { ...pair, model: 'older-default' })
    expect(ownsRuntimeOptionIntent(origin.ownerKey, 'profile-default-save', token)).toBe(true)
    const confirmed = recordComposerModelSelection(captureComposerModelSelection(origin.owner), {
      ...pair,
      model: 'saved-default'
    })
    expect(getComposerModelSelection(a)).toBe(confirmed)
    expect(confirmed?.model).toBe('saved-default')
    expect(recordComposerModelSelection(weakRead, pair)).toBeNull()
  })

  it('preserves a later equal-value manual selection when a current save confirms', () => {
    setComposerModelSelectionOwner(a)
    const origin = captureComposerModelSelection(a)
    const token = beginRuntimeOptionIntent(origin.ownerKey, ['profile-default-save'])['profile-default-save']
    markComposerSelectionManual()
    const manual = recordComposerModelSelection(captureComposerModelSelection(a), { ...pair, source: 'manual' })
    expect(ownsRuntimeOptionIntent(origin.ownerKey, 'profile-default-save', token)).toBe(true)
    expect(recordComposerModelSelection(captureComposerModelSelection(origin.owner), pair)).toBeNull()
    expect(getComposerModelSelection(a)).toBe(manual)
  })

  it('keeps the known selection on save failure and rejects an older save callback after a newer intent fails', () => {
    setComposerModelSelectionOwner(a)
    const known = recordComposerModelSelection(captureComposerModelSelection(a), pair)
    const origin = captureComposerModelSelection(a)
    const older = beginRuntimeOptionIntent(origin.ownerKey, ['profile-default-save'])['profile-default-save']
    const newer = beginRuntimeOptionIntent(origin.ownerKey, ['profile-default-save'])['profile-default-save']
    // The failed newest save never publishes a receipt; the shared namespace
    // still proves that the older success may not become a fresh UI intent.
    expect(ownsRuntimeOptionIntent(origin.ownerKey, 'profile-default-save', older)).toBe(false)
    expect(ownsRuntimeOptionIntent(origin.ownerKey, 'profile-default-save', newer)).toBe(true)
    expect(getComposerModelSelection(a)).toBe(known)
  })
})
