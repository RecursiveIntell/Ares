import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

import { expect, test } from 'vitest'

import { matchingReceiptMessages, recordedOwnersSettled } from '../e2e/native-state-identity'

test('same text in a foreign database, session, or role is not receipt custody', () => {
  const receipt = { db: 'state.db', receipt: { conversation_root: 'root-A', content: 'neutral input' } }
  const correct = { db: 'state.db', session_id: 'root-A', role: 'user', content: 'neutral input' }

  for (const wrong of [
    { ...correct, db: 'foreign.db' },
    { ...correct, session_id: 'unrelated' },
    { ...correct, role: 'assistant' },
  ]) {
    expect(matchingReceiptMessages({ receipts: [receipt], messages: [wrong] }, 'neutral input')).toEqual([])
  }

  expect(matchingReceiptMessages({ receipts: [receipt], messages: [correct] }, 'neutral input')).toEqual([correct])
  expect(matchingReceiptMessages({ receipts: [receipt, receipt], messages: [correct] }, 'neutral input')).toEqual([])
})

test('missing, unreadable, alive, or duplicate observations cannot establish recorded-owner retirement', () => {
  const old = [{ pid: 7, birth: '100' }]
  expect(recordedOwnersSettled(old, [])).toBe(false)

  for (const status of ['alive', 'unreadable', 'unknown']) {
    expect(recordedOwnersSettled(old, [{ ...old[0], status }])).toBe(false)
  }

  expect(recordedOwnersSettled(old, [{ ...old[0], status: 'gone' }])).toBe(true)
  expect(recordedOwnersSettled(old, [{ ...old[0], status: 'replaced', observed_birth: '200' }])).toBe(true)
  expect(recordedOwnersSettled(old, [{ ...old[0], status: 'replaced', observed_birth: '100' }])).toBe(false)
  expect(recordedOwnersSettled(old, [{ ...old[0], status: 'gone' }, { ...old[0], status: 'gone' }])).toBe(false)
  expect(recordedOwnersSettled([], [])).toBe(false)
})

test.skipIf(process.platform !== 'linux')('direct PID/birth inspection distinguishes present, replaced, absent, and permission-unknown', () => {
  const observer = fileURLToPath(new URL('../e2e/native-state-readback.py', import.meta.url))
  const program = `import importlib.util,json,os\nspec=importlib.util.spec_from_file_location('native_readback',${JSON.stringify(observer)})\nm=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(m)\npid=os.getpid()\nbirth=m._read_process_birth(pid)\nresult={'present':m.observe_process_identity(pid,birth),'replaced':m.observe_process_identity(pid,'0'),'absent':m.observe_process_identity(2147483647,'1')}\ndef denied(pid):\n    raise PermissionError('controlled stat refusal')\nm._read_process_birth=denied\nresult['unreadable']=m.observe_process_identity(pid,birth)\nprint(json.dumps(result))`

  const result = spawnSync('python3', ['-P', '-c', program], {
    encoding: 'utf8', timeout: 10_000,
    env: { PATH: process.env.PATH, HOME: process.env.HOME, HERMES_HOME: process.env.HERMES_HOME, LANG: 'C.UTF-8', TZ: 'UTC' },
  })

  expect(result.status, result.stderr).toBe(0)
  const value = JSON.parse(result.stdout)
  expect(value.present.status).toBe('alive')
  expect(value.replaced.status).toBe('replaced')
  expect(value.absent.status).toBe('gone')
  expect(value.unreadable.status).toBe('unreadable')
})
