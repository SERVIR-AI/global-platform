import { ApiError, postChat } from '@/lib/api';
import { queryKeys } from '@/lib/queryKeys';
import { useChatStore } from '@/stores/ChatStore';
import { toChatGeometry, useCustomGeometryStore } from '@/stores/CustomGeometryStore';
import type { ChatProvider, ChatRequest, ChatResponse, HTTPValidationError } from '@/types/chat';
import { useIsMutating, useMutation } from '@tanstack/react-query';

const errorMessage = (err: unknown): string => {
  if (err instanceof ApiError) {
    const detail = (err.body as HTTPValidationError | undefined)?.detail?.[0]?.msg;
    return detail ?? `Request failed (${err.status}).`;
  }
  return `Request failed: ${String(err)}`;
};

// A failed call still needs to land in the store as a turn; wrap the error text
// in a minimal assistant ChatResponse (no geo fields, so no layers).
const errorResponse = (err: unknown, threadId: string, provider: ChatProvider): ChatResponse => ({
  id: globalThis.crypto?.randomUUID?.() ?? `err-${Date.now()}`,
  thread_id: threadId,
  message: { role: 'assistant', content: errorMessage(err) },
  provider,
  model: '',
});

/**
 * Owns the POST /api/chat round-trip. The store keeps client state (messages,
 * provider, threadId); this hook drives the request and writes results back.
 */
export const useChat = () => {
  const appendMessage = useChatStore((s) => s.appendMessage);
  const provider = useChatStore((s) => s.provider);
  const threadId = useChatStore((s) => s.threadId);
  const geometry = useCustomGeometryStore((s) => s.geometry);
  const setGeometry = useCustomGeometryStore((s) => s.setGeometry);

  const mutation = useMutation({
    mutationKey: queryKeys.chat.all(),
    mutationFn: (request: ChatRequest) => postChat(request),
    // Echo the request into the store immediately; append the response on success.
    onMutate: (request) => {
      appendMessage(request);
      setGeometry(null);
    },
    onSuccess: (data) => appendMessage(data),
    onError: (err) => appendMessage(errorResponse(err, threadId, provider)),
  });

  const send = (text: string) => {
    const content = text.trim();
    if (!content || mutation.isPending) return;
    mutation.mutate({
      messages: [{ role: 'user', content }],
      provider,
      thread_id: threadId,
      geometry: toChatGeometry(geometry),
    });
  };

  return { send, isPending: mutation.isPending };
};

/** Shared in-flight status of the chat mutation, observable from any component. */
export const useChatPending = (): boolean =>
  useIsMutating({ mutationKey: queryKeys.chat.all() }) > 0;
