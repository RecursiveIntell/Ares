// Tab selection/visibility is not permission to move keyboard focus.
// An explicit action records the current focus owner. Font readiness may fulfill
// it later only if no newer interaction has changed that owner.
const targets = new Map<string, () => boolean>()
let intent: { id: string; owner: Element | null } | null = null

// A→B→A focus or typing in the same prompt is still newer operator intent.
if (typeof window !== 'undefined') {
  window.addEventListener('pointerdown', () => { intent = null }, true)
  window.addEventListener('keydown', () => { intent = null }, true)
  window.addEventListener('focusin', event => {
    if (intent && event.target !== intent.owner) { intent = null }
  }, true)
}

export function requestTerminalFocus(id: string): void {
  const requested = { id, owner: document.activeElement }
  intent = requested
  flushTerminalFocus(id)
  // Only this explicit lease may wait for the first visible/font-ready frame.
  // Bounded; every newer key/pointer/focus intent cancels it immediately.
  let remaining = 120

  const ready = () => {
    if (intent !== requested) { return }
    flushTerminalFocus(id)

    if (intent === requested && --remaining > 0) { window.requestAnimationFrame(ready) }
    else if (intent === requested) { intent = null }
  }

  if (intent === requested) { window.requestAnimationFrame(ready) }
}

export function flushTerminalFocus(id: string): void {
  if (intent?.id !== id) { return }

  if (document.activeElement !== intent.owner) { intent = null;

 return }

  if (targets.get(id)?.()) { intent = null }
}

export function registerTerminalFocus(id: string, focus: () => boolean): () => void {
  targets.set(id, focus)
  flushTerminalFocus(id)

  return () => {
    if (targets.get(id) === focus) { targets.delete(id) }

    if (intent?.id === id) { intent = null }
  }
}

export function terminalHostVisible(host: HTMLElement): boolean {
  if (!host.isConnected || host.closest('[aria-hidden="true"], [data-pane-hidden]')) { return false }

  for (let node: HTMLElement | null = host; node; node = node.parentElement) {
    const style = getComputedStyle(node)

    if (style.visibility === 'hidden' || style.display === 'none') { return false }
  }

  return true
}
