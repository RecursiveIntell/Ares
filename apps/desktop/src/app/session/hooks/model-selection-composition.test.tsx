import { useStore } from '@nanostores/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useEffect, useRef, useState } from 'react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { getApiRequestConnection, setApiRequestConnection, setApiRequestProfile } from '@/api/client'
import { ModelSettings } from '@/app/settings/model-settings'
import type { ClientSessionState } from '@/app/types'
import {
  getAuxiliaryModels,
  getGlobalModelInfo,
  getGlobalModelOptions,
  getHermesConfigRecord,
  getMoaModels,
  setModelAssignment
} from '@/hermes'
import { createClientSessionState } from '@/lib/chat-runtime'
import { requestGatewayForAgent } from '@/store/gateway'
import { $notifications, clearNotifications } from '@/store/notifications'
import {
  $activeGatewayProfile,
  $newChatProfile,
  $newChatRoute,
  ensureGatewayAgent,
  ensureGatewayProfile
} from '@/store/profile'
import { $projectScope, ALL_PROJECTS } from '@/store/projects'
import {
  $activeSessionId,
  $currentModel,
  $currentProvider,
  $selectedStoredSessionId,
  _resetComposerModelSelectionsForTests,
  getComposerModelSelection,
  setActiveSessionId,
  setCurrentModel,
  setCurrentModelSource,
  setCurrentProvider,
  setNewChatWorkspaceTarget,
  setSelectedStoredSessionId,
  setSessionOwnerHint
} from '@/store/session'
import {
  $sessionStates,
  publishSessionState,
  type SessionTileDelegate,
  setSessionTileDelegate
} from '@/store/session-states'

import { deferred } from '../../../test/deferred'

import { useModelControls } from './use-model-controls'
import { useSessionActions } from './use-session-actions'

// Both hooks, Settings, route capture, selection store and the assignment
// wrapper are real. Only bridge/catalog/transport replies are inert.
vi.mock('@/hermes', async original => ({
  ...(await original<Record<string, unknown>>()),
  getGlobalModelInfo: vi.fn(),
  getGlobalModelOptions: vi.fn(),
  getAuxiliaryModels: vi.fn(),
  getMoaModels: vi.fn(),
  getHermesConfigRecord: vi.fn(),
  setModelAssignment: vi.fn()
}))
vi.mock('@/store/profile', async original => ({
  ...(await original<Record<string, unknown>>()),
  ensureGatewayAgent: vi.fn().mockResolvedValue(undefined),
  ensureGatewayProfile: vi.fn().mockResolvedValue(undefined)
}))
vi.mock('@/store/gateway', async original => ({
  ...(await original<Record<string, unknown>>()),
  requestGatewayForAgent: vi.fn(),
  retainGatewayForAgent: vi.fn(async () => () => undefined)
}))

type Handle = {
  controls: ReturnType<typeof useModelControls>
  actions: ReturnType<typeof useSessionActions>
}

function Harness({
  onReady,
  request,
  settings = 0
}: {
  onReady: (value: Handle) => void
  request: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
  settings?: number
}) {
  const [client] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false } } }))
  const active = useStore($activeSessionId)
  const selected = useStore($selectedStoredSessionId)
  const activeRef = useRef(active)
  const selectedRef = useRef(selected)
  activeRef.current = active
  selectedRef.current = selected
  const controls = useModelControls({ queryClient: client, requestGateway: request })

  const actions = useSessionActions({
    activeSessionId: active,
    activeSessionIdRef: activeRef,
    busyRef: useRef(false),
    creatingSessionRef: useRef(false),
    selectedStoredSessionId: selected,
    selectedStoredSessionIdRef: selectedRef,
    getRouteToken: () => 'owned-composition-route',
    getRoutedStoredSessionId: () => null,
    navigate: vi.fn(),
    requestGateway: request,
    resetViewSync: vi.fn(),
    ensureSessionState: () => ({}) as ClientSessionState,
    runtimeIdByStoredSessionIdRef: useRef(new Map()),
    sessionStateByRuntimeIdRef: useRef(new Map()),
    syncSessionStateToView: vi.fn(),
    updateSessionState: () => ({}) as ClientSessionState
  })

  useEffect(() => onReady({ controls, actions }), [actions, controls, onReady])

  return (
    <MemoryRouter>
      <QueryClientProvider client={client}>
        {Array.from({ length: settings }, (_, index) => (
          <div data-testid={`settings-${index}`} key={index}>
            <ModelSettings onMainModelChanged={controls.applySavedMainModel} />
          </div>
        ))}
      </QueryClientProvider>
    </MemoryRouter>
  )
}

