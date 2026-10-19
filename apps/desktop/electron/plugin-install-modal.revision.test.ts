// RIC-03: execute the production renderer component, its click handler and the
// production registered IPC adapters. Only presentation/store dependencies and
// Electron registration are facades; Git, checkout and publication are real.
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import { build } from 'esbuild'
import { JSDOM } from 'jsdom'
import { expect, it, vi } from 'vitest'

const handlers = vi.hoisted(() => new Map<string, (...args: any[]) => any>())
vi.mock('electron', () => ({
  ipcMain: { handle: (name: string, fn: (...args: any[]) => any) => handlers.set(name, fn) },
  shell: {}
}))
import { registerFsIpc } from './fs-ipc'

it.skipIf(process.platform === 'win32')(
  'real renderer installs the inspected SHA after the branch moves',
  async () => {
    const here = path.dirname(fileURLToPath(import.meta.url))
    const appRequire = createRequire(path.join(here, '../package.json'))
    const base = fs.mkdtempSync(path.join(os.tmpdir(), 'ric03-renderer-'))
    const home = path.join(base, 'home')
    const repo = path.join(base, 'alerts.git')
    fs.mkdirSync(home)
    fs.mkdirSync(repo)
    const git = (...args: string[]) => execFileSync('git', args, { cwd: repo, encoding: 'utf8' }).trim()
    git('init', '-q', '-b', 'main')
    git('config', 'user.name', 'Renderer fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    const effect = path.join(base, 'import-effects')
    const oldBytes = `import fs from 'node:fs'; fs.writeFileSync(${JSON.stringify(effect)}, 'imported'); throw new Error('candidate code must never be imported'); // inspected\n`
    fs.writeFileSync(path.join(repo, 'plugin.js'), oldBytes)
    git('add', '.')
    git('commit', '-qm', 'inspected fixture')
    const sha = git('rev-parse', 'HEAD')
    const identifier = pathToFileURL(repo).href
    registerFsIpc({
      hermesHome: home,
      readActiveDesktopProfile: () => null,
      expandUserPath: v => v,
      resolveRequestedPathForIpc: v => v,
      directoryExists: () => true,
      resolveGitBinary: () => 'git'
    })
    const payloads: any[] = []
    let inspected: any
    const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' })
    const globals = globalThis as any

    const saved = Object.fromEntries(
      ['window', 'document', 'navigator', 'IS_REACT_ACT_ENVIRONMENT', '__p1bRenderer'].map(k => [
        k,
        Object.getOwnPropertyDescriptor(globals, k)
      ])
    )

    const messages = {
      title: 'Install plugin',
      install: 'Install',
      installing: 'Installing',
      probeUnavailable: 'Unavailable',
      agentTargetLocal: () => 'local',
      agentTargetRemote: () => 'remote',
      desktopSuccess: (name: string) => name,
      agentSuccess: (name: string) => name
    }

    const request = { repo: identifier, legacyHint: 'desktop' }

    const noop = () => {}

    const mock = {
      request,
      gateway: { requestGateway: noop },
      t: { settings: { plugins: { installModal: messages } }, common: { cancel: 'Cancel' } },
      close: noop,
      navigate: noop,
      discover: vi.fn(async () => {}),
      load: vi.fn(async () => {})
    }

    let root: any

    try {
      for (const [key, value] of Object.entries({
        window: dom.window,
        document: dom.window.document,
        navigator: dom.window.navigator,
        IS_REACT_ACT_ENVIRONMENT: true,
        __p1bRenderer: mock
      })) {
        Object.defineProperty(globals, key, { value, configurable: true, writable: true })
      }

      ;(dom.window as any).hermesDesktop = {
        probePluginRepo: async (payload: any) => {
          inspected = await handlers.get('hermes:plugin:probe')!({}, payload)

          return inspected
        },
        installDesktopPlugin: async (payload: any) => {
          payloads.push(payload)

          return handlers.get('hermes:plugin:installDesktop')!({}, payload)
        }
      }

      const modules: Record<string, string> = {
        '@nanostores/react': 'export const useStore = s => s',
        'react-router':
          'export const useNavigate=()=>globalThis.__p1bRenderer.navigate; export const useLocation=()=>({pathname:"/chat"})',
        '@/app/gateway/hooks/use-gateway-request':
          'export const useGatewayRequest=()=>globalThis.__p1bRenderer.gateway',
        '@/app/routes': 'export const NEW_CHAT_ROUTE="/chat", SETTINGS_ROUTE="/settings"',
        '@/contrib/runtime-loader': 'export const discoverRuntimePlugins=()=>globalThis.__p1bRenderer.discover()',
        '@/i18n': 'export const useI18n=()=>({t:globalThis.__p1bRenderer.t})',
        '@/lib/plugin-source-urls': 'export const resolvePluginSourceLinks=()=>null',
        '@/store/agent-plugins':
          'export const installAgentPlugin=()=>{throw Error("unexpected agent install")}; export const loadAgentPlugins=()=>globalThis.__p1bRenderer.load()',
        '@/store/notifications': 'export const notify=()=>{}',
        '@/store/plugin-install-request':
          'export const $pluginInstallRequest=globalThis.__p1bRenderer.request; export const closePluginInstallRequest=()=>globalThis.__p1bRenderer.close()',
        '@/store/profile': 'export const $activeGatewayProfile=null, $profileScope=null',
        '@/store/session': 'export const $connection={mode:"local"}'
      }

      const ui = 'import {createElement as h} from "react"; const Box=({children})=>h("div",null,children); '
      modules['@/components/ui/button'] =
        ui + 'export const Button=({children,onClick,disabled})=>h("button",{onClick,disabled},children)'
      modules['@/components/ui/dialog'] =
        ui +
        'export const Dialog=Box,DialogContent=Box,DialogDescription=Box,DialogFooter=Box,DialogHeader=Box,DialogTitle=Box; export const preventCloseButtonAutoFocus=()=>{}'
      modules['@/components/ui/checkbox'] = ui + 'export const Checkbox=()=>h("input",{type:"checkbox"})'
      modules['@/components/ui/switch'] = ui + 'export const Switch=()=>h("input",{type:"checkbox"})'
      modules['@/lib/external-link'] = ui + 'export const ExternalLink=Box'
      modules['@/lib/icons'] = 'export const AlertTriangle=()=>null'
      const outfile = path.join(base, 'modal.cjs')
      await build({
        entryPoints: [path.join(here, '../src/app/settings/plugin-install-modal.tsx')],
        bundle: true,
        platform: 'node',
        format: 'cjs',
        jsx: 'automatic',
        outfile,
        plugins: [
          {
            name: 'renderer-fixture-boundaries',
            setup(builder) {
              builder.onResolve({ filter: /^(?:@\/|@nanostores\/react$|react-router$)/ }, args => {
                if (!(args.path in modules)) {
                  throw Error(`Unaccounted renderer dependency: ${args.path}`)
                }

                return { path: args.path, namespace: 'p1b-fixture' }
              })
              builder.onLoad({ filter: /.*/, namespace: 'p1b-fixture' }, args => ({
                contents: modules[args.path],
                loader: 'js'
              }))
              builder.onResolve({ filter: /^react(?:\/.*)?$/ }, args => ({
                path: appRequire.resolve(args.path),
                external: true
              }))
            }
          }
        ]
      })
      const { act, createElement } = appRequire('react')
      const { createRoot } = appRequire('react-dom/client')
      const { PluginInstallModal } = appRequire(outfile)
      root = createRoot(dom.window.document.getElementById('root'))
      await act(async () => {
        root.render(createElement(PluginInstallModal))
      })

      // Let the real async probe complete without assuming a fixed Git duration.
      for (let n = 0; n < 100 && !inspected; n++) {
        await act(async () => {
          await new Promise(r => setTimeout(r, 20))
        })
      }

      expect(inspected).toMatchObject({ ok: true, revision: sha })
      await act(async () => {
        await Promise.resolve()
      })
      fs.writeFileSync(path.join(repo, 'plugin.js'), '// changed default branch\n')
      git('add', '.')
      git('commit', '-qm', 'branch moved fixture')
      expect(git('rev-parse', 'HEAD')).not.toBe(sha)
      const button = [...dom.window.document.querySelectorAll('button')].find(b => b.textContent === 'Install')!
      expect(button.disabled).toBe(false)
      await act(async () => {
        button.click()
      })

      for (let n = 0; n < 100 && !mock.discover.mock.calls.length; n++) {
        await act(async () => {
          await new Promise(r => setTimeout(r, 20))
        })
      }

      expect(payloads).toEqual([{ identifier, force: false, revision: sha }])
      expect(mock.discover).toHaveBeenCalledOnce()
      expect(fs.readFileSync(path.join(home, 'desktop-plugins/alerts/plugin.js'), 'utf8')).toBe(oldBytes)
      expect(fs.existsSync(effect)).toBe(false)
    } finally {
      if (root) {
        const { act } = appRequire('react')
        await act(async () => {
          root.unmount()
        })
      }

      for (const [key, descriptor] of Object.entries(saved)) {
        if (descriptor) {
          Object.defineProperty(globals, key, descriptor)
        } else {
          delete globals[key]
        }
      }

      dom.window.close()
      fs.rmSync(base, { recursive: true, force: true })
      handlers.clear()
    }
  },
  30_000
)
