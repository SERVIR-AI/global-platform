import { buildChatLayers } from '@/lib/chatLayers';
import type { ChatItem, ChatLayer, ChatProvider, ChatRequest, ChatResponse } from '@/types/chat';
import { create } from 'zustand';

export type UseCase = 'risk' | 'food-security';

interface ChatStore {
  provider: ChatProvider;
  setProvider: (provider: ChatProvider) => void;
  /** Which capability the chat talks to; each mode has its own endpoint. */
  useCase: UseCase;
  setUseCase: (useCase: UseCase) => void;
  messages: ChatItem[];
  /** Append a turn (request or response); its map layers are derived on add. */
  appendMessage: (message: ChatRequest | ChatResponse) => void;
  /** Flip a layer's `visible` flag; the map reconciler picks it up. */
  toggleLayer: (layer: ChatLayer) => void;
  /** Set a layer's opacity (0..1); the map reconciler applies it. */
  setLayerOpacity: (layer: ChatLayer, opacity: number) => void;
  threadId: string;
}

// A stable id per session so the backend's checkpointer keeps multi-turn memory.
const newThreadId = () =>
  globalThis.crypto?.randomUUID?.() ?? `t-${Date.now()}-${Math.random().toString(36).slice(2)}`;

// Return a new messages array with `target` replaced by `update(target)`,
// touching only the turn (and layer) that owns it.
const updateLayer = (
  messages: ChatItem[],
  target: ChatLayer,
  update: (layer: ChatLayer) => ChatLayer,
): ChatItem[] =>
  messages.map((m) =>
    m.layers.includes(target)
      ? { ...m, layers: m.layers.map((l) => (l === target ? update(l) : l)) }
      : m,
  );

export const useChatStore = create<ChatStore>((set) => ({
  provider: 'claude',
  setProvider: (provider) => set({ provider }),
  useCase: 'risk',
  setUseCase: (useCase) => set({ useCase }),
  messages: [],
  appendMessage: (message) =>
    set((s) => {
      const item: ChatItem = { ...message, layers: buildChatLayers(message) };
      // A response carrying map layers takes over the map: hide every existing
      // layer (the reconciler then removes them) so only this turn's layers show.
      // A layerless response — or any request — just appends.
      const takesOverMap = 'id' in message && item.layers.length > 0;
      const prior = takesOverMap
        ? s.messages.map((m) => ({
            ...m,
            layers: m.layers.map((l) => ({ ...l, visible: false })),
          }))
        : s.messages;
      return { messages: [...prior, item] };
    }),
  toggleLayer: (target) =>
    set((s) => ({
      messages: updateLayer(s.messages, target, (l) => ({ ...l, visible: !l.visible })),
    })),
  setLayerOpacity: (target, opacity) =>
    set((s) => ({ messages: updateLayer(s.messages, target, (l) => ({ ...l, opacity })) })),
  threadId: newThreadId(),
}));
