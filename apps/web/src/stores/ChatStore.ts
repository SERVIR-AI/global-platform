import { create } from 'zustand';

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
}

interface ChatStore {
  messages: ChatMessage[];
  loading: boolean;
  threadId: string;
  send: (text: string) => Promise<void>;
}

// A stable id per session so the backend's checkpointer keeps multi-turn memory.
const newThreadId = () =>
  globalThis.crypto?.randomUUID?.() ?? `t-${Date.now()}-${Math.random().toString(36).slice(2)}`;

export const useChatStore = create<ChatStore>((set, get) => ({
  messages: [],
  loading: false,
  threadId: newThreadId(),

  send: async (text) => {
    const content = text.trim();
    if (!content || get().loading) return;

    const userMsg: ChatMessage = { role: 'user', content };
    set((s) => ({ messages: [...s.messages, userMsg], loading: true }));

    try {
      // Relative path -> Vite proxies /api to the backend (see vite.config.ts).
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: [userMsg], thread_id: get().threadId }),
      });
      const data = await res.json();
      const answer =
        data?.message?.content ?? data?.detail ?? 'Sorry — no response from the server.';
      set((s) => ({ messages: [...s.messages, { role: 'assistant', content: answer }], loading: false }));
    } catch (err) {
      set((s) => ({
        messages: [...s.messages, { role: 'assistant', content: `Request failed: ${String(err)}` }],
        loading: false,
      }));
    }
  },
}));
