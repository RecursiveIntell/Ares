import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { PRIMARY_SESSION_VIEW } from '@/app/chat/session-view'
import { useModelControls } from '@/app/session/hooks/use-model-controls'
import { createClientSessionState } from '@/lib/chat-runtime'
import { applyModelPreset } from '@/store/model-presets'
import { notify } from '@/store/notifications'
import {
  $activeSessionId,
  $currentReasoningEffort,
  $selectedStoredSessionId,
  beginRuntimeOptionIntent,
  ownsRuntimeOptionIntent,
  setCurrentModel,
  setCurrentProvider,
  setCurrentReasoningEffort
} from '@/store/session'
import {
  $sessionStates,
  publishSessionState,
  type SessionTileDelegate,
  setSessionTileDelegate
} from '@/store/session-states'

import { ModelMenuPanel } from './model-menu-panel'

vi.mock('@/hermes', () => ({
  getGlobalModelInfo: vi.fn(),
  getGlobalModelOptions: vi.fn(),
  setApiRequestProfile: vi.fn()
}))
vi.mock('@/i18n', () => ({
  translateNow: (key: string) => key,
  useI18n: () => ({
    t: {
      common: { confirm: 'Confirm' },
      desktop: { modelSwitchFailed: 'Failed' },
      shell: {
        modelMenu: { refreshModels: 'Refresh' },
        modelOptions: { updateFailed: 'Failed', fastFailed: 'Failed', unconfirmed: 'Model options unconfirmed' }
      }
    }
  })
}))
vi.mock('@/store/notifications', () => ({ notify: vi.fn(), notifyError: vi.fn(), dismissNotification: vi.fn() }))
vi.mock('@/components/ui/dropdown-menu', () => ({
  DropdownMenuItem: ({ children }: any) => <div>{children}</div>,
  dropdownMenuRow: ''
}))
vi.mock('@/lib/model-options', async importOriginal => ({
  ...(await importOriginal<any>()),
  requestModelOptions: async () => ({
    model: 'a',
    provider: 'p',
    providers: [{ models: ['a', 'b', 'c'], name: 'P', slug: 'p' }]
  })
}))
vi.mock('./model-catalog-menu', () => ({
  ModelCatalogMenu: ({ controller, footer }: any) => (
    <>
      <button onClick={() => controller.setOptions({ effort: 'high' }, { isActive: true, model: 'a', provider: 'p' })}>
        Set high
      </button>
      {footer}
    </>
  )
}))

function seed(id = 'r1', stored = 's1', effort = 'medium') {
  $activeSessionId.set(id)
  $selectedStoredSessionId.set(stored)
  setCurrentModel('a')
  setCurrentProvider('p')
  setCurrentReasoningEffort(effort)
  publishSessionState(id, { ...createClientSessionState(stored), model: 'a', provider: 'p', reasoningEffort: effort })
}

beforeEach(() => {
  $sessionStates.set({})
  seed()
  setSessionTileDelegate({
    updateSession: (id, fn) => {
      const next = fn($sessionStates.get()[id])
      publishSessionState(id, next)

      return next
    }
  } as SessionTileDelegate)
})
afterEach(() => {
  cleanup()
  $sessionStates.set({})
  $activeSessionId.set(null)
  $selectedStoredSessionId.set(null)
  setSessionTileDelegate({} as SessionTileDelegate)
  vi.restoreAllMocks()
})

function panel(request: any) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ModelMenuPanel onSelectModel={() => {}} requestGateway={request} />
    </QueryClientProvider>
  )
}

it('a primary effort click must update the live SessionView, not only its draft mirror', async () => {
  const request = vi.fn(async () => ({ key: 'reasoning', value: 'high' }))
  panel(request)
  await act(async () => fireEvent.click(screen.getByText('Set high')))
  expect(request).toHaveBeenCalledWith('config.set', { key: 'reasoning', session_id: 'r1', value: 'high' })
  console.log({ draft: $currentReasoningEffort.get(), visible: PRIMARY_SESSION_VIEW.$reasoningEffort.get() })
  expect(PRIMARY_SESSION_VIEW.$reasoningEffort.get()).toBe('high')
  expect($currentReasoningEffort.get()).toBe('high')
})
it('a failed old reasoning write must not change the newly selected session draft mirror', async () => {
  let reject!: (e: Error) => void

  const request = vi.fn(
    () =>
      new Promise((_, r) => {
        reject = r
      })
  )

  panel(request)
  await act(async () => fireEvent.click(screen.getByText('Set high')))
  act(() => seed('r2', 's2', 'low'))
  await act(async () => reject(new Error('late old failure')))
  console.log({ newSession: $activeSessionId.get(), draft: $currentReasoningEffort.get() })
  expect($currentReasoningEffort.get()).toBe('low')
})
it('a failed older model write must not roll back a newer successful pick', async () => {
  let reject!: (e: Error) => void

  const request = vi
    .fn()
    .mockImplementationOnce(
      () =>
        new Promise((_, r) => {
          reject = r
        })
    )
    .mockResolvedValueOnce({ key: 'model', value: 'c' })

  const { result } = renderHook(() => useModelControls({ queryClient: new QueryClient(), requestGateway: request }))
  let first!: Promise<boolean>
  act(() => {
    first = result.current.selectModel({ model: 'b', provider: 'p' })
  })
  await act(async () => {
    expect(await result.current.selectModel({ model: 'c', provider: 'p' })).toBe(true)
  })
  await act(async () => {
    reject(new Error('old b failed'))
    await first
  })
  console.log({ visible: PRIMARY_SESSION_VIEW.$model.get(), expected: 'c' })
  expect(PRIMARY_SESSION_VIEW.$model.get()).toBe('c')
})
it('failed tile preset must restore confirmed effort instead of remaining falsely applied', async () => {
  await applyModelPreset(
    { effort: 'high' },
    {
      primary: false,
      sessionId: 'r1',
      failMessage: 'Failed',
      request: vi.fn().mockRejectedValue(new Error('rejected'))
    }
  )
  console.log({ tileEffort: $sessionStates.get().r1.reasoningEffort })
  expect($sessionStates.get().r1.reasoningEffort).toBe('medium')
})

