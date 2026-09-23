import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In dev, proxy the read API to the FastAPI service so the SPA stays
// same-origin (no CORS). Override with API_PROXY_TARGET.
const apiTarget = process.env.API_PROXY_TARGET ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: true,
    proxy: {
      '/status': apiTarget,
      '/healthz': apiTarget,
    },
  },
  preview: {
    port: 5173,
    host: true,
    proxy: {
      '/status': apiTarget,
      '/healthz': apiTarget,
    },
  },
})
