import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Local development proxies API and health requests to the FastAPI controller.
// In production the built static bundle is served same-origin with the API,
// so all requests use same-origin relative paths (/api/...).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/healthz': 'http://127.0.0.1:8000',
    },
  },
})
