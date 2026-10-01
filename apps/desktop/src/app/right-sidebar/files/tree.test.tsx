import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import { $renamingPath } from '@/store/file-actions'

import { ProjectTree } from './tree'
import type { TreeNode } from './use-project-tree'

// jsdom has no layout engine: give the container a real box so
// projectTreeViewportSize measures a non-zero viewport and the tree mounts
// (a 0×0 viewport renders the skeleton instead of rows).
const DOM_RECT = {
  height: 600,
  width: 240,
  top: 0,
  left: 0,
  bottom: 600,
  right: 240,
  x: 0,
  y: 0,
  toJSON: () => ({})
} as DOMRect

const data: TreeNode[] = [
  { id: '/w/a.ts', isDirectory: false, name: 'a.ts' },
  { id: '/w/src', isDirectory: true, name: 'src' }
]

function renderTree(overrides: Partial<Parameters<typeof ProjectTree>[0]> = {}) {
  const onPreviewFile = vi.fn()
  const onActivateFile = vi.fn()
  const onActivateFolder = vi.fn()
  const onNodeOpenChange = vi.fn()

  const view = render(
    <I18nProvider configClient={null} initialLocale="en">
      <ProjectTree
        collapseNonce={0}
        cwd="/w"
        data={data}
        onActivateFile={onActivateFile}
        onActivateFolder={onActivateFolder}
        onLoadChildren={vi.fn()}
        onNodeOpenChange={onNodeOpenChange}
        onPreviewFile={onPreviewFile}
        openState={{}}
        {...overrides}
      />
    </I18nProvider>
  )

  return { ...view, onActivateFile, onActivateFolder, onNodeOpenChange, onPreviewFile }
}

describe('ProjectTree context-menu clicks', () => {
  const writeClipboard = vi.fn().mockResolvedValue(undefined)

  beforeEach(() => {
    $renamingPath.set(null)
    writeClipboard.mockClear()
    vi.stubGlobal('hermesDesktop', { writeClipboard })
    // The resize-observer hook falls back to a single getBoundingClientRect
    // measurement when no ResizeObserver exists, so stubbing the element box
    // is enough to mount rows in jsdom.
    vi.stubGlobal('ResizeObserver', undefined)
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(() => DOM_RECT)
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    $renamingPath.set(null)
  })

  it.each([
    ['Copy Path', '/w/a.ts'],
    ['Copy Relative Path', 'a.ts']
  ])('%s runs its action without selecting or previewing the file', async (label, expectedPath) => {
    const { baseElement, container, onPreviewFile } = renderTree()
    const file = await screen.findByText('a.ts')
    const row = file.closest('[role="treeitem"]')

    expect(row).not.toBeNull()
    expect(row?.getAttribute('aria-selected')).toBe('false')
    fireEvent.contextMenu(file)

    // Exercise the actual Radix portal, outside both the row and React root's
    // DOM container, while still mounted in the rendered document body.
    const copyPath = await screen.findByRole('menuitem', { name: label })
    expect(baseElement.contains(copyPath)).toBe(true)
    expect(container.contains(copyPath)).toBe(false)
    expect(row?.contains(copyPath)).toBe(false)
    fireEvent.click(copyPath)

    await waitFor(() => expect(writeClipboard).toHaveBeenCalledExactlyOnceWith(expectedPath))
    expect(onPreviewFile).not.toHaveBeenCalled()
    expect(row?.getAttribute('aria-selected')).toBe('false')
  })

  it('a folder menu action does not toggle or select the folder', async () => {
    const { container, onNodeOpenChange, onPreviewFile } = renderTree()
    const folder = await screen.findByText('src')
    const row = folder.closest('[aria-expanded]')

    expect(row).not.toBeNull()
    expect(row?.getAttribute('aria-expanded')).toBe('false')
    fireEvent.contextMenu(folder)
    const copyPath = await screen.findByRole('menuitem', { name: 'Copy Path' })
    expect(container.contains(copyPath)).toBe(false)
    fireEvent.click(copyPath)

    await waitFor(() => expect(writeClipboard).toHaveBeenCalledExactlyOnceWith('/w/src'))
    expect(row?.getAttribute('aria-expanded')).toBe('false')
    expect(row?.getAttribute('aria-selected')).toBe('false')
    expect(onNodeOpenChange).not.toHaveBeenCalled()
    expect(onPreviewFile).not.toHaveBeenCalled()
  })

  it('a real single click on a row still selects it', async () => {
    const { onPreviewFile } = renderTree()

    await waitFor(() => {
      expect(screen.getByText('a.ts')).toBeTruthy()
    })

    fireEvent.click(screen.getByText('a.ts'))

    // Positive control: the containment guard must not swallow genuine row
    // clicks — arborist still selects the row.
    const row = screen.getByText('a.ts').closest('[aria-selected]')
    expect(row).not.toBeNull()
    await waitFor(() => {
      expect(row?.getAttribute('aria-selected')).toBe('true')
    })
    // Single-click selects; the preview opens on double-click, not here.
    expect(onPreviewFile).not.toHaveBeenCalled()
  })

  it('a real double click on a row still previews the file', async () => {
    const { onPreviewFile } = renderTree()

    await waitFor(() => {
      expect(screen.getByText('a.ts')).toBeTruthy()
    })

    fireEvent.doubleClick(screen.getByText('a.ts'))

    expect(onPreviewFile).toHaveBeenCalledWith('/w/a.ts')
  })

  it.each(['a.ts', 'src'])('shift-click still attaches %s without preview or expansion', async name => {
    const { onActivateFile, onActivateFolder, onNodeOpenChange, onPreviewFile } = renderTree()
    const row = await screen.findByText(name)

    fireEvent.click(row, { shiftKey: true })

    const onAttach = name === 'a.ts' ? onActivateFile : onActivateFolder
    const otherAttach = name === 'a.ts' ? onActivateFolder : onActivateFile
    expect(onAttach).toHaveBeenCalledExactlyOnceWith(`/w/${name}`)
    expect(otherAttach).not.toHaveBeenCalled()
    expect(onNodeOpenChange).not.toHaveBeenCalled()
    expect(onPreviewFile).not.toHaveBeenCalled()
  })

  it('a real click on the outer row still reaches arborist activation', async () => {
    const { onPreviewFile } = renderTree()
    const file = await screen.findByText('a.ts')
    const row = file.closest('[role="treeitem"]')

    if (!row) {
      throw new Error('Expected the real arborist row')
    }

    fireEvent.click(row)

    expect(row.getAttribute('aria-selected')).toBe('true')
    expect(onPreviewFile).toHaveBeenCalledExactlyOnceWith('/w/a.ts')
  })

  it.each(['ctrlKey', 'metaKey'])('%s still toggles outer-row selection without preview', async modifier => {
    const { onPreviewFile } = renderTree()
    const file = await screen.findByText('a.ts')
    const row = file.closest('[role="treeitem"]')

    if (!row) {
      throw new Error('Expected the real arborist row')
    }

    fireEvent.click(row, { [modifier]: true })
    expect(row.getAttribute('aria-selected')).toBe('true')
    fireEvent.click(row, { [modifier]: true })
    expect(row.getAttribute('aria-selected')).toBe('false')
    expect(onPreviewFile).not.toHaveBeenCalled()
  })
})
