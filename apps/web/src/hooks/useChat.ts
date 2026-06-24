import { ApiError, postChat } from '@/lib/api';
import { queryKeys } from '@/lib/queryKeys';
import { useChatStore } from '@/stores/ChatStore';
import type { HTTPValidationError } from '@/types/chat';
import { useIsMutating, useMutation } from '@tanstack/react-query';

const errorMessage = (err: unknown): string => {
  if (err instanceof ApiError) {
    const detail = (err.body as HTTPValidationError | undefined)?.detail?.[0]?.msg;
    return detail ?? `Request failed (${err.status}).`;
  }
  return `Request failed: ${String(err)}`;
};

/**
 * Owns the POST /api/chat round-trip. The store keeps client state (messages,
 * provider, threadId); this hook drives the request and writes results back.
 */
export const useChat = () => {
  const appendMessage = useChatStore((s) => s.appendMessage);
  const provider = useChatStore((s) => s.provider);
  const threadId = useChatStore((s) => s.threadId);

  const mutation = useMutation({
    mutationKey: queryKeys.chat.all(),
    mutationFn: (content: string) =>
      postChat({ messages: [{ role: 'user', content }], provider, thread_id: threadId }),
    onMutate: (content) => appendMessage({ role: 'user', content }),
    onSuccess: (data) => appendMessage(data.message),
    onError: (err) => appendMessage({ role: 'assistant', content: errorMessage(err) }),
  });

  const send = (text: string) => {
    const content = text.trim();
    if (!content || mutation.isPending) return;
    mutation.mutate(content);
  };

  return { send, isPending: mutation.isPending };
};

/** Shared in-flight status of the chat mutation, observable from any component. */
export const useChatPending = (): boolean =>
  useIsMutating({ mutationKey: queryKeys.chat.all() }) > 0;
