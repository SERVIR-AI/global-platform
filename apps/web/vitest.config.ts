import { fileURLToPath, URL } from 'node:url';
import { defineConfig } from 'vitest/config';

// Unit tests for the pure auth helpers (src/lib/auth.ts). The '@' alias must
// match vite.config.ts.
export default defineConfig({
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  test: {
    environment: 'happy-dom',
  },
});