const a = { connectionId: 'source-a', profile: 'worker', targetProfile: 'backend-a' }
const b = { connectionId: 'source-b', profile: 'worker', targetProfile: 'backend-b' }
const defaultA = { model: 'default-a', provider: 'provider-a' }
const defaultB = { model: 'default-b', provider: 'provider-b' }
let wireA = defaultA
let wireB = defaultB

function route(owner: typeof a | null, active = true) {
  $newChatRoute.set(owner)
  $newChatProfile.set(owner?.profile ?? 'default')

  if (active) {
    $activeGatewayProfile.set(owner?.profile ?? 'default')
    setApiRequestConnection(owner?.connectionId ?? null)
    setApiRequestProfile(owner?.profile ?? 'default')
  }
}

async function setup(settings = 0) {
  let handle!: Handle

  const request = vi.fn(async (method: string) =>
    method === 'session.create' ? { session_id: 'composition-created', stored_session_id: null } : {}
  )

  render(
    <Harness
      onReady={value => {
        handle = value
      }}
      request={request as never}
      settings={settings}
    />
  )
  await waitFor(() => expect(handle).toBeDefined())

  return {
    get handle() {
      return handle
    },
    request
  }
}

async function seed(value: Awaited<ReturnType<typeof setup>>) {
  await act(async () => {
    await value.handle.controls.refreshCurrentModel(true)
  })
  expect(getComposerModelSelection(a)).toMatchObject(defaultA)
}

async function send(value: Awaited<ReturnType<typeof setup>>) {
  let result: null | string = null
  await act(async () => {
    result = await value.handle.actions.createBackendSessionForSend('offline input')
  })

  return result
}

function expectCreate(owner: typeof a, pair: typeof defaultA) {
  expect(requestGatewayForAgent).toHaveBeenCalledWith(
    owner.connectionId,
    owner.profile,
    'session.create',
    expect.objectContaining({ profile: owner.targetProfile, model: pair.model, provider: pair.provider })
  )
}

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

beforeEach(() => {
  cleanup()
  vi.clearAllMocks()
  _resetComposerModelSelectionsForTests()
  clearNotifications()
  $sessionStates.set({})
  setSessionTileDelegate({
    updateSession: (id, updater) => {
      const state = updater($sessionStates.get()[id])
      publishSessionState(id, state)

      return state
    }
  } as SessionTileDelegate)
  setActiveSessionId(null)
  setSelectedStoredSessionId(null)
  setNewChatWorkspaceTarget(undefined)
  $projectScope.set(ALL_PROJECTS)
  setCurrentModel('')
  setCurrentProvider('')
  setCurrentModelSource('')
  route(a)
  wireA = defaultA
  wireB = defaultB
  vi.mocked(ensureGatewayAgent).mockReset().mockResolvedValue(undefined)
  vi.mocked(ensureGatewayProfile).mockReset().mockResolvedValue(undefined)
  vi.mocked(requestGatewayForAgent)
    .mockReset()
    .mockResolvedValue({ session_id: 'composition-created', stored_session_id: null } as never)
  vi.mocked(getGlobalModelInfo)
    .mockReset()
    .mockImplementation(async scope =>
      (scope && typeof scope === 'object' ? scope.connectionId : getApiRequestConnection()) === b.connectionId
        ? wireB
        : wireA
    )
  vi.mocked(getGlobalModelOptions)
    .mockReset()
    .mockResolvedValue({
      providers: [
        {
          slug: 'provider-a',
          name: 'Offline A',
          authenticated: true,
          models: ['default-a', 'saved-a', 'manual-a']
        }
      ]
    } as never)
  vi.mocked(getAuxiliaryModels)
    .mockReset()
    .mockResolvedValue({ main: defaultA, tasks: [] } as never)
  vi.mocked(getMoaModels)
    .mockReset()
    .mockResolvedValue(null as never)
  vi.mocked(getHermesConfigRecord)
    .mockReset()
    .mockResolvedValue({ agent: { reasoning_effort: 'medium' } } as never)
  vi.mocked(setModelAssignment).mockReset()
})

