import { beforeEach, describe, expect, it, vi } from 'vitest'

import { stopRecoveryStateFromError, turnController } from '../app/turnController.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'
import { GatewayRpcError } from '../gatewayClient.js'

beforeEach(() => {
  resetUiState()
  turnController.fullReset()
})

describe('Stop failure disposition mapping', () => {
  it.each([
    ['indeterminate', 'stop_indeterminate'],
    ['not_applied', 'stop_proven_running'],
    ['not_sent', 'stop_request_pending']
  ] as const)('maps %s to the safe controller state %s', (effectDisposition, expected) => {
    const error = new GatewayRpcError('Stop failed', 5019, { effect_disposition: effectDisposition })

    expect(stopRecoveryStateFromError(error)).toBe(expected)
  })

  it('does not reinterpret generic transport errors as a Stop disposition', () => {
    expect(stopRecoveryStateFromError(new Error('websocket closed'))).toBeNull()
    expect(
      stopRecoveryStateFromError(
        new GatewayRpcError('Stop failed', 500, {
          effect_disposition: 'indeterminate'
        })
      )
    ).toBeNull()
  })

  it('reconciles an indeterminate Stop with the authoritative accepted-turn projection', async () => {
    patchUiState({ busy: true, sid: 's1', status: 'running…' })
    turnController.recordAcceptedTurn({
      host_boot_id: 'host-1',
      request_id: 'turn-1',
      route: 'compute_host',
      session_id: 's1'
    })

    const sys = vi.fn()

    const gw = {
      request: vi
        .fn()
        .mockRejectedValueOnce(
          new GatewayRpcError('Stop application could not be confirmed', 5019, {
            effect_disposition: 'indeterminate'
          })
        )
        .mockResolvedValueOnce({ running: false, session_id: 's1' })
    }

    await turnController.interruptTurn({ appendMessage: vi.fn(), gw, sid: 's1', sys })
    await vi.waitFor(() => expect(getUiState().busy).toBe(false))

    expect(gw.request).toHaveBeenNthCalledWith(1, 'session.interrupt', { session_id: 's1' })
    expect(gw.request).toHaveBeenNthCalledWith(2, 'session.turn.poll', {
      accepted_turn: {
        host_boot_id: 'host-1',
        request_id: 'turn-1',
        route: 'compute_host',
        session_id: 's1'
      },
      session_id: 's1'
    })
    expect(sys).toHaveBeenCalledWith('Stop reconciliation confirmed the original turn settled')
  })
})
