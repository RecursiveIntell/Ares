import { registerHooks } from 'node:module'

// Import the whole real plugin with inert renderer dependencies. The loader
// exposes its existing private engine seam; tests make no source-text assertions.
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
  },
  load(url, context, nextLoad) {
    const result = nextLoad(url, context)
    const target = new URL(url)
    if (target.pathname === pluginURL.pathname && target.searchParams.has('groupTurnFixture')) {
      return {
        ...result,
        source: `const { Date, setTimeout, clearTimeout } = globalThis.__groupTurnTestFixtures.get(${JSON.stringify(target.searchParams.get('groupTurnFixture'))});\nconst fixturePorts = globalThis.__groupTurnTestFixtures.get(${JSON.stringify(target.searchParams.get('groupTurnFixture'))});\n          const document = fixturePorts.document ?? globalThis.document;\n          const setInterval = fixturePorts.setInterval ?? globalThis.setInterval;\n          const clearInterval = fixturePorts.clearInterval ?? globalThis.clearInterval;\n${result.source}\nexport { runGroupChatMemberTurn, runGroupChatRounds,
          harvestStrandedGroupReply, stopGroupChatServerSync, currentGroupActivity, groupActivityLabel,
          $groupChats, $groupClarify, $botAttention, appendGroupChatEntry, syncGroupClarify, answerGroupClarify,
          groupChatSyncSnapshot, groupChatSyncEntryKey, stopGroupThread,
          mergeRemoteGroupChatSnapshotIntoRooms, durableGroupChatRooms, sendToGroupChat,
          groupRoomCoordinators, groupRuntimeSessionOwners, groupMemberKey, updateGroupChat,
          groupBlockedMembers, GroupBlockedNotice, CreateGroupChatDialog, createFreshGroupChat,
          groupComposerDraftKey, groupComposerDraftSnapshot, updateGroupComposerDraft };\n
          export const groupRecoveryTestAPI = {\n            createFreshGroupChat: typeof createFreshGroupChat === 'function' ? createFreshGroupChat : undefined,\n            GroupBlockedNotice: typeof GroupBlockedNotice === 'function' ? GroupBlockedNotice : undefined,\n            groupBlockedMembers: typeof groupBlockedMembers === 'function' ? groupBlockedMembers : undefined,\n            updateGroupComposerDraft, GroupChatWorkspace, CreateGroupChatDialog, groupComposerDraftSnapshot, groupComposerDraftKey };\n          export function bindGroupTurnTestStorage(storage) { pluginCtx = { storage }; }\n`
      }
    }
    return result
  }
})

export async function importGroupTurnPlugin(fixture) {
  const id = String(++sequence)
  fixtures.set(id, fixture)
  const url = new URL(pluginURL)
  url.searchParams.set('groupTurnFixture', id)
  try {
    return await import(url.href)
  } finally {
    fixtures.delete(id)
  }
}
