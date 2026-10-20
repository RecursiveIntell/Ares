import { registerHooks } from 'node:module'

// Import the unmodified shipped plugin; mock only its explicit dependencies.
const fixtures = new Map()
globalThis.__groupTurnTestFixtures = fixtures
let sequence = 0
const pluginURL = new URL('../plugin.js', import.meta.url)
const sdkNames = [
  'Button', 'Checkbox', 'cn', 'Codicon', 'ContextMenu', 'ContextMenuContent',
  'ContextMenuItem', 'ContextMenuSeparator', 'ContextMenuTrigger', 'ConfirmDialog',
  'CopyButton', 'Dialog', 'DialogContent', 'DialogDescription', 'DialogFooter',
  'DialogHeader', 'DialogTitle', 'DropdownMenu', 'DropdownMenuContent',
  'DropdownMenuItem', 'DropdownMenuSeparator', 'DropdownMenuTrigger', 'EmptyState',
  'GlyphSpinner', 'haptic', 'Input', 'profileColor', 'queryClient', 'relativeTime',
  'ScrollArea', 'SearchField', 'Select', 'SelectContent', 'SelectItem', 'SelectTrigger',
  'SelectValue', 'Switch', 'surfaceModelSwitchConfirm', 'Textarea', 'Tip', 'useQuery', 'useValue'
]
const moduleURL = source => `data:text/javascript,${encodeURIComponent(source)}`

registerHooks({
  resolve(specifier, context, nextResolve) {
    const id = context.parentURL && new URL(context.parentURL).searchParams.get('groupTurnFixture')
    if (id && specifier === '@hermes/plugin-sdk') {
      return {
        shortCircuit: true,
        url: moduleURL(`const fixture = globalThis.__groupTurnTestFixtures.get(${JSON.stringify(id)});
          export const atom = fixture.atom, host = fixture.host;
          export const PALETTE_AREA = 'palette', COMPOSER_AREAS = { middleware: 'middleware' };
          ${sdkNames.map(name => `export const ${name} = fixture.sdk?.[${JSON.stringify(name)}];`).join('\n')}`)
      }
    }
    if (id && (specifier === 'react' || specifier === 'react/jsx-runtime')) {
      return {
        shortCircuit: true,
        url: moduleURL(`const fixture = globalThis.__groupTurnTestFixtures.get(${JSON.stringify(id)});\n          ${['useEffect', 'useMemo', 'useRef', 'useState', 'jsx', 'jsxs'].map(name => `export const ${name} = fixture.react?.[${JSON.stringify(name)}] ?? fixture[${JSON.stringify(name)}]${['jsx','jsxs'].includes(name) ? ' ?? ((type, props, key) => ({ type, props, key }))' : ''};`).join('\n')}`)
      }
    }
    return nextResolve(specifier, context)
  }
})

export async function importGroupTurnPlugin(fixture) {
  const id = String(++sequence)
  fixtures.set(id, fixture)
  const url = new URL(pluginURL)
  url.searchParams.set('groupTurnFixture', id)
  try {
    const module = await import(url.href)
    const runtime = module.default.groupTurnRuntime
    runtime.bindGroupTurnPorts(fixture)
    return { ...module, ...runtime,
      bindGroupTurnTestStorage: runtime.bindGroupTurnStorage,
      groupRecoveryTestAPI: runtime }
  } finally {
    fixtures.delete(id)
  }
}
