import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const cleanup: Array<() => void> = []

beforeEach(() => {
  window.localStorage.clear()
  document.body.replaceChildren()
  vi.resetModules()
})

afterEach(() => {
  for (const stop of cleanup.splice(0)) {
    stop()
  }
  document.body.replaceChildren()
})

async function terminals() {
  const $currentCwd = atom('/workspace')
  vi.doMock('@/store/session', () => ({ $currentCwd }))
  const store = await import('./terminals')
  const focus = await import('./focus')
  const first = store.createTerminal('/one', false)
  const second = store.createTerminal('/two', false)
  const firstHost = document.createElement('div')
  const secondHost = document.createElement('div')
  const firstInput = document.createElement('textarea')
  const secondInput = document.createElement('textarea')
  firstHost.dataset.terminal = first
  secondHost.dataset.terminal = second
  firstHost.append(firstInput)
  secondHost.append(secondInput)
  document.body.append(firstHost, secondHost)
  let secondReady = true
  cleanup.push(
    focus.registerTerminalFocus(first, () => {
      if (store.$activeTerminalId.get() !== first) {
        return false
      }
      firstInput.focus()

      return true
    })
  )
  cleanup.push(
    focus.registerTerminalFocus(second, () => {
      if (store.$activeTerminalId.get() !== second || !secondReady) {
        return false
      }
      secondInput.focus()

      return true
    })
  )
  store.selectTerminal(first)
  expect(document.activeElement).toBe(firstInput)

  return {
    ...store,
    ...focus,
    $currentCwd,
    first,
    second,
    firstHost,
    firstInput,
    secondInput,
    setSecondReady: (value: boolean) => {
      secondReady = value
    }
  }
}

describe('terminal navigation preserves keyboard ownership', () => {
  it('cycles next and previous with explicit focus intent', async () => {
    const t = await terminals()
    t.cycleTerminal(1)
    t.flushTerminalFocus(t.second)
    expect(t.$activeTerminalId.get()).toBe(t.second)
    expect(document.activeElement).toBe(t.secondInput)
    t.cycleTerminal(-1)
    t.flushTerminalFocus(t.first)
    expect(document.activeElement).toBe(t.firstInput)
  })

  it('closing the focused tab transfers to its replacement', async () => {
    const t = await terminals()
    t.closeActiveTerminal()
    t.flushTerminalFocus(t.second)
    expect(t.$activeTerminalId.get()).toBe(t.second)
    expect(document.activeElement).toBe(t.secondInput)
  })

  it('a close-transfer lease survives only automatic owner removal', async () => {
    const t = await terminals()
    t.setSecondReady(false)
    t.closeActiveTerminal()
    t.firstHost.remove()
    expect(document.activeElement).toBe(document.body)
    t.setSecondReady(true)
    t.flushTerminalFocus(t.second)
    expect(document.activeElement).toBe(t.secondInput)
  })

  it('new prompt focus cancels a delayed close transfer', async () => {
    const t = await terminals()
    t.setSecondReady(false)
    t.closeActiveTerminal()
    t.firstHost.remove()
    const prompt = document.createElement('input')
    document.body.append(prompt)
    prompt.focus()
    t.setSecondReady(true)
    t.flushTerminalFocus(t.second)
    expect(document.activeElement).toBe(prompt)
  })

  it('closing an active tab while the prompt owns focus does not take focus', async () => {
    const t = await terminals()
    const prompt = document.createElement('input')
    document.body.append(prompt)
    prompt.focus()
    t.closeActiveTerminal()
    t.flushTerminalFocus(t.second)
    expect(document.activeElement).toBe(prompt)
  })

  it('passive cwd selection cannot hide the actively typed terminal', async () => {
    const t = await terminals()
    t.$currentCwd.set('/two')
    expect(t.$activeTerminalId.get()).toBe(t.first)
    expect(document.activeElement).toBe(t.firstInput)
  })

  it('passive cwd selection follows the session without stealing prompt focus', async () => {
    const t = await terminals()
    const prompt = document.createElement('input')
    document.body.append(prompt)
    prompt.focus()
    t.$currentCwd.set('/two')
    t.flushTerminalFocus(t.second)
    expect(t.$activeTerminalId.get()).toBe(t.second)
    expect(document.activeElement).toBe(prompt)
  })

  it('ordinary navigation cannot borrow the close-removal exception', async () => {
    const t = await terminals()
    t.setSecondReady(false)
    t.selectTerminal(t.second)
    t.firstHost.remove()
    t.setSecondReady(true)
    t.flushTerminalFocus(t.second)
    expect(document.activeElement).toBe(document.body)
  })

  it('removing only an input is not removal of the admitted closing host', async () => {
    const t = await terminals()
    t.setSecondReady(false)
    t.closeActiveTerminal()
    t.firstInput.remove()
    t.setSecondReady(true)
    t.flushTerminalFocus(t.second)
    expect(document.activeElement).toBe(document.body)
  })

  it.each(['pointerdown', 'keydown', 'blur'])('new %s cancels a delayed close transfer', async event => {
    const t = await terminals()
    t.setSecondReady(false)
    t.closeActiveTerminal()
    t.firstHost.remove()
    window.dispatchEvent(new Event(event))
    t.setSecondReady(true)
    t.flushTerminalFocus(t.second)
    expect(document.activeElement).toBe(document.body)
  })

  it('passive closure never requests replacement focus', async () => {
    const t = await terminals()
    t.closeTerminal(t.first)
    t.firstHost.remove()
    t.flushTerminalFocus(t.second)
    expect(document.activeElement).toBe(document.body)
  })

  it('closing a background tab never moves the focused terminal', async () => {
    const t = await terminals()
    t.closeTerminal(t.second, true)
    t.flushTerminalFocus(t.first)
    expect(document.activeElement).toBe(t.firstInput)
  })
})
