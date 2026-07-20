import { ApiError, postChat, postFoodSecurityChat } from '@/lib/api';
import { queryKeys } from '@/lib/queryKeys';
import { useChatStore } from '@/stores/ChatStore';
import { toChatGeometry, useCustomGeometryStore } from '@/stores/CustomGeometryStore';
import type { ChatProvider, ChatRequest, ChatResponse, HTTPValidationError } from '@/types/chat';
import { useIsMutating, useMutation } from '@tanstack/react-query';

const errorMessage = (err: unknown): string => {
  if (err instanceof ApiError) {
    const detail = (err.body as { detail?: unknown } | undefined)?.detail;
    if (typeof detail === 'string') return detail; // FastAPI string details (400/404/502)
    const msg = (err.body as HTTPValidationError | undefined)?.detail?.[0]?.msg;
    return msg ?? `Request failed (${err.status}).`;
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
  const useCase = useChatStore((s) => s.useCase);
  const threadId = useChatStore((s) => s.threadId);
  const geometry = useCustomGeometryStore((s) => s.geometry);
  const setGeometry = useCustomGeometryStore((s) => s.setGeometry);

  const mutation = useMutation({
    mutationKey: queryKeys.chat.all(),
    mutationFn: async (request: ChatRequest): Promise<ChatResponse> => {
      if (useCase !== 'food-security') return postChat(request);
      // The brief endpoint: adapt its response into the ChatResponse shape the
      // store/bubbles already know; brief-specific fields ride along.
      const adjust = useChatStore.getState().calendarAdjust;
      const body = await postFoodSecurityChat({
        question: request.messages[0].content,
        provider: request.provider,
        verbose: true,
        calendar: adjust?.seasons ?? null,
        calendar_country: adjust?.country ?? null,
        calendar_crop: adjust?.crop ?? null,
      });
      // The brief endpoint returns usage as a per-call [{in, out}] list; fold it
      // into the Usage shape the UI types expect.
      const calls = (body.usage as unknown as { in: number; out: number }[] | undefined) ?? [];
      const input = calls.reduce((a, u) => a + (u.in ?? 0), 0);
      const output = calls.reduce((a, u) => a + (u.out ?? 0), 0);
      return {
        id: globalThis.crypto?.randomUUID?.() ?? `fs-${Date.now()}`,
        thread_id: threadId,
        message: {
          role: 'assistant',
          content: body.declined
            ? (body.decline_reason ?? 'The system declined to answer.')
            : (body.brief ?? ''),
        },
        created_at: new Date().toISOString(),
        ...body,
        usage: { input_tokens: input, output_tokens: output, total_tokens: input + output },
      } as ChatResponse;
    },
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
      verbose: true, // return the step trace (route → resolve L1/L2 → compute → overlay)
      geometry: useCase === 'risk' ? toChatGeometry(geometry) : null,
      created_at: new Date().toISOString(),
    });
  };

  return { send, isPending: mutation.isPending };
};

/** Shared in-flight status of the chat mutation, observable from any component. */
export const useChatPending = (): boolean =>
  useIsMutating({ mutationKey: queryKeys.chat.all() }) > 0;
