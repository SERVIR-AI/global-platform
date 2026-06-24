import type { ChatMessage, ChatProvider } from '@/types/chat';
import { create } from 'zustand';

interface ChatStore {
  provider: ChatProvider;
  setProvider: (provider: ChatProvider) => void;
  messages: ChatMessage[];
  appendMessage: (message: ChatMessage) => void;
  threadId: string;
}

// A stable id per session so the backend's checkpointer keeps multi-turn memory.
const newThreadId = () =>
  globalThis.crypto?.randomUUID?.() ?? `t-${Date.now()}-${Math.random().toString(36).slice(2)}`;

export const useChatStore = create<ChatStore>((set) => ({
  provider: 'gemini',
  setProvider: (provider) => set({ provider }),
  messages: [],
  appendMessage: (message) => set((s) => ({ messages: [...s.messages, message] })),
  threadId: newThreadId(),
}));
