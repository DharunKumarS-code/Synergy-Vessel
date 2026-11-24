import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// The FastAPI backend (dashboard/backend) runs on 127.0.0.1:8000 by default.
// In development /api is proxied there, so the browser stays same-origin.
// Override with VITE_API_PROXY_TARGET in dashboard/frontend/.env.local if needed.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  const target = env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000'
  const proxy = {
    '/api': {
      target,
      changeOrigin: true,
      // Backend unreachable -> 502 (shown as "service unavailable"), not a generic 500.
      configure: (p) =>
        p.on('error', (_err, _req, res) => {
          if (res && !res.headersSent && typeof res.writeHead === 'function') {
            res.writeHead(502, { 'Content-Type': 'application/json' })
            res.end(JSON.stringify({ detail: 'backend unreachable' }))
          }
        }),
    },
  }
  return {
    plugins: [react()],
    server: {
      port: 5173,
      strictPort: true,
      proxy,
    },
    build: {
      rollupOptions: {
        output: {
          manualChunks: {
            charts: ['recharts'],
            vendor: ['react', 'react-dom', 'react-router-dom', 'axios'],
          },
        },
      },
    },
    preview: { port: 4173, proxy },
    test: {
      environment: 'jsdom',
      globals: true,
      setupFiles: './src/test/setup.js',
      css: false,
      // Whole-app render tests can exceed the 5 s default when the full suite runs in parallel on this machine.
      testTimeout: 15000,
    },
  }
})