afterEach(() => {
  cleanup()
  clearNotifications()
  route(null)
  setActiveSessionId(null)
  setSelectedStoredSessionId(null)
  $sessionStates.set({})
})

describe('actual producer/store/admission composition', () => {
  it('uses the same explicit null owner through legacy default production and Send', async () => {
    route(null)
    const value = await setup()
    await act(async () => {
      await value.handle.controls.refreshCurrentModel(true)
    })
    expect(getComposerModelSelection({ connectionId: null, profile: 'default' })).toMatchObject(defaultA)
    expect(await send(value)).toBe('composition-created')
    expect(value.request).toHaveBeenCalledWith('session.create', expect.objectContaining(defaultA))
  })

  it('refuses an unreadable B admission, ignores delayed A, then admits the actual B default', async () => {
    const value = await setup()
    const oldA = deferred<typeof defaultA>()
    vi.mocked(getGlobalModelInfo).mockReturnValueOnce(oldA.promise)
    let old!: Promise<void>
    act(() => {
      old = value.handle.controls.refreshCurrentModel(true)
    })
    act(() => route(b, false))
    await act(async () => {
      await value.handle.controls.refreshCurrentModel(true)
    })
    vi.mocked(getGlobalModelInfo).mockResolvedValueOnce({ model: '', provider: '' })
    expect(await send(value)).toBeNull()
    expect(requestGatewayForAgent).not.toHaveBeenCalled()
    expect(ensureGatewayAgent).not.toHaveBeenCalled()
    expect(getComposerModelSelection(b)).toBeNull()
    act(() => route(b))
    const newB = deferred<typeof defaultB>()
    vi.mocked(getGlobalModelInfo).mockReturnValueOnce(newB.promise)
    let fresh!: Promise<void>
    act(() => {
      fresh = value.handle.controls.refreshCurrentModel(true)
    })
    oldA.resolve(defaultA)
    await act(async () => {
      await old
    })
    expect(getComposerModelSelection(b)).toBeNull()
    newB.resolve(defaultB)
    await act(async () => {
      await fresh
      await new Promise(resolve => setTimeout(resolve, 0))
    })
    expect(getComposerModelSelection(b)).toMatchObject(defaultB)
    expect(await send(value)).toBe('composition-created')
    expectCreate(b, defaultB)
  })

  it('keeps the actual A manual pin through A→B→A and a late B default', async () => {
    const value = await setup()
    await act(async () => {
      await value.handle.controls.selectModel({ model: 'manual-a', provider: 'provider-a' })
    })
    act(() => route(b))
    const late = deferred<typeof defaultB>()
    vi.mocked(getGlobalModelInfo).mockReturnValueOnce(late.promise)
    let waiting!: Promise<void>
    act(() => {
      waiting = value.handle.controls.refreshCurrentModel(true)
    })
    act(() => route(a))
    await act(async () => {
      await value.handle.controls.refreshCurrentModel(true)
    })
    late.resolve(defaultB)
    await act(async () => {
      await waiting
    })
    expect(getComposerModelSelection(a)).toMatchObject({ model: 'manual-a', provider: 'provider-a', source: 'manual' })
    expect(await send(value)).toBe('composition-created')
    expectCreate(a, { model: 'manual-a', provider: 'provider-a' })
  })

  it('freezes a real producer receipt and suppresses duplicate Send during readiness', async () => {
    const value = await setup()
    await seed(value)
    const pending = deferred<void>()
    vi.mocked(ensureGatewayAgent).mockReturnValueOnce(pending.promise)
    let first!: Promise<null | string>
    act(() => {
      first = value.handle.actions.createBackendSessionForSend()
    })
    await waitFor(() => expect(ensureGatewayAgent).toHaveBeenCalledOnce())
    expect(await send(value)).toBeNull()
    await act(async () => {
      await value.handle.controls.selectModel({ model: 'manual-a', provider: 'provider-a' })
    })
    pending.resolve()
    await act(async () => {
      expect(await first).toBe('composition-created')
    })
    expect(requestGatewayForAgent).toHaveBeenCalledOnce()
    expectCreate(a, defaultA)
  })

  it('keeps a real equal-value manual intent when an older default resolves', async () => {
    const value = await setup()
    const late = deferred<typeof defaultA>()
    vi.mocked(getGlobalModelInfo).mockReturnValueOnce(late.promise)
    let waiting!: Promise<void>
    act(() => {
      waiting = value.handle.controls.refreshCurrentModel(true)
    })
    await act(async () => {
      await value.handle.controls.selectModel(defaultA)
    })
    const manual = getComposerModelSelection(a)
    late.resolve(defaultA)
    await act(async () => {
      await waiting
    })
    expect(getComposerModelSelection(a)).toBe(manual)
    expect(manual?.source).toBe('manual')
    expect(await send(value)).toBe('composition-created')
    expectCreate(a, defaultA)
  })

  it('lets the actual Settings Apply result supersede weaker reads before and after the save', async () => {
    const value = await setup(1)
    await seed(value)
    const pending = deferred<{ ok: true; model: string; provider: string }>()
    vi.mocked(setModelAssignment).mockImplementationOnce(async () => {
      const saved = await pending.promise
      wireA = { model: saved.model, provider: saved.provider }

      return saved as never
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(setModelAssignment).toHaveBeenCalledOnce())
    vi.mocked(getGlobalModelInfo).mockResolvedValueOnce({ ...defaultA, model: 'weaker-a' })
    await act(async () => {
      await value.handle.controls.refreshCurrentModel()
    })
    expect(getComposerModelSelection(a)?.model).toBe('weaker-a')
    const late = deferred<typeof defaultA>()
    vi.mocked(getGlobalModelInfo).mockReturnValueOnce(late.promise)
    let waiting!: Promise<void>
    act(() => {
      waiting = value.handle.controls.refreshCurrentModel()
    })
    pending.resolve({ ok: true, model: 'saved-a', provider: 'provider-a' })
    await waitFor(() => expect(getComposerModelSelection(a)?.model).toBe('saved-a'))
    late.resolve({ ...defaultA, model: 'late-weaker-a' })
    await act(async () => {
      await waiting
    })
    expect($currentModel.get()).toBe('saved-a')
    expect(await send(value)).toBe('composition-created')
    expectCreate(a, { model: 'saved-a', provider: 'provider-a' })
  })

  it('preserves a later equal-value manual pin after an actual Settings save began', async () => {
    const value = await setup(1)
    await seed(value)
    const pending = deferred<{ ok: true; model: string; provider: string }>()
    vi.mocked(setModelAssignment).mockImplementationOnce(async () => {
      const saved = await pending.promise
      wireA = { model: saved.model, provider: saved.provider }

      return saved as never
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(setModelAssignment).toHaveBeenCalledOnce())
    await act(async () => {
      await value.handle.controls.selectModel(defaultA)
    })
    const manual = getComposerModelSelection(a)
    pending.resolve({ ok: true, model: 'saved-a', provider: 'provider-a' })
    await waitFor(() =>
      expect((screen.getByRole('button', { name: 'Apply' }) as HTMLButtonElement).disabled).toBe(false)
    )
    expect(getComposerModelSelection(a)).toBe(manual)
    expect(manual?.source).toBe('manual')
    expect(await send(value)).toBe('composition-created')
    expectCreate(a, defaultA)
  })

  it('keeps a known receipt on actual Settings failure and defeats an older save after a newer failure', async () => {
    const value = await setup(2)
    await seed(value)
    const known = getComposerModelSelection(a)
    const older = deferred<{ ok: true; model: string; provider: string }>()
    const newer = deferred<never>()
    vi.mocked(setModelAssignment)
      .mockReturnValueOnce(older.promise as never)
      .mockReturnValueOnce(newer.promise)
    const first = within(screen.getByTestId('settings-0'))
    const second = within(screen.getByTestId('settings-1'))
    fireEvent.click(await first.findByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(setModelAssignment).toHaveBeenCalledTimes(1))
    fireEvent.click(await second.findByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(setModelAssignment).toHaveBeenCalledTimes(2))
    newer.reject(new Error('offline save failure'))
    expect(await second.findByText('offline save failure')).toBeTruthy()
    older.resolve({ ok: true, model: 'older-saved-a', provider: 'provider-a' })
    await waitFor(() =>
      expect((first.getByRole('button', { name: 'Apply' }) as HTMLButtonElement).disabled).toBe(false)
    )
    expect(getComposerModelSelection(a)).toBe(known)
    expect(await send(value)).toBe('composition-created')
    expectCreate(a, defaultA)
  })

  it('restores the exact default receipt after a real live picker rejection, then uses it on a new Send', async () => {
    const value = await setup()
    await seed(value)
    const old = getComposerModelSelection(a)
    setSessionOwnerHint('composition-live-stored', a)
    publishSessionState('composition-live', { ...createClientSessionState('composition-live-stored'), ...defaultA })
    act(() => {
      setActiveSessionId('composition-live')
      setSelectedStoredSessionId('composition-live-stored')
    })
    const pending = deferred<never>()
    value.request.mockReturnValueOnce(pending.promise)
    let picking!: Promise<boolean>
    act(() => {
      picking = value.handle.controls.selectModel({ model: 'rejected-a', provider: 'provider-a' })
    })
    expect(getComposerModelSelection(a)?.model).toBe('rejected-a')
    pending.reject(new Error('offline rejected model'))
    await act(async () => {
      expect(await picking).toBe(false)
    })
    expect(getComposerModelSelection(a)).toMatchObject({ ...defaultA, source: old?.source })
    expect($currentModel.get()).toBe(defaultA.model)
    expect($currentProvider.get()).toBe(defaultA.provider)
    act(() => value.handle.actions.startFreshSessionDraft())
    expect(await send(value)).toBe('composition-created')
    expectCreate(a, defaultA)
  })

  it('refuses the actual Settings confirmation retry after A changes to B with the same logical profile', async () => {
    const value = await setup(1)
    await seed(value)
    const assignmentSources: (string | null)[] = []
    vi.mocked(setModelAssignment).mockImplementation(async () => {
      assignmentSources.push(getApiRequestConnection())

      return { ok: false, confirm_required: true, confirm_message: 'offline model warning' } as never
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Apply' }))
    let confirm!: NonNullable<ReturnType<typeof $notifications.get>[number]['action']>
    await waitFor(() => {
      const notice = $notifications.get().find(item => item.id.startsWith('model-warning-confirm-'))
      expect(notice?.action).toBeDefined()
      confirm = notice!.action!
    })
    act(() => route(b))
    await act(async () => {
      await value.handle.controls.refreshCurrentModel(true)
    })
    const ownedB = getComposerModelSelection(b)
    await act(async () => {
      confirm.onClick()
    })
    await waitFor(() =>
      expect((screen.getByRole('button', { name: 'Apply' }) as HTMLButtonElement).disabled).toBe(false)
    )
    expect(setModelAssignment).toHaveBeenCalledOnce()
    expect(assignmentSources).toEqual([a.connectionId])
    expect(getComposerModelSelection(b)).toBe(ownedB)
    expect(ownedB).toMatchObject(defaultB)
    expect(await send(value)).toBe('composition-created')
    expectCreate(b, defaultB)
  })
})

it('admits a scalar configured model without a provider override', async () => {
  wireA = { model: 'legacy-model', provider: '' }
  const value = await setup()
  await act(async () => {
    await value.handle.controls.refreshCurrentModel(true)
  })
  expect(getComposerModelSelection(a)).toMatchObject({ model: 'legacy-model', provider: '' })
  expect(await send(value)).toBe('composition-created')
  const call = vi.mocked(requestGatewayForAgent).mock.calls.find(call => call[2] === 'session.create')!
  expect(call[3]).toMatchObject({ profile: 'backend-a', model: 'legacy-model' })
  expect(call[3]).not.toHaveProperty('provider')
})

it.each(['empty', 'rejected'])('recovers on Send after an initial %s model-info failure', async failure => {
  const value = await setup()

  if (failure === 'empty') {
    vi.mocked(getGlobalModelInfo).mockResolvedValueOnce({ model: '', provider: '' })
  } else {
    vi.mocked(getGlobalModelInfo).mockRejectedValueOnce(new Error('temporary offline failure'))
  }

  await act(async () => {
    await value.handle.controls.refreshCurrentModel(true)
  })
  expect(getComposerModelSelection(a)).toBeNull()
  expect(await send(value)).toBe('composition-created')
  expect(getGlobalModelInfo).toHaveBeenLastCalledWith({ connectionId: 'source-a', profile: 'backend-a' })
  expectCreate(a, defaultA)
})

it('reads a never-foreground Bot tile owner while preserving the foreground receipt and display', async () => {
  const value = await setup()
  await seed(value)
  const receipt = getComposerModelSelection(a)
  vi.mocked(getGlobalModelInfo).mockResolvedValueOnce(defaultB)
  vi.mocked(requestGatewayForAgent).mockResolvedValueOnce({
    session_id: 'tile-b',
    stored_session_id: 'stored-tile-b'
  } as never)
  await act(async () => {
    await value.handle.actions.openNewSessionTile('center', {
      route: b,
      cwd: null,
      workspaceScope: { workspaceMode: 'bots' }
    })
  })
  expect(getGlobalModelInfo).toHaveBeenLastCalledWith({ connectionId: 'source-b', profile: 'backend-b' })
  expectCreate(b, defaultB)
  expect(getComposerModelSelection(a)).toBe(receipt)
  expect($currentModel.get()).toBe(defaultA.model)
  expect(getComposerModelSelection(b)).toBeNull()
})

it('bounds missing-receipt recovery to one read per Send and retries on the next Send', async () => {
  const value = await setup()
  vi.mocked(getGlobalModelInfo).mockResolvedValueOnce({ model: '', provider: '' })
  expect(await send(value)).toBeNull()
  expect(getGlobalModelInfo).toHaveBeenCalledOnce()
  expect(requestGatewayForAgent).not.toHaveBeenCalled()
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, 0))
  })
  expect(await send(value)).toBe('composition-created')
  expect(getGlobalModelInfo).toHaveBeenCalledTimes(2)
})

