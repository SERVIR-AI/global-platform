import type Map from 'ol/Map';
import { create } from 'zustand';

type BasemapType = 'Street' | 'Satellite';

interface MapStore {
  basemap: BasemapType;
  setBasemap: (basemap: BasemapType) => void;
  /** The live OpenLayers map, shared so other components can add/remove layers. */
  map: Map | null;
  setMap: (map: Map | null) => void;
}

export const useMapStore = create<MapStore>((set) => ({
  basemap: 'Street',
  setBasemap: (basemap) => set({ basemap }),
  map: null,
  setMap: (map) => set({ map }),
}));
