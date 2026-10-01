import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'

import { renderSync } from '@hermes/ink'
import React, { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { TextInput } from '../components/textInput.js'

// Use the real host platform policy. These tests never replace isActionMod or process.platform.
class Input extends EventEmitter {
  chunks: string[] = []
  isRaw = false
  isTTY = true
  readableLength = 0

  read() {
    const next = this.chunks.shift() ?? null
    this.readableLength = this.chunks.length

    return next
  }

  ref() {}
  unref() {}
  setEncoding() {}

  setRawMode(enabled: boolean) {
    this.isRaw = enabled
  }

  send(...chunks: string[]) {
    this.chunks.push(...chunks)
    this.readableLength = this.chunks.length
    this.emit('readable')
  }
}

type Protocol = 'kitty' | 'modifyOtherKeys'

const chord = (protocol: Protocol, letter: string, modifier: number) =>
  protocol === 'kitty'
    ? `\u001b[${letter.charCodeAt(0)};${modifier}u`
    : `\u001b[27;${modifier};${letter.charCodeAt(0)}~`

async function withComposer(
  run: (input: Input, waitValue: (value: string) => Promise<void>, changes: string[]) => Promise<void>
) {
  const stdin = new Input()
  const stdout = new PassThrough()
  const stderr = new PassThrough()
  const changes: string[] = []
  Object.assign(stdout, { columns: 80, isTTY: false, rows: 24 })
  Object.assign(stderr, { columns: 80, isTTY: false, rows: 24 })

  function Harness() {
    const [value, setValue] = useState('')

    return (
      <TextInput
        columns={80}
        onChange={next => {
          changes.push(next)
          setValue(next)
        }}
        onSubmit={() => {}}
        value={value}
      />
    )
  }

  const instance = renderSync(React.createElement(Harness), {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })

  const waitValue = async (value: string) => {
    await vi.waitFor(() => expect(changes.at(-1)).toBe(value), { interval: 10, timeout: 3000 })
  }

  try {
    await vi.waitFor(() => expect(stdin.listenerCount('readable')).toBeGreaterThan(0), { interval: 10, timeout: 3000 })
    await run(stdin, waitValue, changes)
  } finally {
    instance.unmount()
    instance.cleanup()
  }
}

describe.runIf(process.platform === 'linux' || process.platform === 'win32')('native Ctrl-action undo and redo', () => {
  it.each(['kitty', 'modifyOtherKeys'] as const)(
    'redos Shift+Z without a second undo or inserted letter: %s',
    async protocol => {
      await withComposer(async (input, waitValue, changes) => {
        input.send('a')
        await waitValue('a')
        input.send('b')
        await waitValue('ab')
        input.send(chord(protocol, 'z', 5))
        await waitValue('a')
        input.send(chord(protocol, 'z', 6))
        await waitValue('ab')
        input.send(chord(protocol, 'z', 5))
        await waitValue('a')
        input.send(chord(protocol, 'y', 5))
        await waitValue('ab')
        // Empty redo must neither undo again nor type a letter.
        input.send(chord(protocol, 'z', 6), '!')
        await waitValue('ab!')
        expect(changes).not.toContain('aZ')
        expect(changes).not.toContain('az')
      })
    }
  )

  it.each(['kitty', 'modifyOtherKeys'] as const)(
    'new edits invalidate redo; ordinary z and Z remain text: %s',
    async protocol => {
      await withComposer(async (input, waitValue) => {
        input.send('a')
        await waitValue('a')
        input.send('b')
        await waitValue('ab')
        input.send(chord(protocol, 'z', 5))
        await waitValue('a')
        input.send('c')
        await waitValue('ac')
        input.send(chord(protocol, 'z', 6), 'z', 'Z')
        await waitValue('aczZ')
      })
    }
  )

  it('retains raw Ctrl+Y redo', async () => {
    await withComposer(async (input, waitValue) => {
      input.send('a')
      await waitValue('a')
      input.send('b')
      await waitValue('ab')
      input.send('\u001a')
      await waitValue('a')
      input.send('\u0019')
      await waitValue('ab')
    })
  })

  it.each(['kitty', 'modifyOtherKeys'] as const)(
    'preserves native action letter commands with or without Shift: %s',
    async protocol => {
      for (const modifier of [5, 6]) {
        for (const [letter, start, expected] of [
          ['a', false, modifier === 6 ? 'X' : 'Xone two'],
          ['e', true, modifier === 6 ? 'X' : 'one twoX'],
          ['u', false, 'X'],
          ['k', true, 'X'],
          ['w', false, 'one X']
        ] as const) {
          await withComposer(async (input, waitValue) => {
            input.send('one two')
            await waitValue('one two')

            if (start) {
              input.send('\u0001')
            }

            input.send(chord(protocol, letter, modifier), 'X')
            await waitValue(expected)
          })
        }
      }
    }
  )
})

describe.runIf(process.platform === 'linux')('native Linux modifier/text separation', () => {
  it.each(['kitty', 'modifyOtherKeys', 'raw'] as const)(
    'preserves frozen word-command behavior: %s',
    async protocol => {
      for (const letter of ['b', 'f', 'd']) {
        await withComposer(async (input, waitValue) => {
          input.send('one two')
          await waitValue('one two')

          if (letter !== 'b') {
            input.send('\u0001')
          }

          const sequence = protocol === 'raw' ? `\u001b${letter.toUpperCase()}` : chord(protocol, letter, 4)
          input.send(sequence, 'X')

          const expected =
            protocol === 'raw'
              ? { b: 'one twoBX', f: 'FXone two', d: 'DXone two' }[letter]
              : letter === 'd'
                ? 'Xtwo'
                : 'one Xtwo'

          await waitValue(expected!)
        })
      }
    }
  )

  it.each(['kitty', 'modifyOtherKeys'] as const)(
    'keeps unshifted Alt word commands and non-action Super/Meta text: %s',
    async protocol => {
      for (const letter of ['b', 'f', 'd']) {
        await withComposer(async (input, waitValue) => {
          input.send('one two')
          await waitValue('one two')

          if (letter !== 'b') {
            input.send('\u0001')
          }

          input.send(chord(protocol, letter, 3), 'X')
          await waitValue(letter === 'd' ? 'Xtwo' : 'one Xtwo')
        })
      }

      for (const modifier of [4, 10]) {
        await withComposer(async (input, waitValue) => {
          input.send('a')
          await waitValue('a')
          input.send(chord(protocol, 'z', modifier))
          await waitValue('aZ')
        })
      }
    }
  )
})

// These are native-host gates, not a mocked Cmd claim on the Linux CI lane.
describe.runIf(process.platform === 'darwin')('native macOS action undo and redo', () => {
  it.each(['kittySuper', 'kittyMeta', 'rawMeta'] as const)('redos shifted Cmd representations: %s', async mode => {
    await withComposer(async (input, waitValue) => {
      input.send('a')
      await waitValue('a')
      input.send('b')
      await waitValue('ab')
      input.send(mode === 'rawMeta' ? '\u001bz' : chord('kitty', 'z', mode === 'kittySuper' ? 9 : 3))
      await waitValue('a')
      input.send(mode === 'rawMeta' ? '\u001bZ' : chord('kitty', 'z', mode === 'kittySuper' ? 10 : 4))
      await waitValue('ab')
    })
  })
})
