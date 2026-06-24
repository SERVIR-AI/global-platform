import { create } from 'zustand';

type BasemapType = 'Street' | 'Satellite';

interface MapStore {
  basemap: BasemapType;
  setBasemap: (basemap: BasemapType) => void;
}

export const useMapStore = create<MapStore>((set) => ({
  basemap: 'Street',
  setBasemap: (basemap) => set({ basemap }),
}));
