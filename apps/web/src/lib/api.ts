import type {
  ChatRequest,
  ChatResponse,
  HealthResponse,
  HTTPValidationError,
} from '@/types/chat';

/**
 * Error thrown when an /api call returns a non-2xx response. Carries the HTTP
 * status and the parsed body (FastAPI's validation/error payload when present).
 */
export class ApiError extends Error {
  constructor(
    public status: number,
    public body: HTTPValidationError | unknown,
    message?: string,
  ) {
    super(message ?? `API request failed (${status})`);
    this.name = 'ApiError';
  }
}

// Backend base URL. Empty by default -> same-origin relative paths, which Vite
// proxies to the backend in dev (see vite.config.ts). Set VITE_API_BASE_URL to
// point at a cross-origin API (e.g. in production). Trailing slash is trimmed so
// `${API_BASE_URL}/api/...` never doubles up.
const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '');

const request = async <T>(path: string, init?: RequestInit): Promise<T> => {
  const res = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  });

  const body = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, body);
  return body as T;
};

/** GET /api/health — service liveness and active configuration summary. */
export const getHealth = (signal?: AbortSignal): Promise<HealthResponse> =>
  request<HealthResponse>('/api/health', { method: 'GET', signal });

/** POST /api/chat — send a conversation and get the assistant's reply. */
export const postChat = (payload: ChatRequest, signal?: AbortSignal): Promise<ChatResponse> =>
  request<ChatResponse>('/api/chat', {
    method: 'POST',
    body: JSON.stringify(payload),
    signal,
  });
