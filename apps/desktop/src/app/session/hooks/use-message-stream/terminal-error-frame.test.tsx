import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ComposerControls } from '@/app/chat/composer/controls'
import type { ClientSessionState } from '@/app/types'
import { I18nProvider } from '@/i18n'
import { chatMessageText } from '@/lib/chat-messages'

import { type MessageStreamHarness, renderMessageStream } from './test-harness'

const SID = 'session-1'

let stream: MessageStreamHarness

function mountStream() {
  stream = renderMessageStream(SID)
}

const start = () => act(() => stream.handleEvent({ payload: {}, session_id: SID, type: 'message.start' }))

const delta = (text: string) =>
  act(() => stream.handleEvent({ payload: { text }, session_id: SID, type: 'message.delta' }))

const completeWithError = (payload: Record<string, unknown>) =>
  act(() => stream.handleEvent({ payload: { status: 'error', ...payload }, session_id: SID, type: 'message.complete' }))

function getState(): ClientSessionState {
  return stream.state()
}

function lastAssistant() {
  return [...getState().messages].reverse().find(m => m.role === 'assistant' && !m.hidden)
}

describe('terminal error message.complete frames', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('marks the bubble failed from the structured error field, not the text heuristic', async () => {
    mountStream()
    await start()
    await delta('…')

    // "Error: <detail>" does not match the legacy completionErrorText regexes.
    await completeWithError({ text: 'Error: invalid model slug', error: 'invalid model slug', recoverable: true })

    const bubble = lastAssistant()
    expect(bubble?.error).toBe('invalid model slug')
    expect(getState().busy).toBe(false)
    expect(getState().awaitingResponse).toBe(false)
  })

  it('keeps streamed partial text visible on a partial failure', async () => {
    mountStream()
    await start()
    await delta('half an ans')

    await completeWithError({
      text: 'half an ans',
      error: 'connection reset mid-stream',
      partial: true,
      recoverable: true
    })

    const bubble = lastAssistant()
    expect(bubble?.error).toBe('connection reset mid-stream')
    expect(chatMessageText(bubble!)).toBe('half an ans')
    expect(bubble?.pending).toBe(false)
  })

  it('falls back to the frame text when no error field is present', async () => {
    mountStream()
    await start()
    await delta('…')

    await completeWithError({ text: 'Error: something broke' })

    const bubble = lastAssistant()
    expect(bubble?.error).toBe('Error: something broke')
  })

  it('attaches the structured error_surface descriptor to the failed bubble', async () => {
    mountStream()
    await start()
    await delta('…')

    await completeWithError({
      text: 'Error: rate limited',
      error: 'rate limited',
      error_surface: { layer: 'provider', code: 'rate_limit', retryable: true },
      recoverable: true
    })

    const bubble = lastAssistant()
    expect(bubble?.error).toBe('rate limited')
    expect(bubble?.errorSurface).toEqual({ layer: 'provider', code: 'rate_limit', retryable: true })
  })

  it('ignores a garbled error_surface payload (older/foreign backends)', async () => {
    mountStream()
    await start()
    await delta('…')

    await completeWithError({
      text: 'Error: kaput',
      error: 'kaput',
      error_surface: { layer: 'not-a-layer', code: 42 },
      recoverable: true
    })

    const bubble = lastAssistant()
    expect(bubble?.error).toBe('kaput')
    expect(bubble?.errorSurface).toBeUndefined()
  })
})

describe('goal-chain reply boundary', () => {
  afterEach(() => cleanup())

  it('seals the reply without losing Stop before the host chain settles', async () => {
    mountStream()
    await start()
    await delta('first step done')
    await act(() =>
      stream.handleEvent({
        payload: { text: 'first step done', chain_pending: true },
        session_id: SID,
        type: 'message.complete'
      })
    )
    expect(lastAssistant()?.pending).toBe(false)
    expect(getState().busy).toBe(true)
    expect(getState().turnLive).toBe(true)
    render(
      <I18nProvider configClient={null} initialLocale="en">
        <ComposerControls
          autoSpeak={false}
          busy={getState().busy}
          busyAction="stop"
          canSubmit={true}
          conversation={{
            active: false,
            level: 0,
            muted: false,
            status: 'idle',
            onEnd: vi.fn(),
            onStart: vi.fn(),
            onStopTurn: vi.fn(),
            onToggleMute: vi.fn()
          }}
          disabled={false}
          hasComposerPayload={false}
          onDictate={vi.fn()}
          onQueue={vi.fn()}
          onToggleAutoSpeak={vi.fn()}
          state={{
            model: { canSwitch: false, model: '', provider: '' },
            tools: { enabled: false, label: '' },
            voice: { active: false, enabled: false }
          }}
          voiceStatus="idle"
        />
      </I18nProvider>
    )
    expect(screen.getByRole('button', { name: 'Stop' })).toBeTruthy()

    await act(() =>
      stream.handleEvent({
        payload: { running: false },
        session_id: SID,
        type: 'session.info'
      })
    )
    expect(getState().busy).toBe(false)
  })
})