it('rejects a late recovery reply after a deliberate owner pin, then uses that pin on the next Send', async () => {
  const value = await setup()
  const late = deferred<typeof defaultA>()
  vi.mocked(getGlobalModelInfo).mockReturnValueOnce(late.promise)
  let first!: Promise<null | string>
  act(() => {
    first = value.handle.actions.createBackendSessionForSend()
  })
  await waitFor(() => expect(getGlobalModelInfo).toHaveBeenCalledOnce())
  await act(async () => {
    await value.handle.controls.selectModel({ model: 'manual-a', provider: 'provider-a' })
  })
  late.resolve(defaultA)
  await act(async () => {
    expect(await first).toBeNull()
  })
  expect(requestGatewayForAgent).not.toHaveBeenCalled()
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, 0))
  })
  expect(await send(value)).toBe('composition-created')
  expectCreate(a, { model: 'manual-a', provider: 'provider-a' })
  expect(getGlobalModelInfo).toHaveBeenCalledOnce()
})

it('recovers an unstamped B draft from B’s target while the ambient source is still A', async () => {
  const value = await setup()
  await seed(value)
  act(() => route(b, false))
  expect(await send(value)).toBe('composition-created')
  expect(getGlobalModelInfo).toHaveBeenLastCalledWith({ connectionId: 'source-b', profile: 'backend-b' })
  expectCreate(b, defaultB)
})

it('freezes a background tile’s captured owner despite a late reply and a foreground pin change', async () => {
  const value = await setup()
  await seed(value)
  const late = deferred<typeof defaultB>()
  vi.mocked(getGlobalModelInfo).mockReturnValueOnce(late.promise)
  vi.mocked(requestGatewayForAgent).mockResolvedValueOnce({
    session_id: 'tile-b',
    stored_session_id: 'late-tile-b'
  } as never)
  let tile!: Promise<void>
  act(() => {
    tile = value.handle.actions.openNewSessionTile('center', { route: b, cwd: null })
  })
  await waitFor(() => expect(getGlobalModelInfo).toHaveBeenCalledTimes(2))
  await act(async () => {
    await value.handle.controls.selectModel({ model: 'manual-a', provider: 'provider-a' })
  })
  const foregroundPin = getComposerModelSelection(a)
  late.resolve(defaultB)
  await act(async () => {
    await tile
  })
  expectCreate(b, defaultB)
  expect(getComposerModelSelection(a)).toBe(foregroundPin)
  expect($currentModel.get()).toBe('manual-a')
})
