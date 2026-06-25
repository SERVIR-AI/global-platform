import { buildChatLayers } from '@/lib/chatLayers';
import type { ChatItem, ChatProvider, ChatRequest, ChatResponse } from '@/types/chat';
import { create } from 'zustand';

interface ChatStore {
  provider: ChatProvider;
  setProvider: (provider: ChatProvider) => void;
  messages: ChatItem[];
  /** Append a turn (request or response); its map layers are derived on add. */
  appendMessage: (message: ChatRequest | ChatResponse) => void;
  threadId: string;
}

// A stable id per session so the backend's checkpointer keeps multi-turn memory.
const newThreadId = () =>
  globalThis.crypto?.randomUUID?.() ?? `t-${Date.now()}-${Math.random().toString(36).slice(2)}`;

export const useChatStore = create<ChatStore>((set) => ({
  provider: 'gemini',
  setProvider: (provider) => set({ provider }),
  messages: [],
  appendMessage: (message) =>
    set((s) => ({ messages: [...s.messages, { ...message, layers: buildChatLayers(message) }] })),
  threadId: newThreadId(),
}));
