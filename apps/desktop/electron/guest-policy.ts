/** Main-owned permission and guest admission contract. Unknown frames deny. */
export function isAppDocument(raw: string, appUrl: string): boolean {
  try {
    const url = new URL(raw)
    const app = new URL(appUrl)

    if (app.protocol === 'file:') {
      return url.protocol === 'file:' && url.host === app.host && url.pathname === app.pathname
    }

    return ['http:', 'https:'].includes(app.protocol) && url.origin === app.origin
  } catch {
    return false
  }
}

export function installSessionPermissions(sess: any, trusted: (contents: any) => boolean, appUrl: () => string) {
  const allowed = (contents, permission, requestingOrigin, details, check) => {
    if (!contents || !trusted(contents) || details?.isMainFrame !== true) {
      return false
    }

    const url = details?.requestingUrl

    if (!url || !isAppDocument(url, appUrl()) || !isAppDocument(contents.getURL(), appUrl())) {
      return false
    }

    if (check) {
      // The CHECK API reports mediaType; do not infer audio from missing data.
      const origin = new URL(url).origin

      if (requestingOrigin !== origin && requestingOrigin !== url) {
        return false
      }

      return permission === 'media' && details.mediaType === 'audio'
    }

    return (
      permission === 'media' &&
      Array.isArray(details.mediaTypes) &&
      details.mediaTypes.length > 0 &&
      details.mediaTypes.every(type => type === 'audio')
    )
  }

  sess.setPermissionRequestHandler((contents, permission, callback, details) => {
    callback(allowed(contents, permission, '', details, false))
  })
  sess.setPermissionCheckHandler((contents, permission, origin, details) => {
    return allowed(contents, permission, origin, details, true)
  })
}

export function admitPreviewGuest(event: any, preferences: any, params: any, previewSession: () => any) {
  let validUrl = false

  try {
    validUrl = ['http:', 'https:', 'file:'].includes(new URL(params.src).protocol)
  } catch {
    /* deny */
  }

  if (params.partition !== 'persist:hermes-preview' || !validUrl || preferences.preload || params.preload) {
    event.preventDefault()

    return
  }

  preferences.session = previewSession()
  preferences.nodeIntegration = false
  preferences.nodeIntegrationInSubFrames = false
  preferences.contextIsolation = true
  preferences.sandbox = true
  preferences.webSecurity = true
  preferences.webviewTag = false
}
