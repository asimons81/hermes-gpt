import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// Dev server proxies the sidecar API so the SPA runs against a live
// hermes-gpt server.  Production build output (web/dist) is served by the
// Starlette app under the `/ui` mount when HERMES_GPT_UI_ENABLED=1.
export default defineConfig({
  plugins: [react()],
  base: '/ui/',
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.HERMES_GPT_UI_PROXY || 'http://127.0.0.1:7677',
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
  },
});
