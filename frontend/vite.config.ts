import { fileURLToPath } from 'node:url';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vitest/config';

// `split-video edit` serves the API on 8765; the dev server proxies to it so the browser sees one origin.
const backend = 'http://127.0.0.1:8765';

export default defineConfig({
  plugins: [svelte()],
  // FastAPI serves the build under /app/ (see docs/adr/0004-frontend-stack-and-structure.md).
  base: '/app/',
  build: {
    outDir: fileURLToPath(new URL('../src/split_video/editor/static_app', import.meta.url)),
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    // SSE (/api/v1/events) streams through the proxy unbuffered; no response transformation is applied.
    proxy: {
      '/api': { target: backend, changeOrigin: false },
      '/media': { target: backend, changeOrigin: false },
    },
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts'],
  },
});
