import { translateNow } from '@/i18n'
import { textPart } from '@/lib/chat-messages'
import { coerceGatewayText } from '@/lib/chat-runtime'
import { isProviderSetupErrorMessage } from '@/lib/provider-setup-errors'
import { type AgentNoticePayload, clearAgentNotice, nativeNoticeInput, showAgentNotice } from '@/store/agent-notices'
import { clearClarifyRequest } from '@/store/clarify'
import { setSessionCompacting } from '@/store/compaction'
import { enqueueQueuedPrompt } from '@/store/composer-queue'
import { refreshBackgroundProcesses } from '@/store/composer-status'
import { applyGoalState, applyGoalStatusText, type GoalStatePayload } from '@/store/goals'
import { dispatchNativeNotification } from '@/store/native-notifications'
import { isDiskFullErrorMessage, notify, notifyError } from '@/store/notifications'
import { requestDesktopOnboarding } from '@/store/onboarding'
import { flashPetActivity, setPetActivity } from '@/store/pet'
import { clearAllPrompts } from '@/store/prompts'
import { setTurnStartedAt } from '@/store/session'
import { clearActiveSessionTodos } from '@/store/todos'
import { takeUnadmittedSend } from '@/store/unadmitted-send'

import type { GatewayEventContext } from './types'

/** status.update / review.summary / notification.show / notification.clear /
 *  error — the status-and-notice tail of the dispatcher. */
