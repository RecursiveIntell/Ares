import { defineConfig, type Plugin } from 'vite'
import babel from '@rolldown/plugin-babel'
import react, { reactCompilerPreset } from '@vitejs/plugin-react'

/** React Compiler preset scoped to modules that can actually contain
 *  components/hooks (JSX syntax or a react-ish import). The preset's default
 *  code filter matches any PascalCase/use* declaration — effectively every TS
 *  module — which made the babel pass parse the whole codebase. */
function compilerPreset() {
  const preset = reactCompilerPreset()
  // The filter is optional in the plugin's types (rolldown filters may be
  // expressed via id instead of code); guard rather than assume.
  if (preset.rolldown.filter) {
    preset.rolldown.filter.code = /\/>|<\/|from\s*['"][^'"]*react/
  }
  return preset
}
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

const BACKEND = process.env.HERMES_DASHBOARD_URL ?? 'http://127.0.0.1:9119'

/** Dev HTML carries only non-secret mode flags. Browser launch grants are
 * redeemed through the same-origin dev proxy, never scraped from public HTML. */
function hermesDevMetadata(): Plugin {
  return {
    name: 'hermes:dev-bootstrap-metadata',
    apply: 'serve',
    async transformIndexHtml() {
      try {
        const response = await fetch(BACKEND, { redirect: 'error', signal: AbortSignal.timeout(3000) })
        const html = await response.text()
        const gated = html.match(/window\.__HERMES_AUTH_REQUIRED__\s*=\s*(true|false)/)?.[1]
        const embedded = html.match(/window\.__HERMES_DASHBOARD_EMBEDDED_CHAT__\s*=\s*(true|false)/)?.[1]
        if (!gated) throw new Error('Bootstrap metadata unavailable')
        return [
          {
            tag: 'script',
            injectTo: 'head',
            children: `window.__HERMES_AUTH_REQUIRED__=${gated};window.__HERMES_DASHBOARD_EMBEDDED_CHAT__=${embedded ?? 'true'};`
          }
        ]
      } catch {
        // No token is granted by this fallback; the protected POST still must
        // admit a launch grant. A gated/unavailable backend will deny it.
        return [{ tag: 'script', injectTo: 'head', children: 'window.__HERMES_AUTH_REQUIRED__=false;' }]
      }
    }
  }
}

export default defineConfig({
  plugins: [react(), babel({ presets: [compilerPreset()] }), tailwindcss(), hermesDevMetadata()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
      '@hermes/shared': path.resolve(__dirname, '../apps/shared/src')
    },
    // When @nous-research/ui is symlinked via `file:../../design-language`,
    // Node's module resolution would pick up shared deps from
    // design-language/node_modules/*, giving us two copies + breaking
    // hooks (useRef-of-null), webgl contexts, etc. Force everything that
    // exists in BOTH places to use the dashboard's copy.
    //
    // Don't list packages here that only exist in the DS (nanostores,
    // @nanostores/react) — Vite dedupe errors out when it can't find
    // them at the project root.
    dedupe: ['react', 'react-dom', '@react-three/fiber', '@observablehq/plot', 'three', 'leva', 'gsap']
  },
  build: {
    outDir: '../hermes_cli/web_dist',
    emptyOutDir: true,
    // Shell stays a bit over Vite's 500 kB default after vendor splits;
    // page/xterm chunks load on demand. Keep a modest ceiling so a true
    // regression still warns.
    chunkSizeWarningLimit: 600,
    // Split heavy vendors so the first dashboard paint does not download
    // xterm/three/plot/etc. until a route actually needs them. Lazy page
    // imports in App.tsx create the route boundaries; these groups keep
    // shared node_modules out of every page chunk.
    rolldownOptions: {
      output: {
        codeSplitting: {
          minSize: 20_000,
          groups: [
            {
              name: 'react-vendor',
              test: /node_modules[\\/](react|react-dom|scheduler|react-router|react-router)([\\/]|$)/
            },
            {
              name: 'xterm',
              test: /node_modules[\\/]@xterm[\\/]/
            },
            {
              name: 'three',
              test: /node_modules[\\/](three|@react-three)([\\/]|$)/
            },
            {
              name: 'plot',
              test: /node_modules[\\/]@observablehq[\\/]plot([\\/]|$)/
            },
            {
              name: 'motion',
              test: /node_modules[\\/](motion|framer-motion)([\\/]|$)/
            },
            {
              name: 'ui',
              test: /node_modules[\\/]@nous-research[\\/]ui([\\/]|$)/
            },
            {
              name: 'vendor',
              test: /node_modules[\\/]/
            }
          ]
        }
      }
    }
  },
  server: {
    proxy: {
      '/auth': { target: BACKEND, changeOrigin: false },
      '/api': {
        target: BACKEND,
        ws: true,
        changeOrigin: false
      },
      // Same host as `hermes dashboard` must serve these; Vite has no
      // dashboard-plugins/* files, so without this, plugin scripts 404
      // or receive index.html in dev.
      '/dashboard-plugins': BACKEND
    }
  }
})
