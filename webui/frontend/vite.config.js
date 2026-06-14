import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

// The frontend is a SPA that talks to the FastAPI server (webui/server.py).
// All API + SSE calls go through /api and are proxied to :8000 in dev, so the
// browser stays same-origin (no CORS, SSE works through the proxy).
export default defineConfig({
  plugins: [svelte()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
    },
  },
});
