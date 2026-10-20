// Tab selection/visibility is not permission to move keyboard focus.
// An explicit action records the current focus owner. Font readiness may fulfill
// it later only if no newer interaction has changed that owner.
const targets = new Map<string, () => boolean>()
interface FocusIntent {
  id: string
  owner: Element | null
  closingHost: Element | null
}
let intent: FocusIntent | null = null

function ownerMatches(request: FocusIntent, current: Element | null): boolean {
  if (current === request.owner) {
    return true
  }

  // Only an explicit close transfer may survive removal of its exact host.
  // An unrelated blur/removal, or an ordinary pending click, has no such grant.
  return (
    current === document.body &&
    request.closingHost !== null &&
    !request.closingHost.isConnected &&
    request.owner !== null &&
    !request.owner.isConnected
  )
}

export function terminalOwnsKeyboardFocus(id: string): boolean {
  return (
    typeof document !== 'undefined' &&
    document.activeElement?.closest('[data-terminal]')?.getAttribute('data-terminal') === id
  )
}

// A→B→A focus or typing in the same prompt is still newer operator intent.
if (typeof window !== 'undefined') {
  window.addEventListener(
    'pointerdown',
    () => {
      intent = null
    },
    true
  )
  window.addEventListener(
    'keydown',
    () => {
      intent = null
    },
    true
  )
  // Element blur does not bubble; this listens only for the window losing focus.
  window.addEventListener('blur', () => {
    intent = null
  })
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      intent = null
    }
  })
  window.addEventListener(
    'focusin',
    event => {
      if (intent && !ownerMatches(intent, event.target instanceof Element ? event.target : null)) {
        intent = null
      }
    },
    true
  )
}

export function requestTerminalFocus(id: string): void {
  requestFocus(id, null)
}

export function requestTerminalFocusAfterClose(id: string, closingId: string): void {
  const host = document.activeElement?.closest('[data-terminal]')

  if (id === closingId || !host || host.getAttribute('data-terminal') !== closingId) {
    return
  }

  requestFocus(id, host)
}

function requestFocus(id: string, closingHost: Element | null): void {
  const requested = { id, owner: document.activeElement, closingHost }
  intent = requested
  flushTerminalFocus(id)
  // Only this explicit lease may wait for the first visible/font-ready frame.
  // Bounded; every newer key/pointer/focus intent cancels it immediately.
  let remaining = 120

  const ready = () => {
    if (intent !== requested) {
      return
    }

    flushTerminalFocus(id)

    if (intent === requested && --remaining > 0) {
      window.requestAnimationFrame(ready)
    } else if (intent === requested) {
      intent = null
    }
  }

  if (intent === requested) {
    window.requestAnimationFrame(ready)
  }
}

export function flushTerminalFocus(id: string): void {
  if (intent?.id !== id) {
    return
  }

  if (!ownerMatches(intent, document.activeElement)) {
    intent = null

    return
  }

  if (targets.get(id)?.()) {
    intent = null
  }
}

export function registerTerminalFocus(id: string, focus: () => boolean): () => void {
  targets.set(id, focus)
  flushTerminalFocus(id)

  return () => {
    if (targets.get(id) === focus) {
      targets.delete(id)
    }

    if (intent?.id === id) {
      intent = null
    }
  }
}

export function terminalHostVisible(host: HTMLElement): boolean {
  if (!host.isConnected || host.closest('[aria-hidden="true"], [data-pane-hidden]')) {
    return false
  }

  for (let node: HTMLElement | null = host; node; node = node.parentElement) {
    const style = getComputedStyle(node)

    if (style.visibility === 'hidden' || style.display === 'none') {
      return false
    }
  }

  return true
}
