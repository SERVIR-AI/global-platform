import { fileURLToPath, URL } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// The API is a separate, cross-origin service (apps/api on :8000, all routes
// under /api). Proxy that prefix in dev so the frontend can fetch relative
// paths (/api/...) without hardcoding a host or relying on CORS.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
});
