import type { ChatRequest } from '@/types/chat';

/**
 * Centralized TanStack Query keys. Use these everywhere instead of inline arrays
 * so invalidation and caching stay consistent.
 */
export const queryKeys = {
  health: () => ['health'] as const,
  chat: {
    all: () => ['chat'] as const,
    thread: (threadId: string) => ['chat', 'thread', threadId] as const,
    request: (payload: ChatRequest) => ['chat', 'request', payload] as const,
  },
} as const;
