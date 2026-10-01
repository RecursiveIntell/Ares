import { describe, expect, it } from 'vitest'

import { parseMultipleKeypresses } from '../parse-keypress.js'

import { InputEvent } from './input-event.js'

function parseOne(sequence: string) {
  const [keys] = parseMultipleKeypresses({ incomplete: '', mode: 'NORMAL' }, sequence)
  expect(keys).toHaveLength(1)

  return keys[0]!
}

describe('enhanced keyboard modifier parsing', () => {
  it.each(['kitty', 'modifyOtherKeys'] as const)(
    'restores shifted text while preserving canonical command names: %s',
    protocol => {
      for (const letter of ['a', 'r', 'z']) {
        for (const reported of [letter, letter.toUpperCase()]) {
          for (const modifier of [1, 2, 3, 4, 5, 6, 9, 10, 13, 14]) {
            const code = reported.charCodeAt(0)
            const sequence = protocol === 'kitty' ? `\u001b[${code};${modifier}u` : `\u001b[27;${modifier};${code}~`
            const event = new InputEvent(parseOne(sequence))
            const shifted = Boolean((modifier - 1) & 1)
            const controlled = Boolean((modifier - 1) & 4)

            expect(event.keypress.name).toBe(letter)
            expect(event.key.shift).toBe(shifted)
            expect(event.key.ctrl).toBe(controlled)
            expect(event.input).toBe(shifted && !controlled ? letter.toUpperCase() : letter)
            expect(event.input).not.toContain('\u001b')
          }
        }
      }
    }
  )

  it('keeps raw text and existing nonletter projections intact', () => {
    for (const [sequence, input] of [
      ['R', 'R'],
      ['é', 'é'],
      ['\u001b[32;2u', ' '],
      ['\u001b[13;2u', ''],
      ['\u001b[13;5u', ''],
      ['\u001b[27;2u', ''],
      ['\u001b[49;2u', '1'],
      ['\u001b[46;2u', '.'],
      ['\u001bOp', '0'],
      ['\u001b[57358u', '']
    ]) {
      expect(new InputEvent(parseOne(sequence!)).input).toBe(input)
    }
  })

  it('detects modified Enter sequences for multiline composer shortcuts', () => {
    const shiftEnter = new InputEvent(parseOne('\u001b[13;2u'))
    const ctrlEnter = new InputEvent(parseOne('\u001b[13;5u'))
    const modifyOtherShiftEnter = new InputEvent(parseOne('\u001b[27;2;13~'))

    expect(shiftEnter.key.return).toBe(true)
    expect(shiftEnter.key.shift).toBe(true)
    expect(shiftEnter.input).toBe('')

    expect(ctrlEnter.key.return).toBe(true)
    expect(ctrlEnter.key.ctrl).toBe(true)
    expect(ctrlEnter.input).toBe('')

    expect(modifyOtherShiftEnter.key.return).toBe(true)
    expect(modifyOtherShiftEnter.key.shift).toBe(true)
    expect(modifyOtherShiftEnter.input).toBe('')
  })

  it('preserves Cmd as super for kitty keyboard CSI-u sequences', () => {
    const parsed = parseOne('\u001b[99;9u')
    const event = new InputEvent(parsed)

    expect(parsed.name).toBe('c')
    expect(event.key.meta).toBe(false)
    expect(event.key.super).toBe(true)
  })

  it('preserves forwarded VS Code/Cursor Cmd+C copy sequence as ctrl+super+c', () => {
    const parsed = parseOne('\u001b[99;13u')
    const event = new InputEvent(parsed)

    expect(parsed.name).toBe('c')
    expect(event.key.ctrl).toBe(true)
    expect(event.key.super).toBe(true)
  })

  it('preserves Cmd on word-delete and word-navigation sequences', () => {
    const backspace = new InputEvent(parseOne('\u001b[127;9u'))
    const left = new InputEvent(parseOne('\u001b[1;9D'))
    const right = new InputEvent(parseOne('\u001b[1;9C'))

    expect(backspace.key.backspace).toBe(true)
    expect(backspace.key.super).toBe(true)

    expect(left.key.leftArrow).toBe(true)
    expect(left.key.super).toBe(true)

    expect(right.key.rightArrow).toBe(true)
    expect(right.key.super).toBe(true)
  })
})
