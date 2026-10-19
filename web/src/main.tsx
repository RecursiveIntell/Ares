import { initializeDashboardBootstrap } from './lib/dashboard-bootstrap'

// Loading App/api/plugins before bootstrap would start protected requests with
// no admitted token. Keep the whole render graph behind this promise.
initializeDashboardBootstrap()
  .then(() => import('./render-dashboard'))
  .catch((error: unknown) => {
    const root = document.getElementById('root')
    if (root) root.textContent = error instanceof Error ? error.message : 'Dashboard bootstrap failed.'
  })
