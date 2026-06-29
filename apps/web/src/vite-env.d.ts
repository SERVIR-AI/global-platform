/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Base URL of the backend API (e.g. https://api.example.com). Routes are
   * appended to this (e.g. `${VITE_API_BASE_URL}/api/chat`). Leave empty to use
   * same-origin relative paths — in dev these are proxied to the backend by
   * vite.config.ts.
   */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
