/* eslint-disable no-restricted-globals -- actual DOM focus is the regression contract */
import { act, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useAgentTerminal } from './use-agent-terminal'

const xterm = vi.hoisted(() => ({
  attachCustomKeyEventHandler: vi.fn(),
  clearSelection: vi.fn(),
  dispose: vi.fn(),
  focus: vi.fn(),
  getSelection: vi.fn(() => ''),
  loadAddon: vi.fn(),
  onSelectionChange: vi.fn(() => ({ dispose: vi.fn() })),
  open: vi.fn(),
  refresh: vi.fn(),
  write: vi.fn()
}))

const terminalRegistrations = vi.hoisted(() => ({
  makeTerminalReader: vi.fn(() => vi.fn()),
  registerReader: vi.fn(() => vi.fn()),
  registerWriter: vi.fn(() => vi.fn())
}))

vi.mock('@xterm/xterm', () => ({
  Terminal: class {
    readonly buffer = { active: {} }
    readonly rows = 24
    readonly unicode = { activeVersion: '6' }
    options: Record<string, unknown>

    constructor(options: Record<string, unknown>) {
      this.options = { ...options }
    }

    attachCustomKeyEventHandler = xterm.attachCustomKeyEventHandler
    clearSelection = xterm.clearSelection
    dispose = xterm.dispose
    focus = xterm.focus
    getSelection = xterm.getSelection
    loadAddon = xterm.loadAddon
    onSelectionChange = xterm.onSelectionChange
    open = xterm.open
    refresh = xterm.refresh
    write = xterm.write
  }
}))

vi.mock('@xterm/addon-fit', () => ({
  FitAddon: class {
    fit = vi.fn()
  }
}))

vi.mock('@xterm/addon-unicode11', () => ({
  Unicode11Addon: class {}
}))

vi.mock('@xterm/addon-web-links', () => ({
  WebLinksAddon: class {}
}))

vi.mock('@xterm/addon-webgl', () => ({
  WebglAddon: class {
    clearTextureAtlas = vi.fn()
    dispose = vi.fn()
    onContextLoss = vi.fn()
  }
}))

vi.mock('@/components/ui/copy-button', () => ({
  writeClipboardText: vi.fn()
}))

vi.mock('@/lib/haptics', () => ({
  triggerHaptic: vi.fn()
}))

vi.mock('@/themes/context', () => ({
  useTheme: () => ({
    renderedMode: 'dark',
    theme: { terminal: {} },
    themeName: 'test'
  })
}))

vi.mock('./agent-terminal-stream', () => ({
  registerAgentTerminalWriter: terminalRegistrations.registerWriter
}))

vi.mock('./buffer', () => ({
  makeTerminalReader: terminalRegistrations.makeTerminalReader,
  registerTerminalReader: terminalRegistrations.registerReader
}))

function Harness({ active = false }: { active?: boolean }) {
  const { hostRef } = useAgentTerminal({ active, id: 'agent-tab', procId: 'proc-1' })

  return <div ref={hostRef} />
}

describe('useAgentTerminal', () => {
  let resolveFontLoad!: (faces: FontFace[]) => void
  let resizeObserverConstructor = vi.fn<() => void>()

  beforeEach(() => {
    const pendingFontLoad = new Promise<FontFace[]>(resolve => {
      resolveFontLoad = resolve
    })

    Object.defineProperty(globalThis.document, 'fonts', {
      configurable: true,
      value: { load: vi.fn(() => pendingFontLoad) }
    })

    resizeObserverConstructor = vi.fn<() => void>()
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor() {
          resizeObserverConstructor()
        }

        disconnect = vi.fn()
        observe = vi.fn()
        unobserve = vi.fn()
      } as unknown as typeof ResizeObserver
    )
  })

  afterEach(() => {
    vi.clearAllMocks()
    vi.unstubAllGlobals()
    Reflect.deleteProperty(globalThis.document, 'fonts')
  })

  it('async agent-terminal activation must not steal an already-focused prompt', async () => {
    const frames: FrameRequestCallback[] = []
    vi.stubGlobal('requestAnimationFrame', (fn: FrameRequestCallback) => {
      frames.push(fn)

      return frames.length
    })
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
    const prompt = document.createElement('input')
    prompt.id = 'prompt-witness'
    const terminalInput = document.createElement('textarea')
    terminalInput.id = 'terminal-witness'
    document.body.append(prompt, terminalInput)
    xterm.focus.mockImplementation(() => terminalInput.focus())
    const { rerender, unmount } = render(<Harness />)
    await waitFor(() => expect(document.fonts.load).toHaveBeenCalled())
    await act(async () => {
      resolveFontLoad([])
      await Promise.resolve()
    })
    await waitFor(() => expect(xterm.open).toHaveBeenCalledOnce())
    prompt.focus()
    expect(document.activeElement).toBe(prompt)
    rerender(<Harness active />)
    act(() => {
      while (frames.length) {
        frames.shift()!(0)
      }
    })
    const observed = document.activeElement?.id
    console.log('WITNESS asynchronous terminal activation focus target:', observed)
    unmount()
    prompt.remove()
    terminalInput.remove()
    expect(observed).toBe('prompt-witness')
  })

  it('unmounts safely while initial font preparation is pending', async () => {
    const { unmount } = render(<Harness />)

    await waitFor(() => expect(globalThis.document.fonts.load).toHaveBeenCalledTimes(3))

    expect(() => unmount()).not.toThrow()
    expect(xterm.dispose).toHaveBeenCalledOnce()
    expect(resizeObserverConstructor).not.toHaveBeenCalled()

    await act(async () => {
      resolveFontLoad([])
      await Promise.resolve()
    })

    expect(xterm.open).not.toHaveBeenCalled()
    expect(resizeObserverConstructor).not.toHaveBeenCalled()
    expect(terminalRegistrations.registerWriter).not.toHaveBeenCalled()
    expect(terminalRegistrations.registerReader).not.toHaveBeenCalled()
  })
})
