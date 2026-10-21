/** A retryable admission refusal must not be painted as a failed turn.
 *
 * The host refuses to start a turn when another turn still owns the session.
 * The user's message was never attempted, yet the old path fired a native
 * "Turn failed" notification, a global error toast, and an inline assistant
 * error card carrying the model name — and it cleared any live approval or
 * clarify prompt belonging to the turn that was still running.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

const dispatchNativeNotification = vi.fn()
const notify = vi.fn()
const notifyError = vi.fn()
const clearAllPrompts = vi.fn()
const clearClarifyRequest = vi.fn()

vi.mock('@/store/native-notifications', () => ({ dispatchNativeNotification }))
vi.mock('@/store/notifications', () => ({ isDiskFullErrorMessage: () => false, notify, notifyError }))
vi.mock('@/store/prompts', () => ({ clearAllPrompts }))
vi.mock('@/store/clarify', () => ({ clearClarifyRequest }))
vi.mock('@/i18n', () => ({ translateNow: (key: string) => key }))

const { $queuedPromptsBySession } = await import('@/store/composer-queue')
const { $unadmittedSendBySession, recordUnadmittedSend } = await import('@/store/unadmitted-send')
const { handleStatusEvent } = await import('./status')

type Ctx = Parameters<typeof handleStatusEvent>[0]

function context(overrides: { error_surface?: unknown; message?: string } = {}, sessionId: string | null = 's1'): Ctx {
  return {
    deps: {
      compactedTurnRef: { current: new Set<string>() },
      failAssistantMessage: vi.fn(),
      flushQueuedDeltas: vi.fn(),
      queryClient: {},
      updateSessionState: vi.fn()
    } as never,
    event: { type: 'error' } as never,
    explicitSid: sessionId ?? '',
    fromActiveSource: () => true,
    isActiveEvent: true,
    occurredAt: 0,
    payload: { message: overrides.message, error_surface: overrides.error_surface } as never,
    scheduleConfigRefresh: vi.fn(),
    sessionId
  }
}

describe('session-busy admission refusal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    $queuedPromptsBySession.set({})
    $unadmittedSendBySession.set({})
  })

  it('treats "session busy" as retryable, not as a failed turn', () => {
    const ctx = context({ message: 'session busy' })

    expect(handleStatusEvent(ctx)).toBe(true)

    expect(dispatchNativeNotification).not.toHaveBeenCalled()
    expect(ctx.deps.failAssistantMessage).not.toHaveBeenCalled()
    expect(notify).toHaveBeenCalledWith({ kind: 'info', message: 'notifications.errors.sessionBusyRetry' })
    // The owning turn is still live — its approval/clarify prompt must survive.
    expect(clearAllPrompts).not.toHaveBeenCalled()
    expect(clearClarifyRequest).not.toHaveBeenCalled()
  })

  it('settles the optimistic send without an error card', () => {
    const ctx = context({ message: 'session busy' })

    handleStatusEvent(ctx)

    const update = ctx.deps.updateSessionState as unknown as ReturnType<typeof vi.fn>
    expect(update).toHaveBeenCalledTimes(1)

    const [, updater] = update.mock.calls[0]
    const settled = updater({ busy: true, awaitingResponse: true, reconnecting: true, turnStartedAt: 1, messages: [] })

    expect(settled).toMatchObject({ busy: false, awaitingResponse: false, reconnecting: false, turnStartedAt: null })
  })

  it('honours the structured error_surface classification', () => {
    const ctx = context({ error_surface: { code: 'session_busy', retryable: true }, message: 'admission refused' })

    handleStatusEvent(ctx)

    expect(notify).toHaveBeenCalledWith({ kind: 'info', message: 'notifications.errors.sessionBusyRetry' })
    expect(ctx.deps.failAssistantMessage).not.toHaveBeenCalled()
  })

  it('still fails a real turn error', () => {
    const ctx = context({ message: 'provider returned 500' })

    handleStatusEvent(ctx)

    expect(dispatchNativeNotification).toHaveBeenCalledTimes(1)
    expect(ctx.deps.failAssistantMessage).toHaveBeenCalledWith('s1', 'provider returned 500', 0)
    expect(clearAllPrompts).toHaveBeenCalledWith('s1')
  })

  it('re-queues a refused send and drops its optimistic bubble', () => {
    recordUnadmittedSend('s1', {
      attachments: [],
      optimisticId: 'optimistic-1',
      queueKey: 'queue-key-1',
      text: 'run the tests'
    })
    const ctx = context({ message: 'session busy' })

    handleStatusEvent(ctx)

    expect($queuedPromptsBySession.get()['queue-key-1']?.[0]).toMatchObject({
      note: 'composer.queuedBehindBusy',
      text: 'run the tests'
    })
    expect(notify).toHaveBeenCalledWith({ kind: 'info', message: 'notifications.errors.sessionBusyQueued' })

    const update = ctx.deps.updateSessionState as unknown as ReturnType<typeof vi.fn>
    const [, updater] = update.mock.calls[0]

    const settled = updater({
      awaitingResponse: true,
      busy: true,
      messages: [{ id: 'optimistic-1' }, { id: 'other' }],
      reconnecting: true,
      turnStartedAt: 1
    })

    expect(settled.messages).toEqual([{ id: 'other' }])
    // The record is consumed: a second refusal must not queue the same text twice.
    expect($unadmittedSendBySession.get().s1).toBeUndefined()
  })
})
