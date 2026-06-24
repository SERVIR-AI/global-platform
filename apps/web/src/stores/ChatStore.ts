import { ChatProvider } from '@/types/chat';
import { create } from 'zustand';

interface ChatStore {
  provider: ChatProvider;
  setProvider: (provider: ChatProvider) => void;
}

export const UseChatStore = create<ChatStore>((set) => ({
  provider: 'gemini',
  setProvider: (provider) => set({ provider }),
}));
