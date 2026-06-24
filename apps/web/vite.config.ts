import { fileURLToPath, URL } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// The API is a separate, cross-origin service (apps/api, all routes under /api).
// Proxy that prefix in dev so the frontend can fetch relative paths (/api/...)
// without hardcoding a host or relying on CORS. Backend runs on :8001 here
// (:8000 is taken by another local service); change this if you move it.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8001',
    },
  },
});
