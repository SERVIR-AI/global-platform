import { create } from 'zustand';

interface UserInterfaceStore {
  chatExpanded: boolean;
  mapsExpanded: boolean;
  setChatExpanded: (chatExpanded: boolean) => void;
  setMapsExpanded: (mapsExpanded: boolean) => void;
}

export const UseUserInterfaceStore = create<UserInterfaceStore>((set) => ({
  chatExpanded: true,
  mapsExpanded: true,
  setChatExpanded: (chatExpanded) => set({ chatExpanded }),
  setMapsExpanded: (mapsExpanded) => set({ mapsExpanded }),
}));