export function handleStatusEvent(ctx: GatewayEventContext): boolean {
  const { deps, event, payload, sessionId, isActiveEvent, occurredAt } = ctx
  const { compactedTurnRef, failAssistantMessage, flushQueuedDeltas, queryClient, updateSessionState } = deps

  if (event.type === 'status.update') {
    if (sessionId && payload?.kind === 'compacting') {
      setSessionCompacting(sessionId, true)
      compactedTurnRef.current.add(sessionId)
    } else if (sessionId && payload?.kind === 'compacted') {
      setSessionCompacting(sessionId, false)
      compactedTurnRef.current.delete(sessionId)
    } else if (sessionId && payload?.kind === 'process') {
      // The gateway's notification poller announces background process
      // completions / watch matches here — re-sync the status stack.
      void refreshBackgroundProcesses(sessionId)
    } else if (sessionId && payload?.kind === 'goal') {
      // Prefer the authoritative structured state; fall back to the text
      // grammar only for older gateways that do not send `goal_state`.
      const structured = payload?.goal_state

      if (structured && typeof structured === 'object') {
        applyGoalState(sessionId, structured as GoalStatePayload)
      } else {
        applyGoalStatusText(sessionId, coerceGatewayText(payload?.text))
      }
    }

    return true
  }

  if (event.type === 'review.summary') {
    // Self-improvement background review saved something to memory/skills
    // and emitted a persistent summary (Python formats it as
    // "💾 Self-improvement review: …"). The CLI prints this via
    // prompt_toolkit and the Ink TUI renders it as a system line; the
    // desktop has neither, so without this handler the skill/memory
    // change happens silently. Surface it as a persistent system message
    // in the transcript so the user is always informed — it must not be a
    // transient toast that can be missed.
    //
    // Typed here with the `review:` marker (same convention as `steer:` /
    // `slash:`) so SystemMessage can paint it as the memory-write row it
    // is instead of sniffing the backend's prose. The leading 💾 goes with
    // it — the row draws its own glyph.
    const text = coerceGatewayText(payload?.text)
      .trim()
      .replace(/^[^\p{L}\p{N}]+/u, '')

    if (text && sessionId) {
      flushQueuedDeltas(sessionId)
      updateSessionState(sessionId, state => ({
        ...state,
        messages: [
          ...state.messages,
          {
            id: `review-summary-${Date.now()}`,
            role: 'system',
            parts: [textPart(`review:${text}`, occurredAt)],
            timestamp: occurredAt
          }
        ]
      }))
    }

    return true
  }

  if (event.type === 'notification.show') {
    // Driver-agnostic agent notice (credits usage/grant/depleted/restored
    // from `agent/credits_tracker.py`). The Ink TUI renders these in its
    // status bar; the desktop renders them as toasts. The notice key doubles
    // as the toast id, so the escalating 50→75→90 credits line replaces in
    // place instead of stacking. Account-wide signal — shown regardless of
    // which session is focused.
    const notice = event.payload as AgentNoticePayload | undefined

    showAgentNotice(notice)

    // The urgent pair (access paused / restored) also breaks through as a
    // native OS notification when Hermes is backgrounded; dispatch is gated
    // by the user's notification prefs + backgrounded check.
    const native = nativeNoticeInput(notice, translateNow('notifications.native.creditsTitle'))

    if (native) {
      dispatchNativeNotification(native)
    }

    // A credits crossing moves the account balance. Settings → Billing polls
    // `billing.state` every 30s; nudge it so the page reflects the crossing
    // immediately instead of up to 30s late.
    if (notice?.key?.startsWith('credits.')) {
      void queryClient.invalidateQueries({ queryKey: ['billing', 'state'] })
    }

    return true
  }

  if (event.type === 'notification.clear') {
    // Key-matched dismissal (e.g. credits restored clears the depleted
    // notice). notify() keys the toast by the notice key, so this maps
    // straight to dismissNotification(key).
    clearAgentNotice((event.payload as AgentNoticePayload | undefined)?.key)

    return true
  }

  if (event.type === 'error') {
    const errorMessage = payload?.message || 'Hermes reported an error'
    const looksLikeProviderSetup = isProviderSetupErrorMessage(errorMessage)
    // A retryable admission refusal is NOT a failed turn: the host declined to
    // start one because another turn still owned the session (typically an
    // invisible notification/goal-judge turn). Painting it as a terminal error
    // produced a "Turn failed" notification, an error card with the model name,
    // and a global toast for a message that was never attempted. Match the
    // frame's structured classification when present, else the wire text that
    // every surface already emits.
    const errorSurface = (payload?.error_surface ?? undefined) as { code?: string; retryable?: boolean } | undefined

    // Narrow on purpose: only the admission-refusal code is transient. Matching
    // any `retryable` surface would also swallow failed-turn treatment for
    // future error classes that happen to carry that flag. The text fallback
    // applies only when the gateway sends no structured surface at all.
    const transientAdmissionRefusal =
      errorSurface?.code === 'session_busy' || (!errorSurface && /session busy/i.test(errorMessage))

    // A turn that errors out has also ended — drop any open blocking prompt
    // for this session so an approval/sudo/secret overlay can't linger past
    // the failed turn (same intent as the message.complete clear). A refused
    // admission ended nothing: the owning turn is still live, so its approval
    // or clarify prompt must stay answerable.
    if (sessionId && !transientAdmissionRefusal) {
      clearAllPrompts(sessionId)
      clearClarifyRequest(undefined, sessionId)
      clearActiveSessionTodos(sessionId)
      setSessionCompacting(sessionId, false)
      compactedTurnRef.current.delete(sessionId)
    }

    if (isActiveEvent) {
      setPetActivity({ reasoning: false, toolRunning: false })

      if (!transientAdmissionRefusal) {
        flashPetActivity({ error: true })
      }
    }

    if (!transientAdmissionRefusal) {
      dispatchNativeNotification({
        body: errorMessage,
        kind: 'turnError',
        sessionId,
        title: translateNow('notifications.native.turnErrorTitle')
      })
    }

    // A refused send was recorded at dispatch. Re-queue it and drop its
    // optimistic bubble: leaving the bubble would keep a user message that
    // never ran, and dropping the text would lose work.
    const unadmitted = transientAdmissionRefusal && sessionId ? takeUnadmittedSend(sessionId) : null

    if (transientAdmissionRefusal) {
      if (unadmitted?.queueKey) {
        enqueueQueuedPrompt(unadmitted.queueKey, {
          text: unadmitted.text,
          attachments: unadmitted.attachments,
          note: translateNow('composer.queuedBehindBusy')
        })
        notify({ kind: 'info', message: translateNow('notifications.errors.sessionBusyQueued') })
      } else {
        // Older gateway or a send that was never recorded: be honest rather
        // than silently dropping it.
        notify({ kind: 'info', message: translateNow('notifications.errors.sessionBusyRetry') })
      }
    } else if (looksLikeProviderSetup) {
      requestDesktopOnboarding(errorMessage)
    } else if (isDiskFullErrorMessage(errorMessage)) {
      notifyError(new Error(errorMessage), translateNow('notifications.errors.diskFull'))
    } else {
      // Toast globally, not just when the failing thread is focused: a
      // turn-ending error (e.g. out of funds) blocks every thread, so the
      // inline error alone is too easy to miss. The stable id collapses the
      // same error from multiple blocked threads into one toast.
      notify({
        id: `gateway-error:${errorMessage}`,
        kind: 'error',
        title: 'Hermes error',
        message: errorMessage
      })
    }

    if (sessionId) {
      flushQueuedDeltas(sessionId)

      if (transientAdmissionRefusal) {
        // No turn started. Drop the optimistic bubble for a re-queued send (it
        // now lives in the queue) and settle the send state. When the send
        // could not be recovered, keep the bubble as the record of the attempt.
        updateSessionState(sessionId, state => ({
          ...state,
          messages: unadmitted
            ? state.messages.filter(message => message.id !== unadmitted.optimisticId)
            : state.messages,
          awaitingResponse: false,
          busy: false,
          reconnecting: false,
          turnStartedAt: null
        }))
      } else {
        takeUnadmittedSend(sessionId)
        failAssistantMessage(sessionId, errorMessage, occurredAt)
      }
    }

    if (isActiveEvent) {
      setTurnStartedAt(null)
    }

    return true
  }

  return false
}
