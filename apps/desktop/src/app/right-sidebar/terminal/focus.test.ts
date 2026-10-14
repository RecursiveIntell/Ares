import { afterEach, describe, expect, it, vi } from 'vitest'

import { flushTerminalFocus, registerTerminalFocus, requestTerminalFocus, terminalHostVisible } from './focus'
afterEach(() => { document.body.replaceChildren() })
describe('terminal focus intent', () => {
  it('does nothing on passive readiness without explicit intent', () => {
    const focus = vi.fn(() => true)
    const stop = registerTerminalFocus('passive', focus)
    flushTerminalFocus('passive')
    expect(focus).not.toHaveBeenCalled()
    stop()
  })
  it('fulfills an explicit click after font readiness', () => {
    const button = document.createElement('button')
    const input = document.createElement('textarea')
    document.body.append(button, input); button.focus()
    requestTerminalFocus('clicked')

    const stop = registerTerminalFocus('clicked', () => { input.focus();

 return true })

    expect(document.activeElement).toBe(input)
    stop()
  })
  it('cancels a delayed terminal click after newer prompt focus', () => {
    const button = document.createElement('button'), prompt = document.createElement('input')
    document.body.append(button, prompt); button.focus()
    requestTerminalFocus('delayed'); prompt.focus()
    const focus = vi.fn(() => true), stop = registerTerminalFocus('delayed', focus)
    expect(focus).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(prompt)
    stop()
  })
  it('does not fulfill an intent for an inactive hidden host', () => {
    const parent = document.createElement('div'), host = document.createElement('div')
    parent.style.visibility = 'hidden'; parent.append(host); document.body.append(parent)
    expect(terminalHostVisible(host)).toBe(false)
    parent.style.visibility = 'visible'
    expect(terminalHostVisible(host)).toBe(true)
    parent.setAttribute('aria-hidden', 'true')
    expect(terminalHostVisible(host)).toBe(false)
  })
})
