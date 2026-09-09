import { fileURLToPath, URL } from 'node:url';
import { defineConfig } from 'vitest/config';

// Unit tests for the pure auth helpers (src/lib/auth.ts) — this repo's first
// frontend tests. A DOM env supplies location/sessionStorage/history; network
// (fetch) is mocked per test. The '@' alias must match vite.config.ts.
//
// happy-dom rather than jsdom: jsdom's Location is built from own non-
// configurable properties, so location.assign — what login/logout/switchAccount
// navigate through — cannot be stubbed there. happy-dom is the same class of
// environment and allows the spy.
export default defineConfig({
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  test: {
    environment: 'happy-dom',
  },
});
