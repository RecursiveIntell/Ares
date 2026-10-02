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
        url: moduleURL(`const fixture = globalThis.__groupTurnTestFixtures.get(${JSON.stringify(id)});
          export const useEffect = fixture.useEffect, useMemo = undefined, useRef = undefined, useState = fixture.useState;
          export const jsx = (type, props, key) => ({ type, props, key }), jsxs = jsx;`)
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
        source: `const { Date, setTimeout, clearTimeout } = globalThis.__groupTurnTestFixtures.get(${JSON.stringify(target.searchParams.get('groupTurnFixture'))});
          const fixturePorts = globalThis.__groupTurnTestFixtures.get(${JSON.stringify(target.searchParams.get('groupTurnFixture'))});
          const document = fixturePorts.document ?? globalThis.document;
          const setInterval = fixturePorts.setInterval ?? globalThis.setInterval;
          const clearInterval = fixturePorts.clearInterval ?? globalThis.clearInterval;\n${result.source}\nexport { runGroupChatMemberTurn, runGroupChatRounds,
          harvestStrandedGroupReply, stopGroupChatServerSync, currentGroupActivity, groupActivityLabel,
          $groupChats, $groupClarify, $botAttention, appendGroupChatEntry, syncGroupClarify, answerGroupClarify,
          groupChatSyncSnapshot, groupChatSyncEntryKey, stopGroupThread,
          mergeRemoteGroupChatSnapshotIntoRooms, durableGroupChatRooms, sendToGroupChat,
          groupRoomCoordinators, groupRuntimeSessionOwners, groupMemberKey, updateGroupChat,
          groupBlockedMembers, GroupBlockedNotice, CreateGroupChatDialog, createFreshGroupChat,
          groupComposerDraftKey, groupComposerDraftSnapshot, updateGroupComposerDraft };\n
          export function bindGroupTurnTestStorage(storage) { pluginCtx = { storage }; }\n`
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