it('separate hook instances share the same runtime intent fence', async () => {
  let rejectOld!: (error: Error) => void

  const firstRequest = vi.fn(
    () =>
      new Promise<never>((_, reject) => {
        rejectOld = reject
      })
  )

  const first = renderHook(() => useModelControls({ queryClient: new QueryClient(), requestGateway: firstRequest }))

  const second = renderHook(() =>
    useModelControls({ queryClient: new QueryClient(), requestGateway: vi.fn().mockResolvedValue({}) })
  )

  let pending!: Promise<boolean>
  act(() => {
    pending = first.result.current.selectModel({ model: 'b', provider: 'p' })
  })
  expect(firstRequest).toHaveBeenCalledTimes(1)
  await act(async () => {
    await second.result.current.selectModel({ model: 'c', provider: 'p' })
  })
  await act(async () => {
    rejectOld(new Error('rejected'))
    await pending
  })
  expect(PRIMARY_SESSION_VIEW.$model.get()).toBe('c')
})

it('a model rejection cannot mutate a replacement stored owner under the same runtime id', async () => {
  let reject!: (error: Error) => void

  const request = vi.fn(
    () =>
      new Promise<never>((_, r) => {
        reject = r
      })
  )

  const { result } = renderHook(() => useModelControls({ queryClient: new QueryClient(), requestGateway: request }))
  let pending!: Promise<boolean>
  act(() => {
    pending = result.current.selectModel({ model: 'b', provider: 'p' })
  })
  act(() => {
    seed('r1', 'replacement', 'low')
    setCurrentModel('replacement-model')
    publishSessionState('r1', { ...$sessionStates.get().r1, model: 'replacement-model' })
  })
  await act(async () => {
    reject(new Error('rejected'))
    await pending
  })
  expect(PRIMARY_SESSION_VIEW.$model.get()).toBe('replacement-model')
})

it('a confirmation cannot revive a superseded model intent after an ABA selection', async () => {
  vi.mocked(notify).mockClear()

  const request = vi
    .fn()
    .mockResolvedValueOnce({ confirm_required: true, confirm_message: 'confirm' })
    .mockResolvedValue({})

  const { result } = renderHook(() => useModelControls({ queryClient: new QueryClient(), requestGateway: request }))
  await act(async () => {
    await result.current.selectModel({ model: 'b', provider: 'p' })
  })
  const warning = vi.mocked(notify).mock.calls[0]?.[0]
  expect(warning?.action).toBeDefined()
  await act(async () => {
    await result.current.selectModel({ model: 'a', provider: 'p' })
  })
  await act(async () => {
    await warning!.action!.onClick()
  })
  expect(request).toHaveBeenCalledTimes(2)
  expect(PRIMARY_SESSION_VIEW.$model.get()).toBe('a')
})

it('a rejected first preset field restores the never-sent Fast field too', async () => {
  act(() => publishSessionState('r1', { ...$sessionStates.get().r1, fast: false }))
  const request = vi.fn().mockRejectedValue(new Error('rejected'))
  await applyModelPreset(
    { effort: 'high', fast: true },
    { primary: false, sessionId: 'r1', failMessage: 'Failed', request }
  )
  expect(request).toHaveBeenCalledTimes(1)
  expect($sessionStates.get().r1).toMatchObject({ reasoningEffort: 'medium', fast: false })
})

it('a superseded preset cannot send its late Fast write after another Fast choice succeeds', async () => {
  let resolveEffort!: (value: unknown) => void

  const oldRequest = vi
    .fn()
    .mockImplementationOnce(
      () =>
        new Promise(resolve => {
          resolveEffort = resolve
        })
    )
    .mockResolvedValue({})

  const old = applyModelPreset(
    { effort: 'high', fast: true },
    { primary: false, sessionId: 'r1', failMessage: 'Failed', request: oldRequest }
  )

  await applyModelPreset(
    { fast: false },
    { primary: false, sessionId: 'r1', failMessage: 'Failed', request: vi.fn().mockResolvedValue({}) }
  )
  resolveEffort({})
  await old
  expect(oldRequest).toHaveBeenCalledTimes(1)
  expect($sessionStates.get().r1.fast).toBe(false)
})

it('a rejected second preset field preserves the acknowledged effort', async () => {
  act(() => publishSessionState('r1', { ...$sessionStates.get().r1, fast: false }))
  const request = vi.fn().mockResolvedValueOnce({}).mockRejectedValueOnce(new Error('rejected'))
  await applyModelPreset(
    { effort: 'high', fast: true },
    { primary: false, sessionId: 'r1', failMessage: 'Failed', request }
  )
  expect($sessionStates.get().r1).toMatchObject({ reasoningEffort: 'high', fast: false })
})

it('bounds batched option-intent retention instead of accumulating two extra entries per call', () => {
  const captured: Record<string, number>[] = []

  for (let i = 0; i < 3000; i += 1) {
    captured.push(beginRuntimeOptionIntent(`capacity-${i}`, ['model', 'effort', 'fast']))
  }

  expect(ownsRuntimeOptionIntent('capacity-1000', 'effort', captured[1000].effort)).toBe(false)
  expect(ownsRuntimeOptionIntent('capacity-2999', 'fast', captured[2999].fast)).toBe(true)
})

it('a primary preset updates its explicit draft preference as well as the live slice', async () => {
  await applyModelPreset(
    { effort: 'high' },
    { primary: true, sessionId: 'r1', failMessage: 'Failed', request: vi.fn().mockResolvedValue({}) }
  )
  expect(PRIMARY_SESSION_VIEW.$reasoningEffort.get()).toBe('high')
  expect($currentReasoningEffort.get()).toBe('high')
})

it('an uncertain effort write reads host state rather than guessing a rollback or retrying the write', async () => {
  const request = vi
    .fn()
    .mockRejectedValueOnce(new Error('request timed out after 30s: config.set'))
    .mockResolvedValueOnce({ owner: 'compute_host', session_id: 'r1', host_boot_id: 'owner-boot', value: 'low' })

  panel(request)
  await act(async () => fireEvent.click(screen.getByText('Set high')))
  expect(request).toHaveBeenNthCalledWith(2, 'config.get', { key: 'reasoning', session_id: 'r1' })
  expect(request.mock.calls.filter(([method]) => method === 'config.set')).toHaveLength(1)
  expect(PRIMARY_SESSION_VIEW.$reasoningEffort.get()).toBe('low')
  expect($currentReasoningEffort.get()).toBe('low')
})

it('unavailable readback leaves the desired effort visibly unconfirmed', async () => {
  const request = vi.fn().mockRejectedValue(new Error('Hermes gateway connection closed'))
  panel(request)
  await act(async () => fireEvent.click(screen.getByText('Set high')))
  expect(request.mock.calls.filter(([method]) => method === 'config.set')).toHaveLength(1)
  expect($sessionStates.get().r1).toMatchObject({ reasoningEffort: 'high', unconfirmedRuntimeOptions: ['effort'] })
  expect(screen.getByRole('status').textContent).toContain('unconfirmed')
})

it('a late preset recovery cannot recreate an unsaved runtime after close', async () => {
  publishSessionState('r1', { ...$sessionStates.get().r1, storedSessionId: null })
  let resolve!: (value: unknown) => void

  const request = vi
    .fn()
    .mockRejectedValueOnce(new Error('request timed out'))
    .mockImplementationOnce(
      () =>
        new Promise(r => {
          resolve = r
        })
    )

  const pending = applyModelPreset(
    { effort: 'high', fast: true },
    {
      primary: false,
      sessionId: 'r1',
      failMessage: 'Failed',
      request
    }
  )

  await act(async () => {
    await Promise.resolve()
  })
  expect(request).toHaveBeenCalledTimes(2)
  $sessionStates.set({})
  resolve({ owner: 'compute_host', session_id: 'r1', value: 'high' })
  await pending
  expect($sessionStates.get()).toEqual({})
})

it('an uncertain preset effort reads back but never sends the unattempted Fast write', async () => {
  const request = vi
    .fn()
    .mockRejectedValueOnce(new Error('request timed out after 30s: config.set'))
    .mockResolvedValueOnce({ owner: 'compute_host', session_id: 'r1', host_boot_id: 'owner-boot', value: 'xhigh' })

  await applyModelPreset(
    { effort: 'high', fast: true },
    { primary: false, sessionId: 'r1', failMessage: 'Failed', request }
  )
  expect(request).toHaveBeenCalledTimes(2)
  expect(request).toHaveBeenLastCalledWith('config.get', { key: 'reasoning', session_id: 'r1' })
  expect($sessionStates.get().r1).toMatchObject({ reasoningEffort: 'xhigh', fast: false })
})
